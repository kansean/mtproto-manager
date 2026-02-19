import json
import os
import secrets
import tempfile

DATA_DIR = os.environ.get("MTPROTO_DATA_DIR", "/opt/mtproto/data")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

FAKE_TLS_DOMAIN_POOL = [
    # International — major CDNs and services
    "www.cloudflare.com",
    "www.google.com",
    "www.apple.com",
    "www.microsoft.com",
    "www.amazon.com",
    "www.facebook.com",
    "www.instagram.com",
    "www.twitter.com",
    "www.youtube.com",
    "www.netflix.com",
    "www.linkedin.com",
    "www.github.com",
    "www.stackoverflow.com",
    "www.reddit.com",
    "www.wikipedia.org",
    "www.mozilla.org",
    "www.dropbox.com",
    "www.spotify.com",
    "www.twitch.tv",
    "www.discord.com",
    # Russian — ISPs will never block these
    "vk.com",
    "ya.ru",
    "mail.ru",
    "ok.ru",
    "yandex.ru",
    "dzen.ru",
    "rutube.ru",
    "sber.ru",
    "gosuslugi.ru",
    "mos.ru",
]

DEFAULT_CONFIG = {
    "admin_username": "admin",
    "admin_password_hash": "",
    "secret_key": "",
    "server_domain": "",
    "server_ip": "",
    "proxy_port": 2443,
    "proxy_tag": "",
    "proxy_container_name": "mtg-proxy",
    "proxy_image": "mtg-custom",
    "users": [],
    "fake_tls_domain": "www.cloudflare.com",
    "proxy_buffer_size": "32KB",
    "proxy_prefer_ip": "v4",
    "stats_enabled": True,
    "traffic_limit_gb": 0,
    "throttle_speed_mbps": 1,
    "port_443_mode": False,
}


def migrate_user_ports(cfg):
    """Assign port to users that don't have one yet."""
    base_port = cfg.get("proxy_port", 2443)
    users = cfg.get("users", [])
    changed = False
    for i, user in enumerate(users):
        if "port" not in user:
            user["port"] = base_port + i
            changed = True
    return changed


def next_available_port(cfg):
    """Return the next unused port for a new user."""
    base_port = cfg.get("proxy_port", 2443)
    users = cfg.get("users", [])
    if not users:
        return base_port
    used_ports = [u.get("port", base_port) for u in users]
    return max(used_ports) + 1


def load_config():
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            cfg = json.load(f)
        for key, val in DEFAULT_CONFIG.items():
            if key not in cfg:
                cfg[key] = val
        if migrate_user_ports(cfg):
            save_config(cfg)
        return cfg
    return DEFAULT_CONFIG.copy()


def save_config(cfg):
    """Atomic config write: write to temp file, then rename."""
    os.makedirs(DATA_DIR, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=DATA_DIR, suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        os.replace(tmp_path, CONFIG_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def get_user_effective_limits(user, cfg):
    """Return (traffic_limit_gb, throttle_speed_mbps) for a user, falling back to global."""
    traffic_limit = user.get("traffic_limit_gb", 0)
    if not traffic_limit or traffic_limit <= 0:
        traffic_limit = cfg.get("traffic_limit_gb", 0)
    throttle_speed = user.get("throttle_speed_mbps", 0)
    if not throttle_speed or throttle_speed <= 0:
        throttle_speed = cfg.get("throttle_speed_mbps", 1)
    return traffic_limit, throttle_speed


def get_or_create_secret_key():
    cfg = load_config()
    if not cfg.get("secret_key"):
        cfg["secret_key"] = secrets.token_hex(32)
        save_config(cfg)
    return cfg["secret_key"]
