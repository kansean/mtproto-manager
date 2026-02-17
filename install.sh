#!/usr/bin/env bash
set -euo pipefail

# MTProto Proxy Manager — One-Command Installer
# Usage: curl -sSL <url>/install.sh | bash

INSTALL_DIR="/opt/mtproto"
REPO="kansean/mtproto-manager"
BRANCH="master"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}[INFO]${NC} $*"; }
ok()    { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; exit 1; }

# ─── Check root ───
if [ "$(id -u)" -ne 0 ]; then
    error "This script must be run as root (use sudo)"
fi

echo ""
echo -e "${CYAN}╔══════════════════════════════════════╗${NC}"
echo -e "${CYAN}║   MTProto Proxy Manager Installer    ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════╝${NC}"
echo ""

# ─── Detect OS ───
detect_os() {
    if [ -f /etc/os-release ]; then
        . /etc/os-release
        OS_ID="$ID"
        OS_VERSION="${VERSION_ID:-unknown}"
    elif [ -f /etc/redhat-release ]; then
        OS_ID="centos"
        OS_VERSION=$(rpm -q --queryformat '%{VERSION}' centos-release 2>/dev/null || echo "unknown")
    else
        OS_ID="unknown"
        OS_VERSION="unknown"
    fi
    info "Detected OS: $OS_ID $OS_VERSION"
}

# ─── Detect virtualization environment ───
VIRT_TYPE="none"
detect_virt() {
    if command -v systemd-detect-virt &>/dev/null; then
        VIRT_TYPE=$(systemd-detect-virt 2>/dev/null || echo "none")
    elif [ -f /proc/1/environ ] && tr '\0' '\n' < /proc/1/environ 2>/dev/null | grep -q "container=lxc"; then
        VIRT_TYPE="lxc"
    elif [ -d /proc/vz ] && [ ! -d /proc/bc ]; then
        VIRT_TYPE="openvz"
    fi
    info "Virtualization: $VIRT_TYPE"
}

# ─── Install base prerequisites ───
install_prerequisites() {
    info "Checking prerequisites..."
    case "$OS_ID" in
        ubuntu|debian)
            apt-get update -qq
            apt-get install -y -qq curl ca-certificates tar gnupg lsb-release >/dev/null 2>&1
            ;;
        centos|rhel|rocky|almalinux)
            yum install -y -q curl ca-certificates tar >/dev/null 2>&1
            ;;
        fedora)
            dnf install -y -q curl ca-certificates tar >/dev/null 2>&1
            ;;
        *)
            # Try common package managers
            apt-get install -y -qq curl ca-certificates tar >/dev/null 2>&1 \
                || yum install -y -q curl ca-certificates tar >/dev/null 2>&1 \
                || dnf install -y -q curl ca-certificates tar >/dev/null 2>&1 \
                || warn "Could not install prerequisites automatically"
            ;;
    esac
    ok "Prerequisites installed"
}

# ─── Install Docker ───
install_docker() {
    if command -v docker &>/dev/null; then
        ok "Docker is already installed: $(docker --version)"
        return
    fi

    info "Installing Docker..."
    case "$OS_ID" in
        ubuntu|debian)
            install -m 0755 -d /etc/apt/keyrings
            curl -fsSL "https://download.docker.com/linux/$OS_ID/gpg" | gpg --dearmor -o /etc/apt/keyrings/docker.gpg 2>/dev/null
            chmod a+r /etc/apt/keyrings/docker.gpg
            echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/$OS_ID $(lsb_release -cs) stable" > /etc/apt/sources.list.d/docker.list
            apt-get update -qq
            apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin >/dev/null
            ;;
        centos|rhel|rocky|almalinux)
            yum install -y -q yum-utils >/dev/null
            yum-config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo >/dev/null
            yum install -y -q docker-ce docker-ce-cli containerd.io docker-compose-plugin >/dev/null
            ;;
        fedora)
            dnf install -y -q dnf-plugins-core >/dev/null
            dnf config-manager --add-repo https://download.docker.com/linux/fedora/docker-ce.repo >/dev/null
            dnf install -y -q docker-ce docker-ce-cli containerd.io docker-compose-plugin >/dev/null
            ;;
        *)
            warn "Unknown OS '$OS_ID'. Attempting install via get.docker.com..."
            curl -fsSL https://get.docker.com | sh
            ;;
    esac

    systemctl enable docker
    systemctl start docker
    ok "Docker installed: $(docker --version)"
}

# ─── Configure Docker for LXC/OpenVZ environments ───
configure_docker_env() {
    if [ "$VIRT_TYPE" != "lxc" ] && [ "$VIRT_TYPE" != "openvz" ]; then
        return
    fi

    info "Configuring Docker for $VIRT_TYPE environment..."

    # Create daemon.json with AppArmor disabled and security adjustments
    mkdir -p /etc/docker
    local DAEMON_JSON="/etc/docker/daemon.json"

    if [ -f "$DAEMON_JSON" ] && [ -s "$DAEMON_JSON" ]; then
        cp "$DAEMON_JSON" "${DAEMON_JSON}.bak"
        info "Existing daemon.json backed up"
    fi

    cat > "$DAEMON_JSON" <<'DAEMONJSON'
{
  "default-security-opt": ["apparmor=unconfined", "seccomp=unconfined"]
}
DAEMONJSON

    # Disable AppArmor service if present (it interferes with Docker in LXC)
    if systemctl is-active apparmor &>/dev/null; then
        systemctl stop apparmor 2>/dev/null || true
        systemctl disable apparmor 2>/dev/null || true
        info "AppArmor disabled"
    fi

    # Restart Docker to apply new config
    systemctl restart docker
    sleep 2
    ok "Docker configured for $VIRT_TYPE"
}

# ─── Install Docker Compose (if not available as plugin) ───
ensure_compose() {
    if docker compose version &>/dev/null; then
        ok "Docker Compose plugin available"
        COMPOSE_CMD="docker compose"
        return
    fi

    if command -v docker-compose &>/dev/null; then
        ok "docker-compose standalone available"
        COMPOSE_CMD="docker-compose"
        return
    fi

    info "Installing Docker Compose plugin..."
    apt-get install -y -qq docker-compose-plugin 2>/dev/null \
        || yum install -y -q docker-compose-plugin 2>/dev/null \
        || dnf install -y -q docker-compose-plugin 2>/dev/null \
        || {
            COMPOSE_VERSION=$(curl -s https://api.github.com/repos/docker/compose/releases/latest | grep '"tag_name"' | cut -d'"' -f4)
            curl -fsSL "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
            chmod +x /usr/local/bin/docker-compose
            COMPOSE_CMD="docker-compose"
            ok "Docker Compose standalone installed"
            return
        }
    COMPOSE_CMD="docker compose"
    ok "Docker Compose plugin installed"
}

# ─── Verify Docker works ───
verify_docker() {
    info "Verifying Docker..."
    if docker run --rm hello-world &>/dev/null; then
        ok "Docker is working correctly"
    else
        echo ""
        warn "Docker verification failed!"
        if [ "$VIRT_TYPE" = "lxc" ]; then
            echo -e "${YELLOW}  Your server runs inside an LXC container (Proxmox CT, etc.).${NC}"
            echo -e "${YELLOW}  Docker requires these settings on the HOST:${NC}"
            echo -e "${YELLOW}    1. Enable 'Nesting' feature for this CT${NC}"
            echo -e "${YELLOW}    2. Use a privileged CT (unprivileged won't work)${NC}"
            echo -e "${YELLOW}  Then reboot the CT and re-run this script.${NC}"
        elif [ "$VIRT_TYPE" = "openvz" ]; then
            echo -e "${YELLOW}  Your server runs on OpenVZ, which has limited Docker support.${NC}"
            echo -e "${YELLOW}  Consider using a KVM-based VPS instead.${NC}"
        else
            echo -e "${YELLOW}  Try rebooting the server and re-running this script.${NC}"
        fi
        echo ""
        error "Cannot proceed without a working Docker installation"
    fi
}

# ─── Download / extract release ───
download_release() {
    if [ -d "$INSTALL_DIR/app" ]; then
        warn "Existing installation found at $INSTALL_DIR"
        read -rp "Overwrite? (existing data/ will be preserved) [y/N]: " confirm < /dev/tty
        if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
            error "Installation cancelled"
        fi
    fi

    info "Setting up MTProto Proxy Manager in $INSTALL_DIR..."
    mkdir -p "$INSTALL_DIR"

    # If running from a local clone (install.sh is already in the repo)
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    if [ -f "$SCRIPT_DIR/docker-compose.yml" ] && [ -f "$SCRIPT_DIR/Dockerfile" ]; then
        info "Local installation detected, using files from $SCRIPT_DIR"
        if [ "$SCRIPT_DIR" != "$INSTALL_DIR" ]; then
            cp -r "$SCRIPT_DIR/app" "$INSTALL_DIR/"
            cp -r "$SCRIPT_DIR/nginx" "$INSTALL_DIR/"
            cp "$SCRIPT_DIR/docker-compose.yml" "$INSTALL_DIR/"
            cp "$SCRIPT_DIR/Dockerfile" "$INSTALL_DIR/"
            cp "$SCRIPT_DIR/.dockerignore" "$INSTALL_DIR/"
            cp "$SCRIPT_DIR/requirements.txt" "$INSTALL_DIR/"
            cp "$SCRIPT_DIR/wsgi.py" "$INSTALL_DIR/"
            [ -f "$SCRIPT_DIR/update.sh" ] && cp "$SCRIPT_DIR/update.sh" "$INSTALL_DIR/"
        fi
        return
    fi

    # Download from GitHub
    info "Downloading from GitHub..."
    TARBALL_URL="https://github.com/${REPO}/releases/latest/download/mtproto.tar.gz"
    if ! curl -fsSL "$TARBALL_URL" -o /tmp/mtproto.tar.gz 2>/dev/null; then
        info "Downloading from branch ${BRANCH}..."
        TARBALL_URL="https://github.com/${REPO}/archive/refs/heads/${BRANCH}.tar.gz"
        curl -fsSL "$TARBALL_URL" -o /tmp/mtproto.tar.gz || error "Failed to download release"
    fi

    tar -xzf /tmp/mtproto.tar.gz -C "$INSTALL_DIR" --strip-components=1
    rm -f /tmp/mtproto.tar.gz
    ok "Files extracted to $INSTALL_DIR"
}

# ─── Interactive configuration ───
configure() {
    echo ""
    echo -e "${CYAN}── Configuration ──${NC}"
    echo ""

    # Domain
    read -rp "Domain name (leave empty for IP-only access): " DOMAIN < /dev/tty
    DOMAIN="${DOMAIN:-}"

    # Proxy port
    read -rp "MTProto proxy port [2443]: " PROXY_PORT < /dev/tty
    PROXY_PORT="${PROXY_PORT:-2443}"

    # HTTPS
    ENABLE_SSL="n"
    if [ -n "$DOMAIN" ]; then
        read -rp "Enable HTTPS with Let's Encrypt? [y/N]: " ENABLE_SSL < /dev/tty
        ENABLE_SSL="${ENABLE_SSL:-n}"
    fi

    # HTTP port
    read -rp "HTTP port [80]: " HTTP_PORT < /dev/tty
    HTTP_PORT="${HTTP_PORT:-80}"

    # Generate .env file
    cat > "$INSTALL_DIR/.env" <<EOF
# MTProto Proxy Manager Configuration
DOMAIN=${DOMAIN}
PROXY_PORT=${PROXY_PORT}
HTTP_PORT=${HTTP_PORT}
HTTPS_PORT=443
ENABLE_SSL=${ENABLE_SSL}
EOF

    ok "Configuration saved to $INSTALL_DIR/.env"
}

# ─── Setup SSL ───
setup_ssl() {
    if [[ ! "$ENABLE_SSL" =~ ^[Yy]$ ]] || [ -z "$DOMAIN" ]; then
        return
    fi

    info "Setting up SSL certificate for $DOMAIN..."

    mkdir -p "$INSTALL_DIR/certbot/conf"
    mkdir -p "$INSTALL_DIR/certbot/www"

    # Start nginx temporarily for the ACME challenge
    $COMPOSE_CMD -f "$INSTALL_DIR/docker-compose.yml" up -d nginx
    sleep 3

    # Request certificate
    read -rp "Email for Let's Encrypt notifications: " CERT_EMAIL < /dev/tty
    docker run --rm \
        -v "$INSTALL_DIR/certbot/conf:/etc/letsencrypt" \
        -v "$INSTALL_DIR/certbot/www:/var/www/certbot" \
        certbot/certbot certonly \
        --webroot --webroot-path=/var/www/certbot \
        --email "$CERT_EMAIL" \
        --agree-tos --no-eff-email \
        -d "$DOMAIN"

    if [ $? -eq 0 ]; then
        # Swap nginx config to SSL version
        sed "s/__DOMAIN__/${DOMAIN}/g" "$INSTALL_DIR/nginx/ssl.conf.template" \
            > "$INSTALL_DIR/nginx/default.conf"
        ok "SSL certificate obtained and nginx configured"
    else
        warn "SSL certificate request failed. Continuing with HTTP only."
        ENABLE_SSL="n"
    fi

    $COMPOSE_CMD -f "$INSTALL_DIR/docker-compose.yml" down
}

# ─── Open firewall ports ───
setup_firewall() {
    if command -v ufw &>/dev/null; then
        info "Configuring UFW firewall..."
        ufw allow "$HTTP_PORT/tcp" >/dev/null 2>&1
        ufw allow "$PROXY_PORT/tcp" >/dev/null 2>&1
        if [[ "$ENABLE_SSL" =~ ^[Yy]$ ]]; then
            ufw allow 443/tcp >/dev/null 2>&1
        fi
        ok "Firewall rules added"
    elif command -v firewall-cmd &>/dev/null; then
        info "Configuring firewalld..."
        firewall-cmd --permanent --add-port="$HTTP_PORT/tcp" >/dev/null 2>&1
        firewall-cmd --permanent --add-port="$PROXY_PORT/tcp" >/dev/null 2>&1
        if [[ "$ENABLE_SSL" =~ ^[Yy]$ ]]; then
            firewall-cmd --permanent --add-port=443/tcp >/dev/null 2>&1
        fi
        firewall-cmd --reload >/dev/null 2>&1
        ok "Firewall rules added"
    fi
}

# ─── Start services ───
start_services() {
    info "Building and starting services..."
    cd "$INSTALL_DIR"

    COMPOSE_PROFILES=""
    if [[ "$ENABLE_SSL" =~ ^[Yy]$ ]]; then
        COMPOSE_PROFILES="--profile ssl"
    fi

    $COMPOSE_CMD $COMPOSE_PROFILES up -d --build

    # Wait for app to become ready
    info "Waiting for application to start..."
    for i in $(seq 1 30); do
        if docker logs mtproto-app 2>&1 | grep -q "INITIAL ADMIN CREDENTIALS"; then
            break
        fi
        sleep 1
    done

    ok "Services started"
}

# ─── Print summary ───
print_summary() {
    echo ""
    echo -e "${GREEN}╔══════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║   Installation Complete!                 ║${NC}"
    echo -e "${GREEN}╚══════════════════════════════════════════╝${NC}"
    echo ""

    # Get admin credentials from logs
    ADMIN_CREDS=$(docker logs mtproto-app 2>&1 | grep -A3 "INITIAL ADMIN CREDENTIALS" || true)
    if [ -n "$ADMIN_CREDS" ]; then
        echo -e "${YELLOW}$ADMIN_CREDS${NC}"
        echo ""
    fi

    SERVER_IP=$(curl -s https://ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}')

    echo -e "  ${CYAN}Web Panel:${NC}"
    if [[ "$ENABLE_SSL" =~ ^[Yy]$ ]] && [ -n "$DOMAIN" ]; then
        echo -e "    https://${DOMAIN}"
    elif [ -n "$DOMAIN" ]; then
        echo -e "    http://${DOMAIN}:${HTTP_PORT}"
    else
        echo -e "    http://${SERVER_IP}:${HTTP_PORT}"
    fi
    echo ""
    echo -e "  ${CYAN}Proxy Port:${NC} ${PROXY_PORT}"
    echo -e "  ${CYAN}Install Dir:${NC} ${INSTALL_DIR}"
    echo ""
    echo -e "  ${CYAN}Management:${NC}"
    echo -e "    Start:   cd ${INSTALL_DIR} && $COMPOSE_CMD up -d"
    echo -e "    Stop:    cd ${INSTALL_DIR} && $COMPOSE_CMD down"
    echo -e "    Logs:    cd ${INSTALL_DIR} && $COMPOSE_CMD logs -f"
    echo -e "    Update:  bash ${INSTALL_DIR}/update.sh"
    echo ""
}

# ─── Main ───
main() {
    detect_os
    detect_virt
    install_prerequisites
    install_docker
    configure_docker_env
    ensure_compose
    verify_docker
    download_release
    configure
    setup_ssl
    setup_firewall
    start_services
    print_summary
}

main
