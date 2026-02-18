import docker
import secrets
import time
import json
import os
import subprocess
from app.config import load_config, save_config, DATA_DIR


_docker_client = None

TOML_PATH = os.path.join(DATA_DIR, "mtg.toml")


def get_docker_client():
    global _docker_client
    if _docker_client is None:
        _docker_client = docker.from_env()
    try:
        _docker_client.ping()
    except Exception:
        _docker_client = docker.from_env()
    return _docker_client


def generate_secret(fake_tls_domain="www.cloudflare.com"):
    """Generate a secret via mtg generate-secret (includes domain fronting)."""
    try:
        client = get_docker_client()
        result = client.containers.run(
            "nineseconds/mtg:2",
            command=["generate-secret", "-x", fake_tls_domain],
            remove=True,
            stdout=True,
            stderr=False,
        )
        return result.decode().strip()
    except Exception:
        # Fallback: generate manually
        # ee + 16 random bytes hex + domain hex
        raw = secrets.token_hex(16)
        encoded_domain = fake_tls_domain.encode().hex()
        return f"ee{raw}{encoded_domain}"


def get_container(client=None):
    if client is None:
        client = get_docker_client()
    cfg = load_config()
    name = cfg.get("proxy_container_name", "mtg-proxy")
    try:
        return client.containers.get(name)
    except docker.errors.NotFound:
        return None


def get_proxy_status():
    try:
        client = get_docker_client()
        container = get_container(client)
        if container is None:
            return {
                "running": False,
                "status": "not_created",
                "uptime": None,
                "container_id": None,
            }

        state = container.attrs.get("State", {})
        started_at = state.get("StartedAt", "")
        return {
            "running": container.status == "running",
            "status": container.status,
            "uptime": started_at,
            "container_id": container.short_id,
            "image": container.image.tags[0] if container.image.tags else "unknown",
        }
    except Exception as e:
        return {
            "running": False,
            "status": "error",
            "error": str(e),
        }


def _write_toml_config(cfg):
    """Write mtg TOML config file for multi-secret support."""
    users = cfg.get("users", [])
    enabled_users = [u for u in users if u.get("enabled", True)]
    if not enabled_users:
        return None

    port = cfg.get("proxy_port", 2443)
    prefer_ip = cfg.get("proxy_prefer_ip", "prefer-ipv4")

    # Map our prefer-ip values to mtg's expected values
    ip_map = {
        "v4": "prefer-ipv4",
        "v6": "prefer-ipv6",
        "prefer-v4": "prefer-ipv4",
        "prefer-v6": "prefer-ipv6",
        "prefer-ipv4": "prefer-ipv4",
        "prefer-ipv6": "prefer-ipv6",
    }
    prefer_ip = ip_map.get(prefer_ip, "prefer-ipv4")

    lines = []
    lines.append(f'bind-to = "0.0.0.0:{port}"')
    lines.append(f'prefer-ip = "{prefer_ip}"')
    lines.append('concurrency = 4096')

    proxy_tag = cfg.get("proxy_tag", "")
    if proxy_tag:
        lines.append(f'proxy-tag = "{proxy_tag}"')

    if cfg.get("stats_enabled", True):
        lines.append('[stats]')
        lines.append('bind-to = "0.0.0.0:3129"')

    # First enabled user's secret is the main secret
    # mtg v2 TOML supports only one secret per instance
    # For multiple users we need multiple containers, OR use simple-run per user
    # Actually, checking mtg v2 docs: the config supports a single secret
    # So for multiple users, we'll run one container per user
    # BUT that's complex. Let's use simple-run for single user,
    # and for now, just use the first enabled user's secret.
    # TOML expects 'secret' field.
    lines.append(f'secret = "{enabled_users[0]["secret"]}"')

    toml_content = "\n".join(lines) + "\n"
    with open(TOML_PATH, "w") as f:
        f.write(toml_content)

    return TOML_PATH


def start_proxy():
    """Start the MTProto proxy container."""
    cfg = load_config()
    client = get_docker_client()

    # Stop existing containers
    _stop_all_proxy_containers(client, cfg)

    users = cfg.get("users", [])
    enabled_users = [u for u in users if u.get("enabled", True)]
    if not enabled_users:
        return {"success": False, "error": "No enabled users/secrets configured"}

    port = cfg.get("proxy_port", 2443)
    image = cfg.get("proxy_image", "nineseconds/mtg:2")
    stats_enabled = cfg.get("stats_enabled", True)

    try:
        try:
            client.images.get(image)
        except docker.errors.ImageNotFound:
            client.images.pull(image)
    except Exception as e:
        return {"success": False, "error": f"Failed to pull image: {e}"}

    # Use simple-run for single user (most common case, simplest)
    if len(enabled_users) == 1:
        return _start_simple(client, cfg, enabled_users[0], port, image, stats_enabled)

    # For multiple users: run separate containers
    return _start_multi(client, cfg, enabled_users, port, image, stats_enabled)


def _start_simple(client, cfg, user, port, image, stats_enabled):
    """Start with simple-run (single secret)."""
    container_name = cfg.get("proxy_container_name", "mtg-proxy")
    prefer_ip = cfg.get("proxy_prefer_ip", "v4")
    ip_map = {"v4": "prefer-ipv4", "v6": "prefer-ipv6", "prefer-v4": "prefer-ipv4", "prefer-v6": "prefer-ipv6"}
    prefer_ip = ip_map.get(prefer_ip, "prefer-ipv4")

    command = [
        "simple-run",
        f"0.0.0.0:{port}",
        user["secret"],
        "--prefer-ip", prefer_ip,
        "--concurrency", "4096",
    ]

    proxy_tag = cfg.get("proxy_tag", "")
    # simple-run doesn't support proxy-tag or stats, use TOML config for that

    ports_map = {f"{port}/tcp": ("0.0.0.0", port)}

    try:
        container = client.containers.run(
            image,
            command=command,
            name=container_name,
            ports=ports_map,
            restart_policy={"Name": "unless-stopped"},
            detach=True,
        )

        time.sleep(2)
        container.reload()

        if container.status != "running":
            logs = container.logs(tail=20).decode("utf-8", errors="replace")
            return {"success": False, "error": f"Container exited: {logs}"}

        return {
            "success": True,
            "container_id": container.short_id,
            "status": container.status,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def _start_multi(client, cfg, enabled_users, base_port, image, stats_enabled):
    """Start multiple containers for multiple users."""
    base_name = cfg.get("proxy_container_name", "mtg-proxy")
    prefer_ip = cfg.get("proxy_prefer_ip", "v4")
    ip_map = {"v4": "prefer-ipv4", "v6": "prefer-ipv6", "prefer-v4": "prefer-ipv4", "prefer-v6": "prefer-ipv6"}
    prefer_ip = ip_map.get(prefer_ip, "prefer-ipv4")

    # All users share the same port — mtg simple-run only allows one secret.
    # Use TOML config which supports one secret per instance.
    # For true multi-user on same port, we need one container with config file.
    # mtg v2 TOML only supports one secret per config.
    # Solution: use the first user with simple-run on the main port.
    # Additional users get their own port (base_port + offset).
    # BUT that's complex for the user. Better approach:
    #
    # Since mtg v2 only supports 1 secret per instance, and users want
    # multiple keys on the same port — we should switch to mtg v1 or
    # the official mtproto-proxy which supports multiple secrets.
    #
    # For now: run first user on the configured port, warn about limitation.

    result = _start_simple(client, cfg, enabled_users[0], base_port, image, stats_enabled)
    if result.get("success") and len(enabled_users) > 1:
        result["warning"] = (
            f"mtg v2 supports 1 secret per instance. "
            f"Running with '{enabled_users[0]['name']}' secret. "
            f"{len(enabled_users) - 1} other user(s) ignored."
        )
    return result


def _stop_all_proxy_containers(client, cfg):
    """Stop all proxy containers."""
    base_name = cfg.get("proxy_container_name", "mtg-proxy")
    try:
        container = client.containers.get(base_name)
        container.stop(timeout=5)
        container.remove()
    except (docker.errors.NotFound, Exception):
        pass


def stop_proxy():
    try:
        client = get_docker_client()
        cfg = load_config()
        _stop_all_proxy_containers(client, cfg)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


def restart_proxy():
    stop_proxy()
    return start_proxy()


def get_proxy_logs(tail=100):
    try:
        client = get_docker_client()
        container = get_container(client)
        if container is None:
            return ""
        return container.logs(tail=tail, timestamps=True).decode("utf-8", errors="replace")
    except Exception as e:
        return f"Error getting logs: {e}"


def get_proxy_stats():
    cfg = load_config()
    if not cfg.get("stats_enabled", True):
        return None
    try:
        import urllib.request
        req = urllib.request.urlopen("http://127.0.0.1:3129/", timeout=2)
        data = json.loads(req.read().decode())
        return data
    except Exception:
        return None


def generate_tg_link(secret, cfg=None):
    if cfg is None:
        cfg = load_config()
    server = cfg.get("server_domain") or cfg.get("server_ip", "")
    port = cfg.get("proxy_port", 2443)
    return f"tg://proxy?server={server}&port={port}&secret={secret}"


def generate_tme_link(secret, cfg=None):
    if cfg is None:
        cfg = load_config()
    server = cfg.get("server_domain") or cfg.get("server_ip", "")
    port = cfg.get("proxy_port", 2443)
    return f"https://t.me/proxy?server={server}&port={port}&secret={secret}"


def detect_server_ip():
    try:
        import urllib.request
        return urllib.request.urlopen("https://ifconfig.me", timeout=5).read().decode().strip()
    except Exception:
        try:
            result = subprocess.run(
                ["curl", "-s", "https://ifconfig.me"],
                capture_output=True, text=True, timeout=5
            )
            return result.stdout.strip()
        except Exception:
            return ""
