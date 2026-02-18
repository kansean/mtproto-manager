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

DEFAULT_TRAFFIC = {
    "rx_bytes": 0,
    "tx_bytes": 0,
    "last_rx": 0,
    "last_tx": 0,
    "last_reset": "",
}


def load_traffic_data():
    with _lock:
        if os.path.exists(TRAFFIC_FILE):
            try:
                with open(TRAFFIC_FILE, "r") as f:
                    data = json.load(f)
                for key, val in DEFAULT_TRAFFIC.items():
                    if key not in data:
                        data[key] = val
                return data
            except (json.JSONDecodeError, OSError):
                pass
        return DEFAULT_TRAFFIC.copy()


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


def _get_container():
    """Get the proxy container via Docker API."""
    try:
        from app.services.mtproto import get_docker_client, get_container
        client = get_docker_client()
        return get_container(client)
    except Exception:
        return None


def _collect_stats_snapshot():
    """Collect Docker network stats and accumulate traffic delta."""
    container = _get_container()
    if container is None or container.status != "running":
        return

    try:
        stats = container.stats(stream=False)
    except Exception as e:
        logger.debug("Failed to get container stats: %s", e)
        return

    networks = stats.get("networks", {})
    eth0 = networks.get("eth0", {})
    if not eth0:
        # Some setups use different interface names; try first available
        for iface_data in networks.values():
            eth0 = iface_data
            break
    if not eth0:
        return

    current_rx = eth0.get("rx_bytes", 0)
    current_tx = eth0.get("tx_bytes", 0)

    data = load_traffic_data()

    last_rx = data.get("last_rx", 0)
    last_tx = data.get("last_tx", 0)

    # Calculate delta; if current < last, container restarted (counters reset)
    if current_rx >= last_rx:
        delta_rx = current_rx - last_rx
    else:
        delta_rx = current_rx  # restart: baseline = 0

    if current_tx >= last_tx:
        delta_tx = current_tx - last_tx
    else:
        delta_tx = current_tx

    data["rx_bytes"] = data.get("rx_bytes", 0) + delta_rx
    data["tx_bytes"] = data.get("tx_bytes", 0) + delta_tx
    data["last_rx"] = current_rx
    data["last_tx"] = current_tx

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
    """Apply tc rate limit inside the proxy container."""
    global _throttle_active
    container = _get_container()
    if container is None or container.status != "running":
        return

    rate = f"{speed_mbps}mbit"
    # burst = rate / 8 * 0.01 (10ms worth of data), minimum 1600 bytes
    burst_bytes = max(int(speed_mbps * 1000000 / 8 * 0.01), 1600)
    burst = f"{burst_bytes}"

    try:
        # Remove existing qdisc first (ignore errors if none exists)
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
            _throttle_active = True
            logger.info("Throttle applied: %s", rate)
        else:
            output = result.output if hasattr(result, 'output') else result[1]
            logger.warning("Failed to apply throttle (exit %s): %s", exit_code, output)
    except Exception as e:
        logger.warning("Failed to apply throttle: %s", e)


def _remove_throttle():
    """Remove tc rate limit from the proxy container."""
    global _throttle_active
    container = _get_container()
    if container is None or container.status != "running":
        _throttle_active = False
        return

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
    data = DEFAULT_TRAFFIC.copy()
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
