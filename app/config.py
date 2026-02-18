import json
import os
import secrets
import tempfile

DATA_DIR = os.environ.get("MTPROTO_DATA_DIR", "/opt/mtproto/data")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

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
    "fake_tls_domain": "google.com",
    "proxy_buffer_size": "32KB",
    "proxy_prefer_ip": "v4",
    "stats_enabled": True,
    "traffic_limit_gb": 0,
    "throttle_speed_mbps": 1,
}


def load_config():
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r") as f:
            cfg = json.load(f)
        for key, val in DEFAULT_CONFIG.items():
            if key not in cfg:
                cfg[key] = val
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


def get_or_create_secret_key():
    cfg = load_config()
    if not cfg.get("secret_key"):
        cfg["secret_key"] = secrets.token_hex(32)
        save_config(cfg)
    return cfg["secret_key"]
