"""Generate nginx stream/http configs for port 443 SNI routing."""

import os
import logging

import docker

from app.config import DATA_DIR

logger = logging.getLogger(__name__)

NGINX_DATA_DIR = os.path.join(DATA_DIR, "nginx")
CONF_D_DIR = os.path.join(NGINX_DATA_DIR, "conf.d")
STREAM_D_DIR = os.path.join(NGINX_DATA_DIR, "stream.d")


def bootstrap_nginx_configs(cfg):
    """Called at app startup. Ensure data/nginx dirs exist and migrate legacy config."""
    os.makedirs(CONF_D_DIR, exist_ok=True)
    os.makedirs(STREAM_D_DIR, exist_ok=True)

    default_conf = os.path.join(CONF_D_DIR, "default.conf")
    if not os.path.exists(default_conf):
        # Try migrating from legacy location (nginx/default.conf mounted as /etc/nginx/conf.d/default.conf)
        # Inside the app container, the install dir files are at /app level,
        # but the nginx dir is on the host. We write a basic HTTP config as fallback.
        _write_default_http_config(cfg, default_conf)


def _write_default_http_config(cfg, path):
    """Write a basic HTTP-only nginx config (no SSL, no port 443 mode)."""
    content = """\
server {
    listen 80;
    server_name _;

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        proxy_pass http://app:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
"""
    with open(path, "w") as f:
        f.write(content)


def write_http_config(cfg):
    """Generate data/nginx/conf.d/default.conf.

    When port_443_mode ON: SSL listens on 8443 (stream block handles 443).
    When OFF: SSL on 443 (standard behavior).
    """
    default_conf = os.path.join(CONF_D_DIR, "default.conf")
    domain = cfg.get("server_domain", "")
    port_443_mode = cfg.get("port_443_mode", False)

    # Check if SSL certs exist
    ssl_cert = f"/etc/letsencrypt/live/{domain}/fullchain.pem" if domain else ""
    ssl_key = f"/etc/letsencrypt/live/{domain}/privkey.pem" if domain else ""

    # Determine if we have SSL configured by checking cert dir on host
    has_ssl = False
    if domain:
        host_cert_dir = os.path.join(DATA_DIR, "..", "certbot", "conf", "live", domain)
        host_cert_dir = os.path.normpath(host_cert_dir)
        if os.path.isdir(host_cert_dir):
            has_ssl = True

    if not has_ssl:
        _write_default_http_config(cfg, default_conf)
        return

    if port_443_mode:
        # SSL on port 8443 — stream block forwards 443 traffic here based on SNI
        ssl_listen_port = 8443
    else:
        ssl_listen_port = 443

    content = f"""\
server {{
    listen 80;
    server_name {domain};

    location /.well-known/acme-challenge/ {{
        root /var/www/certbot;
    }}

    location / {{
        return 301 https://$host$request_uri;
    }}
}}

server {{
    listen {ssl_listen_port} ssl;
    server_name {domain};

    ssl_certificate {ssl_cert};
    ssl_certificate_key {ssl_key};

    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;

    location / {{
        proxy_pass http://app:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}
}}
"""
    with open(default_conf, "w") as f:
        f.write(content)


def write_stream_config(cfg):
    """Generate data/nginx/stream.d/proxy.conf with SNI map.

    Uses map-to-backend (host:port) with variable proxy_pass so nginx
    resolves container names via Docker DNS at RUNTIME, not at config load.
    This way nginx reload never fails even if a container is down.
    When mode OFF: removes the file.
    """
    stream_conf = os.path.join(STREAM_D_DIR, "proxy.conf")

    if not cfg.get("port_443_mode", False):
        # Remove stream config when mode is off
        if os.path.exists(stream_conf):
            os.remove(stream_conf)
        return

    domain = cfg.get("server_domain", "")
    users = cfg.get("users", [])
    enabled_users = [u for u in users if u.get("enabled", True)]

    # Filter to only users with fake_tls_domain assigned
    routable_users = [u for u in enabled_users if u.get("fake_tls_domain")]

    # Build SNI map: domain -> host:port (no upstream blocks!)
    map_entries = []

    for user in routable_users:
        port = user.get("port", 2443)
        ftd = user["fake_tls_domain"]
        container_name = f"mtg-proxy-{port}"
        map_entries.append(f"    {ftd}  {container_name}:{port};")

    # Panel: server domain and default → local SSL on 8443
    if domain:
        map_entries.append(f"    {domain}  127.0.0.1:8443;")
    map_entries.append("    default  127.0.0.1:8443;")

    map_block = "map $ssl_preread_server_name $backend {\n"
    map_block += "\n".join(map_entries)
    map_block += "\n}"

    content = f"""\
{map_block}

server {{
    listen 443;
    ssl_preread on;
    resolver 127.0.0.11 valid=10s ipv6=off;
    proxy_pass $backend;
    proxy_connect_timeout 5s;
    proxy_timeout 24h;
}}
"""
    with open(stream_conf, "w") as f:
        f.write(content)


def reload_nginx():
    """Reload nginx config via Docker SDK exec."""
    try:
        client = docker.from_env()
        nginx = client.containers.get("mtproto-nginx")
        result = nginx.exec_run("nginx -s reload")
        exit_code = result.exit_code if hasattr(result, 'exit_code') else result[0]
        if exit_code != 0:
            output = result.output if hasattr(result, 'output') else result[1]
            logger.warning("nginx reload failed (exit %s): %s", exit_code, output)
        else:
            logger.info("nginx reloaded")
    except docker.errors.NotFound:
        logger.warning("nginx container 'mtproto-nginx' not found, cannot reload")
    except Exception as e:
        logger.warning("Failed to reload nginx: %s", e)


def apply_port_443_mode(cfg):
    """Full cycle: write http + stream configs, then reload nginx."""
    write_http_config(cfg)
    write_stream_config(cfg)
    reload_nginx()
