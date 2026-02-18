import docker
import secrets
import time
import json
import subprocess
from app.config import load_config


_docker_client = None


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


def _container_name_for_user(user):
    """Return container name based on user's port: mtg-proxy-{port}."""
    port = user.get("port", 2443)
    return f"mtg-proxy-{port}"


def get_container_for_user(client, user):
    """Find the container for a specific user by port-based name."""
    name = _container_name_for_user(user)
    try:
        return client.containers.get(name)
    except docker.errors.NotFound:
        return None


def get_all_proxy_containers(client):
    """List all mtg-proxy-* containers."""
    containers = []
    try:
        for c in client.containers.list(all=True):
            if c.name.startswith("mtg-proxy-"):
                containers.append(c)
    except Exception:
        pass
    return containers


def get_container(client=None):
    """Return the first running proxy container (backwards compat for traffic.py)."""
    if client is None:
        client = get_docker_client()
    for c in get_all_proxy_containers(client):
        if c.status == "running":
            return c
    # Legacy fallback: try old container name
    cfg = load_config()
    name = cfg.get("proxy_container_name", "mtg-proxy")
    try:
        return client.containers.get(name)
    except docker.errors.NotFound:
        return None


def get_proxy_status():
    """Return aggregate proxy status across all per-user containers."""
    try:
        client = get_docker_client()
        cfg = load_config()
        users = cfg.get("users", [])
        enabled_users = [u for u in users if u.get("enabled", True)]

        containers = get_all_proxy_containers(client)
        running = [c for c in containers if c.status == "running"]

        if not containers:
            return {
                "running": False,
                "status": "not_created",
                "uptime": None,
                "container_id": None,
                "running_count": 0,
                "total_count": len(enabled_users),
            }

        # Find earliest start time among running containers
        uptime = None
        container_id = None
        if running:
            earliest = None
            for c in running:
                started = c.attrs.get("State", {}).get("StartedAt", "")
                if started and (earliest is None or started < earliest):
                    earliest = started
                    container_id = c.short_id
            uptime = earliest

        return {
            "running": len(running) > 0,
            "status": "running" if running else containers[0].status,
            "uptime": uptime,
            "container_id": container_id,
            "running_count": len(running),
            "total_count": len(enabled_users),
        }
    except Exception as e:
        return {
            "running": False,
            "status": "error",
            "error": str(e),
            "running_count": 0,
            "total_count": 0,
        }


def _start_user_container(client, cfg, user, image):
    """Start a single container for one user on their assigned port."""
    port = user.get("port", cfg.get("proxy_port", 2443))
    container_name = _container_name_for_user(user)

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

    ports_map = {f"{port}/tcp": ("0.0.0.0", port)}

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
        return {"success": False, "user": user["name"], "port": port, "error": f"Container exited: {logs}"}

    return {"success": True, "user": user["name"], "port": port, "container_id": container.short_id}


def start_proxy():
    """Start one container per enabled user."""
    cfg = load_config()
    client = get_docker_client()

    # Stop all existing containers
    _stop_all_proxy_containers(client)

    users = cfg.get("users", [])
    enabled_users = [u for u in users if u.get("enabled", True)]
    if not enabled_users:
        return {"success": False, "error": "No enabled users/secrets configured"}

    image = cfg.get("proxy_image", "nineseconds/mtg:2")

    try:
        try:
            client.images.get(image)
        except docker.errors.ImageNotFound:
            client.images.pull(image)
    except Exception as e:
        return {"success": False, "error": f"Failed to pull image: {e}"}

    results = []
    all_ok = True
    for user in enabled_users:
        try:
            result = _start_user_container(client, cfg, user, image)
            results.append(result)
            if not result.get("success"):
                all_ok = False
        except Exception as e:
            results.append({"success": False, "user": user["name"], "error": str(e)})
            all_ok = False

    running_count = sum(1 for r in results if r.get("success"))
    return {
        "success": running_count > 0,
        "running_count": running_count,
        "total_count": len(enabled_users),
        "results": results,
        "error": None if all_ok else f"{len(enabled_users) - running_count} container(s) failed to start",
    }


def _stop_all_proxy_containers(client):
    """Stop legacy mtg-proxy and all mtg-proxy-* containers."""
    # Stop legacy container
    try:
        container = client.containers.get("mtg-proxy")
        container.stop(timeout=5)
        container.remove()
    except (docker.errors.NotFound, Exception):
        pass

    # Stop all per-user containers
    for c in get_all_proxy_containers(client):
        try:
            c.stop(timeout=5)
            c.remove()
        except Exception:
            pass


def stop_proxy():
    try:
        client = get_docker_client()
        _stop_all_proxy_containers(client)
        return {"success": True}
    except Exception as e:
        return {"success": False, "error": str(e)}


def restart_proxy():
    stop_proxy()
    return start_proxy()


def get_proxy_logs(tail=100):
    """Aggregate logs from all running proxy containers."""
    try:
        client = get_docker_client()
        containers = get_all_proxy_containers(client)
        if not containers:
            return ""

        all_logs = []
        for c in containers:
            try:
                prefix = f"[{c.name}] "
                lines = c.logs(tail=tail // max(len(containers), 1), timestamps=True).decode("utf-8", errors="replace")
                for line in lines.splitlines():
                    all_logs.append(prefix + line)
            except Exception:
                pass

        # Sort by timestamp (timestamps are at the start of each line after prefix)
        all_logs.sort(key=lambda x: x.split("] ", 1)[1] if "] " in x else x)
        return "\n".join(all_logs[-tail:])
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


def generate_tg_link(secret, cfg=None, port=None):
    if cfg is None:
        cfg = load_config()
    server = cfg.get("server_domain") or cfg.get("server_ip", "")
    if port is None:
        port = cfg.get("proxy_port", 2443)
    return f"tg://proxy?server={server}&port={port}&secret={secret}"


def generate_tme_link(secret, cfg=None, port=None):
    if cfg is None:
        cfg = load_config()
    server = cfg.get("server_domain") or cfg.get("server_ip", "")
    if port is None:
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
