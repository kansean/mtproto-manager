import json
import os
import threading
import time
import tempfile
import datetime
import logging

from app.config import load_config, DATA_DIR

logger = logging.getLogger(__name__)

TRAFFIC_FILE = os.path.join(DATA_DIR, "traffic.json")

_lock = threading.Lock()
_monitor_thread = None
_throttle_active = False


def _default_traffic():
    return {
        "rx_bytes": 0,
        "tx_bytes": 0,
        "last_per_container": {},
        "last_reset": "",
    }


def load_traffic_data():
    with _lock:
        if os.path.exists(TRAFFIC_FILE):
            try:
                with open(TRAFFIC_FILE, "r") as f:
                    data = json.load(f)
                for key, val in _default_traffic().items():
                    if key not in data:
                        data[key] = val
                # Migrate old single-container fields
                if "last_rx" in data or "last_tx" in data:
                    data.pop("last_rx", None)
                    data.pop("last_tx", None)
                return data
            except (json.JSONDecodeError, OSError):
                pass
        return _default_traffic()


def save_traffic_data(data):
    with _lock:
        os.makedirs(DATA_DIR, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=DATA_DIR, suffix=".json.tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, TRAFFIC_FILE)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


def _get_containers():
    """Get all running proxy containers."""
    try:
        from app.services.mtproto import get_docker_client, get_all_proxy_containers
        client = get_docker_client()
        return [c for c in get_all_proxy_containers(client) if c.status == "running"]
    except Exception:
        return []


def _collect_stats_snapshot():
    """Collect Docker network stats from all proxy containers and accumulate traffic delta."""
    containers = _get_containers()
    if not containers:
        return

    data = load_traffic_data()
    last_per_container = data.get("last_per_container", {})

    for container in containers:
        try:
            stats = container.stats(stream=False)
        except Exception as e:
            logger.debug("Failed to get stats for %s: %s", container.name, e)
            continue

        networks = stats.get("networks", {})
        eth0 = networks.get("eth0", {})
        if not eth0:
            for iface_data in networks.values():
                eth0 = iface_data
                break
        if not eth0:
            continue

        current_rx = eth0.get("rx_bytes", 0)
        current_tx = eth0.get("tx_bytes", 0)

        cname = container.name
        prev = last_per_container.get(cname, {"rx": 0, "tx": 0})
        last_rx = prev.get("rx", 0)
        last_tx = prev.get("tx", 0)

        # Calculate delta; if current < last, container restarted (counters reset)
        delta_rx = current_rx - last_rx if current_rx >= last_rx else current_rx
        delta_tx = current_tx - last_tx if current_tx >= last_tx else current_tx

        data["rx_bytes"] = data.get("rx_bytes", 0) + delta_rx
        data["tx_bytes"] = data.get("tx_bytes", 0) + delta_tx

        last_per_container[cname] = {"rx": current_rx, "tx": current_tx}

    data["last_per_container"] = last_per_container
    save_traffic_data(data)


def _check_and_enforce_limits():
    """Check if traffic limit exceeded and apply/remove throttle."""
    global _throttle_active

    cfg = load_config()
    limit_gb = cfg.get("traffic_limit_gb", 0)
    if not limit_gb or limit_gb <= 0:
        if _throttle_active:
            _remove_throttle()
        return

    data = load_traffic_data()
    total_bytes = data.get("rx_bytes", 0) + data.get("tx_bytes", 0)
    limit_bytes = limit_gb * (1024 ** 3)

    if total_bytes >= limit_bytes:
        if not _throttle_active:
            speed_mbps = cfg.get("throttle_speed_mbps", 1)
            _apply_throttle(speed_mbps)
    else:
        if _throttle_active:
            _remove_throttle()


def _apply_throttle(speed_mbps):
    """Apply tc rate limit inside all proxy containers."""
    global _throttle_active
    containers = _get_containers()
    if not containers:
        return

    rate = f"{speed_mbps}mbit"
    burst_bytes = max(int(speed_mbps * 1000000 / 8 * 0.01), 1600)
    burst = f"{burst_bytes}"

    applied = False
    for container in containers:
        try:
            container.exec_run("tc qdisc del dev eth0 root", demux=True)
        except Exception:
            pass
        try:
            result = container.exec_run(
                f"tc qdisc add dev eth0 root tbf rate {rate} burst {burst} latency 50ms",
                demux=True,
            )
            exit_code = result.exit_code if hasattr(result, 'exit_code') else result[0]
            if exit_code == 0:
                applied = True
                logger.info("Throttle applied to %s: %s", container.name, rate)
            else:
                output = result.output if hasattr(result, 'output') else result[1]
                logger.warning("Failed to apply throttle to %s (exit %s): %s", container.name, exit_code, output)
        except Exception as e:
            logger.warning("Failed to apply throttle to %s: %s", container.name, e)

    if applied:
        _throttle_active = True


def _remove_throttle():
    """Remove tc rate limit from all proxy containers."""
    global _throttle_active
    containers = _get_containers()
    for container in containers:
        try:
            container.exec_run("tc qdisc del dev eth0 root", demux=True)
        except Exception:
            pass
    _throttle_active = False
    logger.info("Throttle removed")


def get_traffic_summary():
    """Return traffic summary for dashboard/API."""
    data = load_traffic_data()
    cfg = load_config()
    limit_gb = cfg.get("traffic_limit_gb", 0)

    total_bytes = data.get("rx_bytes", 0) + data.get("tx_bytes", 0)
    if limit_gb and limit_gb > 0:
        limit_bytes = limit_gb * (1024 ** 3)
        limit_used_pct = min(round(total_bytes / limit_bytes * 100, 1), 100)
    else:
        limit_used_pct = 0

    return {
        "rx_bytes": data.get("rx_bytes", 0),
        "tx_bytes": data.get("tx_bytes", 0),
        "limit_gb": limit_gb,
        "limit_used_pct": limit_used_pct,
        "throttle_active": _throttle_active,
        "last_reset": data.get("last_reset", ""),
    }


def reset_traffic_data():
    """Reset traffic counters and remove throttle."""
    data = _default_traffic()
    data["last_reset"] = datetime.datetime.now().isoformat()
    save_traffic_data(data)
    if _throttle_active:
        _remove_throttle()


def _monitor_loop(interval):
    """Background loop: collect stats and enforce limits."""
    while True:
        try:
            _collect_stats_snapshot()
            _check_and_enforce_limits()
        except Exception as e:
            logger.debug("Traffic monitor error: %s", e)
        time.sleep(interval)


def start_traffic_monitor(interval=10):
    """Start the background traffic monitoring thread."""
    global _monitor_thread
    if _monitor_thread is not None and _monitor_thread.is_alive():
        return
    _monitor_thread = threading.Thread(
        target=_monitor_loop,
        args=(interval,),
        daemon=True,
        name="traffic-monitor",
    )
    _monitor_thread.start()
    logger.info("Traffic monitor started (interval=%ds)", interval)
