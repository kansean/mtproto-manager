#!/usr/bin/env bash
set -euo pipefail

# MTProto Proxy Manager — Updater
# Pulls latest release, rebuilds, preserves data/

INSTALL_DIR="/opt/mtproto"
REPO="kansean/mtproto-manager"
BRANCH="master"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

if [ "$(id -u)" -ne 0 ]; then
    error "This script must be run as root (use sudo)"
fi

if [ ! -f "$INSTALL_DIR/docker-compose.yml" ]; then
    error "No installation found at $INSTALL_DIR. Run install.sh first."
fi

# Detect compose command
if docker compose version &>/dev/null; then
    COMPOSE_CMD="docker compose"
elif command -v docker-compose &>/dev/null; then
    COMPOSE_CMD="docker-compose"
else
    error "Docker Compose not found"
fi

echo ""
echo -e "${CYAN}╔══════════════════════════════════════╗${NC}"
echo -e "${CYAN}║   MTProto Proxy Manager Updater      ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════╝${NC}"
echo ""

# Backup current installation
info "Backing up current installation..."
BACKUP_DIR="/tmp/mtproto-backup-$(date +%Y%m%d%H%M%S)"
mkdir -p "$BACKUP_DIR"
cp -r "$INSTALL_DIR/app" "$BACKUP_DIR/" 2>/dev/null || true
cp "$INSTALL_DIR/docker-compose.yml" "$BACKUP_DIR/" 2>/dev/null || true
cp "$INSTALL_DIR/Dockerfile" "$BACKUP_DIR/" 2>/dev/null || true
ok "Backup saved to $BACKUP_DIR"

# Download latest release
info "Downloading latest release..."
TARBALL_URL="https://github.com/${REPO}/releases/latest/download/mtproto.tar.gz"
if ! curl -fsSL "$TARBALL_URL" -o /tmp/mtproto-update.tar.gz 2>/dev/null; then
    warn "No release tarball found, downloading from branch..."
    TARBALL_URL="https://github.com/${REPO}/archive/refs/heads/${BRANCH}.tar.gz"
    curl -fsSL "$TARBALL_URL" -o /tmp/mtproto-update.tar.gz || error "Failed to download update"
fi

# Extract to temp dir
TEMP_DIR=$(mktemp -d)
tar -xzf /tmp/mtproto-update.tar.gz -C "$TEMP_DIR" --strip-components=1
rm -f /tmp/mtproto-update.tar.gz

# Stop containers
info "Stopping services..."
cd "$INSTALL_DIR"
$COMPOSE_CMD down || true

# Update app files (preserve data/, .env, certbot/, nginx/default.conf)
info "Updating application files..."
rm -rf "$INSTALL_DIR/app"
cp -r "$TEMP_DIR/app" "$INSTALL_DIR/"
cp "$TEMP_DIR/Dockerfile" "$INSTALL_DIR/"
cp "$TEMP_DIR/docker-compose.yml" "$INSTALL_DIR/"
cp "$TEMP_DIR/requirements.txt" "$INSTALL_DIR/"
cp "$TEMP_DIR/wsgi.py" "$INSTALL_DIR/"
cp "$TEMP_DIR/.dockerignore" "$INSTALL_DIR/"
[ -f "$TEMP_DIR/update.sh" ] && cp "$TEMP_DIR/update.sh" "$INSTALL_DIR/"
[ -f "$TEMP_DIR/Dockerfile.mtg" ] && cp "$TEMP_DIR/Dockerfile.mtg" "$INSTALL_DIR/"

# Update nginx build files and config template
if [ -f "$TEMP_DIR/nginx/ssl.conf.template" ]; then
    cp "$TEMP_DIR/nginx/ssl.conf.template" "$INSTALL_DIR/nginx/"
fi
[ -f "$TEMP_DIR/nginx/Dockerfile.nginx" ] && cp "$TEMP_DIR/nginx/Dockerfile.nginx" "$INSTALL_DIR/nginx/"
[ -f "$TEMP_DIR/nginx/nginx.conf" ] && cp "$TEMP_DIR/nginx/nginx.conf" "$INSTALL_DIR/nginx/"

# Migrate nginx config to data/nginx/conf.d if needed
mkdir -p "$INSTALL_DIR/data/nginx/conf.d"
mkdir -p "$INSTALL_DIR/data/nginx/stream.d"
if [ ! -f "$INSTALL_DIR/data/nginx/conf.d/default.conf" ] && [ -f "$INSTALL_DIR/nginx/default.conf" ]; then
    info "Migrating nginx config to data/nginx/conf.d..."
    cp "$INSTALL_DIR/nginx/default.conf" "$INSTALL_DIR/data/nginx/conf.d/default.conf"
    ok "Nginx config migrated"
fi

rm -rf "$TEMP_DIR"
ok "Files updated"

# Rebuild custom mtg image
if [ -f "$INSTALL_DIR/Dockerfile.mtg" ]; then
    info "Rebuilding custom mtg image..."
    docker build -t mtg-custom -f "$INSTALL_DIR/Dockerfile.mtg" "$INSTALL_DIR" \
        && ok "Custom mtg image rebuilt" \
        || warn "Failed to build custom mtg image; traffic throttling will not work"
fi

# Rebuild and restart
info "Rebuilding and starting services..."
cd "$INSTALL_DIR"

COMPOSE_PROFILES=""
if grep -q 'ENABLE_SSL=[Yy]' "$INSTALL_DIR/.env" 2>/dev/null; then
    COMPOSE_PROFILES="--profile ssl"
fi

$COMPOSE_CMD $COMPOSE_PROFILES up -d --build

ok "Update complete!"
echo ""
echo -e "  ${CYAN}Backup:${NC} $BACKUP_DIR"
echo -e "  ${CYAN}Logs:${NC}   cd $INSTALL_DIR && $COMPOSE_CMD logs -f"
echo ""
