import os
import hashlib
import secrets
import functools
import io
import datetime
import time
import threading

from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, jsonify, send_file, abort, g
)
import qrcode

from app.config import load_config, save_config, get_or_create_secret_key, next_available_port, DATA_DIR
from app.services.mtproto import (
    generate_secret, get_proxy_status, start_proxy, stop_proxy,
    restart_proxy, get_proxy_logs, get_proxy_stats,
    generate_tg_link, generate_tme_link, detect_server_ip
)
from app.services.traffic import (
    start_traffic_monitor, get_traffic_summary, reset_traffic_data
)

# Simple in-memory rate limiter for login
_login_attempts = {}  # ip -> (count, first_attempt_time)
_login_lock = threading.Lock()
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 300


def _check_rate_limit(ip):
    with _login_lock:
        now = time.time()
        if ip in _login_attempts:
            count, first_time = _login_attempts[ip]
            if now - first_time > LOGIN_WINDOW_SECONDS:
                _login_attempts[ip] = (1, now)
                return True
            if count >= LOGIN_MAX_ATTEMPTS:
                return False
            _login_attempts[ip] = (count + 1, first_time)
            return True
        _login_attempts[ip] = (1, now)
        return True


def _reset_rate_limit(ip):
    with _login_lock:
        _login_attempts.pop(ip, None)


def init_config():
    """One-time initialization with file locking."""
    import fcntl
    os.makedirs(DATA_DIR, exist_ok=True)
    lock_path = os.path.join(DATA_DIR, ".init.lock")
    with open(lock_path, "w") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            cfg = load_config()
            changed = False
            if not cfg.get("admin_password_hash"):
                default_password = secrets.token_urlsafe(12)
                cfg["admin_password_hash"] = hash_password(default_password)
                cfg["_initial_password"] = default_password
                changed = True
                print(f"\n{'='*50}")
                print(f"  INITIAL ADMIN CREDENTIALS")
                print(f"  Username: {cfg['admin_username']}")
                print(f"  Password: {default_password}")
                print(f"{'='*50}\n")
            if not cfg.get("server_ip"):
                cfg["server_ip"] = detect_server_ip()
                changed = True
            if changed:
                save_config(cfg)
        except IOError:
            pass


def create_app():
    app = Flask(
        __name__,
        template_folder=os.path.join(os.path.dirname(__file__), "templates"),
        static_folder=os.path.join(os.path.dirname(__file__), "static"),
    )
    app.secret_key = get_or_create_secret_key()
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["PERMANENT_SESSION_LIFETIME"] = datetime.timedelta(hours=12)

    init_config()
    start_traffic_monitor()

    # --- CSRF ---
    @app.before_request
    def csrf_protect():
        if request.method == "POST" and request.endpoint != "login":
            token = session.get("csrf_token")
            form_token = request.form.get("csrf_token")
            if not token or token != form_token:
                abort(403)

    @app.before_request
    def ensure_csrf_token():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_hex(32)

    @app.context_processor
    def inject_csrf():
        return {"csrf_token": session.get("csrf_token", "")}

    # --- Auth ---
    def login_required(f):
        @functools.wraps(f)
        def decorated(*args, **kwargs):
            if not session.get("logged_in"):
                return redirect(url_for("login"))
            return f(*args, **kwargs)
        return decorated

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            # Skip CSRF check for login (no session yet on first visit)
            client_ip = request.remote_addr
            if not _check_rate_limit(client_ip):
                flash("Too many login attempts. Try again later.", "error")
                return render_template("login.html"), 429

            cfg = load_config()
            username = request.form.get("username", "")
            password = request.form.get("password", "")
            if (
                username == cfg.get("admin_username", "admin")
                and verify_password(password, cfg.get("admin_password_hash", ""))
            ):
                _reset_rate_limit(client_ip)
                session["logged_in"] = True
                session["csrf_token"] = secrets.token_hex(32)
                session.permanent = True
                if cfg.get("_initial_password"):
                    del cfg["_initial_password"]
                    save_config(cfg)
                return redirect(url_for("dashboard"))
            flash("Invalid credentials", "error")
        return render_template("login.html")

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # --- Dashboard ---
    @app.route("/")
    @login_required
    def dashboard():
        cfg = load_config()
        status = get_proxy_status()
        stats = get_proxy_stats()
        traffic = get_traffic_summary()
        users = cfg.get("users", [])
        enabled_count = sum(1 for u in users if u.get("enabled", True))
        return render_template(
            "dashboard.html",
            status=status,
            stats=stats,
            traffic=traffic,
            cfg=cfg,
            user_count=len(users),
            enabled_count=enabled_count,
        )

    # --- Users/Secrets Management ---
    @app.route("/users")
    @login_required
    def users_list():
        cfg = load_config()
        users = cfg.get("users", [])
        for user in users:
            user["tg_link"] = generate_tg_link(user["secret"], cfg, port=user.get("port"))
            user["tme_link"] = generate_tme_link(user["secret"], cfg, port=user.get("port"))
        traffic = get_traffic_summary()
        traffic_per_user = {e["port"]: e for e in traffic.get("per_user", [])}
        return render_template("users.html", users=users, cfg=cfg, traffic_per_user=traffic_per_user)

    @app.route("/users/add", methods=["POST"])
    @login_required
    def user_add():
        cfg = load_config()
        name = request.form.get("name", "").strip()
        if not name:
            flash("Name is required", "error")
            return redirect(url_for("users_list"))

        fake_tls = cfg.get("fake_tls_domain", "google.com")
        secret = generate_secret(fake_tls)
        port = next_available_port(cfg)
        user = {
            "name": name,
            "secret": secret,
            "enabled": True,
            "port": port,
            "created_at": datetime.datetime.now().isoformat(),
        }
        cfg.setdefault("users", []).append(user)
        save_config(cfg)
        flash(f"User '{name}' added", "success")

        status = get_proxy_status()
        if status.get("running"):
            restart_proxy()
            flash("Proxy restarted to apply changes", "info")

        return redirect(url_for("users_list"))

    @app.route("/users/<int:idx>/toggle", methods=["POST"])
    @login_required
    def user_toggle(idx):
        cfg = load_config()
        users = cfg.get("users", [])
        if 0 <= idx < len(users):
            users[idx]["enabled"] = not users[idx].get("enabled", True)
            save_config(cfg)
            status = get_proxy_status()
            if status.get("running"):
                restart_proxy()
            state = "enabled" if users[idx]["enabled"] else "disabled"
            flash(f"User '{users[idx]['name']}' {state}", "success")
        return redirect(url_for("users_list"))

    @app.route("/users/<int:idx>/delete", methods=["POST"])
    @login_required
    def user_delete(idx):
        cfg = load_config()
        users = cfg.get("users", [])
        if 0 <= idx < len(users):
            name = users[idx]["name"]
            users.pop(idx)
            save_config(cfg)
            status = get_proxy_status()
            if status.get("running"):
                restart_proxy()
            flash(f"User '{name}' deleted", "success")
        return redirect(url_for("users_list"))

    @app.route("/users/<int:idx>/qr")
    @login_required
    def user_qr(idx):
        cfg = load_config()
        users = cfg.get("users", [])
        if 0 <= idx < len(users):
            link = generate_tme_link(users[idx]["secret"], cfg, port=users[idx].get("port"))
            img = qrcode.make(link, box_size=8, border=2)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
            return send_file(buf, mimetype="image/png")
        return "Not found", 404

    # --- Proxy Control ---
    @app.route("/proxy/start", methods=["POST"])
    @login_required
    def proxy_start():
        result = start_proxy()
        if result.get("success"):
            rc = result.get("running_count", 0)
            tc = result.get("total_count", 0)
            flash(f"Proxy started ({rc}/{tc} containers)", "success")
            if result.get("error"):
                flash(result["error"], "warning")
        else:
            flash(f"Failed to start: {result.get('error')}", "error")
        return redirect(url_for("dashboard"))

    @app.route("/proxy/stop", methods=["POST"])
    @login_required
    def proxy_stop():
        result = stop_proxy()
        if result.get("success"):
            flash("Proxy stopped", "success")
        else:
            flash(f"Failed to stop: {result.get('error')}", "error")
        return redirect(url_for("dashboard"))

    @app.route("/proxy/restart", methods=["POST"])
    @login_required
    def proxy_restart():
        result = restart_proxy()
        if result.get("success"):
            rc = result.get("running_count", 0)
            tc = result.get("total_count", 0)
            flash(f"Proxy restarted ({rc}/{tc} containers)", "success")
            if result.get("error"):
                flash(result["error"], "warning")
        else:
            flash(f"Failed to restart: {result.get('error')}", "error")
        return redirect(url_for("dashboard"))

    # --- Logs ---
    @app.route("/logs")
    @login_required
    def logs():
        log_text = get_proxy_logs(tail=200)
        return render_template("logs.html", logs=log_text)

    # --- Settings ---
    @app.route("/settings", methods=["GET", "POST"])
    @login_required
    def settings():
        cfg = load_config()
        if request.method == "POST":
            cfg["server_domain"] = request.form.get("server_domain", "").strip()
            cfg["server_ip"] = request.form.get("server_ip", "").strip()

            new_port = request.form.get("proxy_port", "2443")
            try:
                port_int = int(new_port)
                if not (1 <= port_int <= 65535):
                    raise ValueError
                cfg["proxy_port"] = port_int
            except ValueError:
                flash("Port must be between 1 and 65535", "error")
                return redirect(url_for("settings"))

            cfg["proxy_tag"] = request.form.get("proxy_tag", "").strip()
            cfg["fake_tls_domain"] = request.form.get("fake_tls_domain", "google.com").strip()
            cfg["proxy_buffer_size"] = request.form.get("proxy_buffer_size", "32KB").strip()
            cfg["proxy_prefer_ip"] = request.form.get("proxy_prefer_ip", "v4")
            cfg["stats_enabled"] = "stats_enabled" in request.form

            try:
                traffic_limit = float(request.form.get("traffic_limit_gb", "0"))
                cfg["traffic_limit_gb"] = max(traffic_limit, 0)
            except (ValueError, TypeError):
                pass

            try:
                throttle_speed = float(request.form.get("throttle_speed_mbps", "1"))
                cfg["throttle_speed_mbps"] = max(throttle_speed, 0.1)
            except (ValueError, TypeError):
                pass

            new_password = request.form.get("new_password", "").strip()
            if new_password:
                if len(new_password) < 6:
                    flash("Password must be at least 6 characters", "error")
                    return redirect(url_for("settings"))
                cfg["admin_password_hash"] = hash_password(new_password)
                flash("Password updated", "success")

            new_username = request.form.get("admin_username", "").strip()
            if new_username:
                cfg["admin_username"] = new_username

            save_config(cfg)
            flash("Settings saved", "success")
            return redirect(url_for("settings"))

        traffic = get_traffic_summary()
        return render_template("settings.html", cfg=cfg, traffic=traffic)

    # --- Traffic ---
    @app.route("/traffic/reset", methods=["POST"])
    @login_required
    def traffic_reset():
        reset_traffic_data()
        flash("Traffic counters reset", "success")
        return redirect(request.referrer or url_for("settings"))

    # --- API endpoints for AJAX ---
    @app.route("/api/status")
    @login_required
    def api_status():
        status = get_proxy_status()
        stats = get_proxy_stats()
        traffic = get_traffic_summary()
        return jsonify({"status": status, "stats": stats, "traffic": traffic})

    @app.route("/api/logs")
    @login_required
    def api_logs():
        log_text = get_proxy_logs(tail=50)
        return jsonify({"logs": log_text})

    return app


def hash_password(password):
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100000)
    return f"{salt}:{h.hex()}"


def verify_password(password, stored_hash):
    if not stored_hash or ":" not in stored_hash:
        return False
    salt, h = stored_hash.split(":", 1)
    computed = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100000)
    return secrets.compare_digest(computed.hex(), h)
