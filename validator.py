import hmac
import os
import secrets
import time
import json
import socket
import sqlite3
import hashlib
import ipaddress
import shutil
import subprocess
import tempfile
import threading
import http.client
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from urllib.error import HTTPError, URLError
from pathlib import Path
from datetime import datetime
from urllib.parse import parse_qs, quote, urlsplit

STATE = Path(os.getenv("STATE_DIR", "/state")).expanduser()
CONFIG_FILE = STATE / "config.json"
CONFIG_LOCK = threading.RLock()
SESSION_COOKIE = "media_manager_session"
SESSION_TTL_SECONDS = 12 * 60 * 60
SESSION_LOCK = threading.RLock()
SESSIONS = {}


def default_settings():
    return {
        "input_path": os.getenv("INPUT", "/data/Downloads/Complete"),
        "quarantine_path": os.getenv("QUARANTINE", "/data/Quarantine"),
        "clamav_host": os.getenv("CLAMAV_HOST", "clamav"),
        "clamav_port": os.getenv("CLAMAV_PORT", "3310"),
        "clamav_scan_root": os.getenv("CLAMAV_SCAN_ROOT", "/scan"),
        "radarr_url": os.getenv("RADARR_URL", ""),
        "radarr_api_key": os.getenv("RADARR_API_KEY", ""),
        "sonarr_url": os.getenv("SONARR_URL", ""),
        "sonarr_api_key": os.getenv("SONARR_API_KEY", ""),
        "radarr_import_root": os.getenv(
            "RADARR_IMPORT_ROOT",
            "/data/Quarantine/Pending/Movies"
        ),
        "sonarr_import_root": os.getenv(
            "SONARR_IMPORT_ROOT",
            "/data/Quarantine/Pending/TV"
        ),
        "scan_interval": os.getenv("SCAN_INTERVAL", "60"),
        "settle_seconds": os.getenv("SETTLE_SECONDS", "120"),
        "import_retry_seconds": os.getenv("IMPORT_RETRY_SECONDS", "300"),
        "vpn_enabled": False,
        "vpn_check_container": "",
        "vpn_shutdown_containers": [],
        "vpn_check_interval": "5",
        "vpn_failure_threshold": "3",
    }


def normalize_settings(raw):
    settings = default_settings()
    settings.update({key: value for key, value in raw.items() if key in settings})

    string_fields = (
        "input_path", "quarantine_path", "clamav_host", "clamav_scan_root",
        "radarr_url", "radarr_api_key", "sonarr_url", "sonarr_api_key",
        "radarr_import_root", "sonarr_import_root",
    )
    for field in string_fields:
        value = settings[field]
        if not isinstance(value, str):
            raise ValueError(f"{field.replace('_', ' ').capitalize()} must be text.")
        settings[field] = value.strip()

    if not isinstance(settings["vpn_enabled"], bool):
        raise ValueError("VPN Guard enabled must be true or false.")
    for field in ("vpn_check_container",):
        value = settings[field]
        if not isinstance(value, str):
            raise ValueError(f"{field.replace('_', ' ').capitalize()} must be text.")
        settings[field] = value.strip()
    shutdown_containers = settings["vpn_shutdown_containers"]
    if not isinstance(shutdown_containers, list) or len(shutdown_containers) > 50:
        raise ValueError("Choose no more than 50 containers for VPN Guard to stop.")
    normalized_containers = []
    for name in shutdown_containers:
        if not isinstance(name, str):
            raise ValueError("VPN Guard container names must be text.")
        name = name.strip()
        if not name or len(name) > 128 or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-" for c in name):
            raise ValueError("A selected VPN Guard container name is invalid.")
        if name not in normalized_containers:
            normalized_containers.append(name)
    settings["vpn_shutdown_containers"] = normalized_containers

    for field in ("input_path", "quarantine_path", "radarr_import_root", "sonarr_import_root", "clamav_scan_root"):
        if not Path(settings[field]).is_absolute():
            raise ValueError(f"{field.replace('_', ' ').capitalize()} must be an absolute container path.")

    if not settings["clamav_host"] or any(c.isspace() for c in settings["clamav_host"]):
        raise ValueError("ClamAV host is required and cannot contain spaces.")

    try:
        settings["clamav_port"] = int(settings["clamav_port"])
    except (TypeError, ValueError):
        raise ValueError("ClamAV port must be a number.")
    if not 1 <= settings["clamav_port"] <= 65535:
        raise ValueError("ClamAV port must be between 1 and 65535.")

    for field, minimum, maximum in (
        ("scan_interval", 1, 3600),
        ("settle_seconds", 0, 86400),
        ("import_retry_seconds", 1, 86400),
        ("vpn_check_interval", 5, 120),
        ("vpn_failure_threshold", 1, 10),
    ):
        try:
            settings[field] = int(settings[field])
        except (TypeError, ValueError):
            raise ValueError(f"{field.replace('_', ' ').capitalize()} must be a number.")
        if not minimum <= settings[field] <= maximum:
            raise ValueError(f"{field.replace('_', ' ').capitalize()} is outside the allowed range.")

    for field, label in (("radarr_url", "Radarr"), ("sonarr_url", "Sonarr")):
        value = settings[field].rstrip("/")
        if value:
            parsed = urlsplit(value)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                raise ValueError(f"{label} URL must start with http:// or https:// and include a host.")
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError(f"{label} URL cannot contain credentials, query parameters, or a fragment.")
        settings[field] = value

    if settings["vpn_enabled"]:
        if not settings["vpn_check_container"]:
            raise ValueError("Choose the container whose VPN connection should be checked.")
        if not settings["vpn_shutdown_containers"]:
            raise ValueError("Choose at least one container to stop if the VPN check fails.")
    return settings


def load_settings():
    raw = {}
    try:
        raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        pass
    except (OSError, json.JSONDecodeError):
        raise RuntimeError("Stored configuration could not be read. Check /state/config.json.")
    if not isinstance(raw, dict):
        raise RuntimeError("Stored configuration must be a JSON object.")
    return normalize_settings(raw)


def apply_settings(settings):
    global CONFIG, INPUT, QUARANTINE, CLAMAV_HOST, CLAMAV_PORT
    global CLAMAV_SCAN_ROOT, RADARR_URL, RADARR_API_KEY, SONARR_URL
    global SONARR_API_KEY, RADARR_IMPORT_ROOT, SONARR_IMPORT_ROOT
    global SCAN_INTERVAL, SETTLE_SECONDS, IMPORT_RETRY_SECONDS

    with CONFIG_LOCK:
        CONFIG = dict(settings)
        INPUT = Path(CONFIG["input_path"]).expanduser()
        QUARANTINE = Path(CONFIG["quarantine_path"]).expanduser()
        CLAMAV_HOST = CONFIG["clamav_host"]
        CLAMAV_PORT = CONFIG["clamav_port"]
        CLAMAV_SCAN_ROOT = CONFIG["clamav_scan_root"].rstrip("/")
        RADARR_URL = CONFIG["radarr_url"]
        RADARR_API_KEY = CONFIG["radarr_api_key"]
        SONARR_URL = CONFIG["sonarr_url"]
        SONARR_API_KEY = CONFIG["sonarr_api_key"]
        RADARR_IMPORT_ROOT = CONFIG["radarr_import_root"].rstrip("/")
        SONARR_IMPORT_ROOT = CONFIG["sonarr_import_root"].rstrip("/")
        SCAN_INTERVAL = CONFIG["scan_interval"]
        SETTLE_SECONDS = CONFIG["settle_seconds"]
        IMPORT_RETRY_SECONDS = CONFIG["import_retry_seconds"]


STATE.mkdir(parents=True, exist_ok=True)
apply_settings(load_settings())

VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".m4v", ".avi", ".mov", ".webm",
    ".ts", ".m2ts", ".mpg", ".mpeg", ".wmv"
}

RISKY_EXTENSIONS = {
    ".exe", ".com", ".scr", ".bat", ".cmd", ".ps1",
    ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh",
    ".msi", ".dll", ".lnk", ".jar", ".apk",
    ".dmg", ".pkg", ".sh", ".py",
    ".zip", ".rar", ".7z", ".iso"
}

JUNK_VIDEO_WORDS = {
    "sample", "trailer", "featurette",
    "behindthescenes", "behind-the-scenes", "extras"
}

DB_LOCK = threading.RLock()
db = sqlite3.connect(STATE / "validator.db", check_same_thread=False)

db.execute("""
CREATE TABLE IF NOT EXISTS jobs (
    signature TEXT PRIMARY KEY,
    job TEXT,
    result TEXT,
    timestamp TEXT
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS imports (
    signature TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    job TEXT NOT NULL,
    destination TEXT NOT NULL,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_attempt REAL NOT NULL DEFAULT 0
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS activity_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    level TEXT NOT NULL,
    message TEXT NOT NULL
)
""")

db.execute("""
CREATE INDEX IF NOT EXISTS activity_events_timestamp
ON activity_events(timestamp)
""")

db.commit()


SECRET_FIELDS = ("radarr_api_key", "sonarr_api_key")


def public_settings():
    with CONFIG_LOCK:
        result = {key: value for key, value in CONFIG.items() if key not in SECRET_FIELDS}
        result["radarr_api_key_set"] = bool(CONFIG["radarr_api_key"])
        result["sonarr_api_key_set"] = bool(CONFIG["sonarr_api_key"])
        return result


DOCKER_SOCKET_PATH = os.getenv("DOCKER_SOCKET", "/var/run/docker.sock")
VPN_STATUS_LOCK = threading.RLock()
VPN_CHECK_LOCK = threading.Lock()
VPN_STATUS = {
    "state": "disabled",
    "enabled": False,
    "last_check": "",
    "check_ip": "",
    "container_ip": "",
    "consecutive_failures": 0,
    "tripped": False,
    "last_message": "VPN Guard is turned off.",
    "stopped_containers": [],
}


class UnixSocketHTTPConnection(http.client.HTTPConnection):
    def __init__(self, socket_path, timeout=5):
        super().__init__("localhost", timeout=timeout)
        self.socket_path = socket_path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self.socket_path)
        self.sock = sock


def docker_request(method, path, payload=None, timeout=5):
    connection = UnixSocketHTTPConnection(DOCKER_SOCKET_PATH, timeout=timeout)
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"} if body is not None else {}
    try:
        connection.request(method, "/v1.41" + path, body=body, headers=headers)
        response = connection.getresponse()
        response_body = response.read(1024 * 1024)
        if not 200 <= response.status < 300:
            detail = response_body.decode("utf-8", "replace")[:300]
            raise RuntimeError(f"Docker returned HTTP {response.status}: {detail}")
        return response_body
    finally:
        connection.close()


def is_media_manager_container(item, container_id):
    current_id = socket.gethostname().lower()
    labels = item.get("Labels") or {}
    image = str(item.get("Image", "")).lower()
    has_app_label = labels.get("org.opencontainers.image.title") == "Media Manager"
    is_manager_image = "media-manager" in image
    return container_id.startswith(current_id) or (has_app_label and is_manager_image)


def docker_container_records():
    body = docker_request("GET", "/containers/json?all=1")
    containers = json.loads(body.decode("utf-8"))
    result = []
    for item in containers:
        container_id = str(item.get("Id", ""))
        names = item.get("Names") or []
        name = str(names[0] if names else "").lstrip("/")
        if not name or is_media_manager_container(item, container_id):
            continue
        result.append({
            "id": container_id,
            "name": name,
            "running": str(item.get("State", "unknown")) == "running",
            "state": str(item.get("State", "unknown")),
            "status": str(item.get("Status", ""))[:100],
        })
    return sorted(result, key=lambda item: item["name"].lower())


def public_ip():
    request = urllib.request.Request(
        "https://api.ipify.org",
        headers={"User-Agent": "MediaManager-VPNGuard/1.0"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        value = response.read(128).decode("ascii", "ignore").strip()
    return str(ipaddress.ip_address(value))


def container_public_ip(container_id):
    encoded_id = quote(container_id, safe="")
    command = [
        "sh", "-c",
        "curl -fsS --max-time 5 https://api.ipify.org",
    ]
    create = docker_request(
        "POST",
        f"/containers/{encoded_id}/exec",
        {"AttachStdout": True, "AttachStderr": True, "Tty": False, "Cmd": command},
    )
    exec_id = json.loads(create.decode("utf-8"))["Id"]
    encoded_exec_id = quote(exec_id, safe="")
    output = docker_request(
        "POST",
        f"/exec/{encoded_exec_id}/start",
        {"Detach": False, "Tty": False},
        timeout=8,
    )
    exit_info = docker_request("GET", f"/exec/{encoded_exec_id}/json")
    exit_code = json.loads(exit_info.decode("utf-8")).get("ExitCode")
    stdout = bytearray()
    if output and len(output) >= 8 and output[0] in (1, 2):
        cursor = 0
        while cursor + 8 <= len(output):
            size = int.from_bytes(output[cursor + 4:cursor + 8], "big")
            cursor += 8
            chunk = output[cursor:cursor + size]
            cursor += size
            stdout.extend(chunk)
    else:
        stdout.extend(output)
    value = stdout.decode("ascii", "ignore").strip()
    if exit_code != 0 or not value:
        raise RuntimeError("The selected container could not check its public IP. Confirm it is running and has curl installed.")
    return str(ipaddress.ip_address(value))


def masked_ip(value):
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return ""
    if address.version == 4:
        parts = str(address).split(".")
        return ".".join(parts[:3]) + ".×"
    return ":".join(address.exploded.split(":")[:4]) + ":…"


def read_vpn_status():
    with VPN_STATUS_LOCK:
        return dict(VPN_STATUS)


def set_vpn_status(**updates):
    with VPN_STATUS_LOCK:
        VPN_STATUS.update(updates)
        return dict(VPN_STATUS)


def stop_container(container_id, grace_seconds=10):
    encoded_id = quote(container_id, safe="")
    docker_request("POST", f"/containers/{encoded_id}/stop?t={int(grace_seconds)}", timeout=8)


def record_vpn_failure(settings, containers, timestamp, failure, check_address="", container_address="", trigger=True):
    if not trigger:
        return set_vpn_status(
            state="warning", enabled=settings["vpn_enabled"], last_check=timestamp,
            check_ip=masked_ip(check_address), container_ip=masked_ip(container_address),
            last_message=failure,
        )

    failures = int(read_vpn_status().get("consecutive_failures", 0)) + 1
    set_vpn_status(
        state="warning", enabled=settings["vpn_enabled"], last_check=timestamp,
        check_ip=masked_ip(check_address), container_ip=masked_ip(container_address),
        consecutive_failures=failures, last_message=failure,
    )
    if failures < settings["vpn_failure_threshold"]:
        log(f"VPN GUARD: Check failed ({failures}/{settings['vpn_failure_threshold']}); confirming before stopping containers.")
        return read_vpn_status()

    selected = set(settings["vpn_shutdown_containers"])
    stopped = []
    stop_errors = []
    for item in containers:
        if item["name"] not in selected or not item["running"]:
            continue
        try:
            stop_container(item["id"])
            stopped.append(item["name"])
        except Exception as exc:
            stop_errors.append(f"{item['name']}: {str(exc)[:160]}")
    message = f"VPN Guard triggered: {failure}"
    if stopped:
        message += " Stopped: " + ", ".join(stopped) + "."
    if stop_errors:
        message += " Stop errors: " + "; ".join(stop_errors)
    set_vpn_status(
        state="tripped", enabled=settings["vpn_enabled"], tripped=True,
        stopped_containers=stopped, last_message=message,
    )
    log("ERROR " + message)
    return read_vpn_status()


def run_vpn_check(trigger=True):
    with VPN_CHECK_LOCK:
        with CONFIG_LOCK:
            settings = dict(CONFIG)
            settings["vpn_shutdown_containers"] = list(CONFIG["vpn_shutdown_containers"])
        if read_vpn_status().get("tripped"):
            return read_vpn_status()
        if trigger and not settings["vpn_enabled"]:
            return set_vpn_status(
                state="disabled", enabled=False, last_message="VPN Guard is turned off."
            )

        set_vpn_status(state="checking", enabled=settings["vpn_enabled"])
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        try:
            containers = docker_container_records()
            monitor = next(
                (item for item in containers if item["name"] == settings["vpn_check_container"]),
                None,
            )
            if not monitor:
                return set_vpn_status(
                    state="unavailable", enabled=settings["vpn_enabled"],
                    last_check=timestamp, last_message="The selected VPN check container is unavailable.",
                    check_ip="", container_ip="",
                )
            if not monitor["running"]:
                return set_vpn_status(
                    state="waiting", enabled=settings["vpn_enabled"],
                    last_check=timestamp,
                    last_message=f"Waiting for {monitor['name']} to start.",
                    check_ip="", container_ip="", consecutive_failures=0,
                )

            check_address = ""
            container_address = ""
            check_error = ""
            try:
                check_address = public_ip()
            except Exception:
                check_error = "Media Manager could not determine its public IP."
            try:
                container_address = container_public_ip(monitor["id"])
            except Exception:
                check_error = "The monitored container could not reach the public IP check service."

            if check_error:
                return record_vpn_failure(
                    settings, containers, timestamp, check_error,
                    check_address, container_address, trigger,
                )
            if check_address == container_address:
                failure = "The monitored container is using the same public IP as Media Manager."
                check_ok = False
            else:
                failure = "The monitored container public IP could not be verified as different."
                check_ok = bool(check_address and container_address)
            if check_ok:
                previous_status = read_vpn_status()
                previous_failures = previous_status.get("consecutive_failures", 0)
                success_state = "protected" if settings["vpn_enabled"] else "disabled"
                success_message = f"{monitor['name']} is using a different public IP."
                if not settings["vpn_enabled"]:
                    success_message += " VPN Guard is off."
                set_vpn_status(
                    state=success_state, enabled=settings["vpn_enabled"], last_check=timestamp,
                    check_ip=masked_ip(check_address), container_ip=masked_ip(container_address),
                    consecutive_failures=0,
                    last_message=success_message,
                )
                if previous_failures:
                    log(f"VPN GUARD: Connection check recovered for {monitor['name']}.")
                elif previous_status.get("state") != "protected":
                    log(f"VPN GUARD: Connection verified for {monitor['name']}.")
                return read_vpn_status()

            return record_vpn_failure(
                settings, containers, timestamp, failure,
                check_address, container_address, trigger,
            )
        except Exception as exc:
            message = f"VPN Guard check unavailable: {str(exc)[:240]}"
            previous_status = read_vpn_status()
            set_vpn_status(
                state="unavailable", enabled=settings["vpn_enabled"],
                last_check=timestamp, last_message=message,
            )
            if previous_status.get("state") != "unavailable" or previous_status.get("last_message") != message:
                log("VPN GUARD: " + message)
            return read_vpn_status()


def vpn_guard_worker():
    while True:
        with CONFIG_LOCK:
            enabled = CONFIG["vpn_enabled"]
            interval = CONFIG["vpn_check_interval"]
        if enabled and not read_vpn_status().get("tripped"):
            run_vpn_check(trigger=True)
        elif not enabled:
            set_vpn_status(state="disabled", enabled=False, last_message="VPN Guard is turned off.")
        time.sleep(interval if enabled else 2)


def request_settings(payload, allow_clear=False):
    if not isinstance(payload, dict):
        raise ValueError("Settings must be sent as a JSON object.")

    with CONFIG_LOCK:
        candidate = dict(CONFIG)

    for key in candidate:
        if key in SECRET_FIELDS:
            if key not in payload:
                continue
            value = payload[key]
            if not isinstance(value, str):
                raise ValueError("API keys must be text.")
            clear_flag = f"clear_{key}"
            if allow_clear and payload.get(clear_flag):
                candidate[key] = ""
            elif value.strip():
                candidate[key] = value.strip()
            continue

        if key in payload:
            candidate[key] = payload[key]

    return normalize_settings(candidate)


def persist_settings(payload):
    candidate = request_settings(payload, allow_clear=True)
    STATE.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=STATE)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(candidate, handle, indent=2)
            handle.write("\n")
        os.replace(temp_name, CONFIG_FILE)
        os.chmod(CONFIG_FILE, 0o600)
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise

    apply_settings(candidate)
    return public_settings()


def test_arr_connection(app_name, candidate):
    prefix = app_name.lower()
    base_url = candidate[f"{prefix}_url"]
    api_key = candidate[f"{prefix}_api_key"]
    if not base_url or not api_key:
        return {"ok": False, "message": f"Enter the {app_name} URL and API key first."}

    request = urllib.request.Request(
        f"{base_url}/api/v3/system/status",
        headers={"X-Api-Key": api_key, "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=6) as response:
            if 200 <= response.status < 300:
                return {"ok": True, "message": f"Connected to {app_name}."}
            return {"ok": False, "message": f"{app_name} returned HTTP {response.status}."}
    except HTTPError as exc:
        if exc.code in (401, 403):
            return {"ok": False, "message": f"{app_name} rejected the API key."}
        return {"ok": False, "message": f"{app_name} returned HTTP {exc.code}."}
    except (URLError, TimeoutError, socket.timeout, OSError):
        return {"ok": False, "message": f"Could not reach {app_name}. Check the URL and network."}


def test_clamav_connection(candidate):
    try:
        with socket.create_connection(
            (candidate["clamav_host"], candidate["clamav_port"]),
            timeout=4,
        ) as sock:
            sock.sendall(b"PING\0")
            response = sock.recv(64).split(b"\0", 1)[0].decode("utf-8", "replace")
        if response == "PONG":
            return {"ok": True, "message": "ClamAV is responding."}
        return {"ok": False, "message": "ClamAV replied unexpectedly. Check the configured endpoint."}
    except (OSError, TimeoutError):
        return {"ok": False, "message": "Could not reach ClamAV. Check the host, port, and network."}


def test_paths(candidate):
    input_path = Path(candidate["input_path"])
    quarantine_path = Path(candidate["quarantine_path"])
    input_ok = input_path.is_dir() and os.access(input_path, os.R_OK | os.X_OK)
    write_target = quarantine_path
    while not write_target.exists() and write_target.parent != write_target:
        write_target = write_target.parent
    quarantine_ok = write_target.is_dir() and os.access(write_target, os.W_OK | os.X_OK)
    details = [
        {"ok": input_ok, "message": "Completed downloads folder is readable." if input_ok else "Completed downloads folder is missing or unreadable."},
        {"ok": quarantine_ok, "message": "Quarantine location is writable." if quarantine_ok else "Quarantine location or its parent is not writable."},
    ]
    return {"ok": all(item["ok"] for item in details), "details": details}


WEB_ROOT = Path(__file__).parent / "web"
ICON_FILE = Path(__file__).parent / "icon.svg"


def create_session():
    now = time.time()
    token = secrets.token_urlsafe(32)
    with SESSION_LOCK:
        expired = [key for key, expires in SESSIONS.items() if expires <= now]
        for key in expired:
            SESSIONS.pop(key, None)
        SESSIONS[token] = now + SESSION_TTL_SECONDS
    return token


def session_is_valid(token):
    if not token:
        return False
    now = time.time()
    with SESSION_LOCK:
        expires = SESSIONS.get(token)
        if expires is None:
            return False
        if expires <= now:
            SESSIONS.pop(token, None)
            return False
        return True


class SetupHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Do not log setup request bodies or connection-test details.
        return

    def send_content(self, status, body, content_type, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self'; object-src 'none'; "
            "base-uri 'self'; form-action 'self'; frame-ancestors 'none'",
        )
        for name, value in extra_headers or []:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, data, extra_headers=None):
        self.send_content(
            status,
            json.dumps(data).encode("utf-8"),
            "application/json; charset=utf-8",
            extra_headers,
        )

    def session_token(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return ""
        morsel = cookie.get(SESSION_COOKIE)
        return morsel.value if morsel else ""

    def authenticated(self):
        return bool(os.getenv("WEB_PASSWORD", "")) and session_is_valid(self.session_token())

    def require_authentication(self):
        if not os.getenv("WEB_PASSWORD", ""):
            self.send_json(503, {
                "ok": False,
                "message": "Set WEB_PASSWORD in the container settings to enable the setup page.",
            })
            return False
        if not self.authenticated():
            self.send_json(401, {"ok": False, "message": "Please sign in to continue."})
            return False
        return True

    def send_asset(self, filename, content_type, icon=False):
        try:
            body = (ICON_FILE if icon else WEB_ROOT / filename).read_bytes()
        except OSError:
            self.send_json(500, {"ok": False, "message": "Setup page files are missing."})
            return
        self.send_content(200, body, content_type)

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Invalid request body length.")
        if length < 0 or length > 65536:
            raise ValueError("Request body is too large.")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("Request body must be valid JSON.")

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/api/session":
            configured = bool(os.getenv("WEB_PASSWORD", ""))
            self.send_json(200, {
                "ok": True,
                "configured": configured,
                "authenticated": configured and self.authenticated(),
                "message": "" if configured else "Set WEB_PASSWORD in the Unraid container settings to enable sign-in.",
            })
            return

        public_assets = {
            "/login.css": ("login.css", "text/css; charset=utf-8"),
            "/login.js": ("login.js", "text/javascript; charset=utf-8"),
            "/icon.svg": (None, "image/svg+xml"),
        }
        if path in public_assets:
            filename, content_type = public_assets[path]
            self.send_asset(filename, content_type, icon=path == "/icon.svg")
            return

        if path in ("/", "/index.html"):
            filename = "index.html" if self.authenticated() else "login.html"
            content_type = "text/html; charset=utf-8"
            self.send_asset(filename, content_type)
            return

        if not self.require_authentication():
            return

        if path == "/api/config":
            self.send_json(200, {"ok": True, "settings": public_settings()})
            return
        if path == "/api/health":
            self.send_json(200, {"ok": True})
            return
        if path == "/api/vpn":
            try:
                containers = docker_container_records()
                self.send_json(200, {
                    "ok": True,
                    "docker_available": True,
                    "containers": [
                        {key: value for key, value in item.items() if key != "id"}
                        for item in containers
                    ],
                    "status": read_vpn_status(),
                })
            except Exception:
                self.send_json(200, {
                    "ok": True,
                    "docker_available": False,
                    "containers": [],
                    "status": read_vpn_status(),
                    "message": "In the Unraid container settings, set the Docker socket host path to /var/run/docker.sock.",
                })
            return
        if path == "/api/vpn/status":
            self.send_json(200, {"ok": True, "status": read_vpn_status()})
            return
        if path in ("/api/history", "/api/activity"):
            try:
                limit, offset, search = activity_query_params(
                    urlsplit(self.path).query
                )
            except ValueError as exc:
                self.send_json(400, {"ok": False, "message": str(exc)})
                return
            if path == "/api/history":
                result = get_media_history(limit, offset, search)
            else:
                result = get_activity_events(limit, offset, search)
            self.send_json(200, {"ok": True, **result})
            return

        assets = {
            "/styles.css": ("styles.css", "text/css; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
        }
        asset = assets.get(path)
        if asset is None:
            self.send_json(404, {"ok": False, "message": "Not found."})
            return
        filename, content_type = asset
        self.send_asset(filename, content_type)

    def do_POST(self):
        path = urlsplit(self.path).path
        if path == "/api/login":
            expected = os.getenv("WEB_PASSWORD", "")
            if not expected:
                self.send_json(503, {
                    "ok": False,
                    "message": "Set WEB_PASSWORD in the Unraid container settings to enable sign-in.",
                })
                return
            try:
                payload = self.read_json()
            except ValueError as exc:
                self.send_json(400, {"ok": False, "message": str(exc)})
                return
            username = payload.get("username", "") if isinstance(payload, dict) else ""
            password = payload.get("password", "") if isinstance(payload, dict) else ""
            username_bytes = username.encode("utf-8") if isinstance(username, str) else b""
            password_bytes = password.encode("utf-8") if isinstance(password, str) else b""
            username_ok = hmac.compare_digest(username_bytes, b"admin")
            password_ok = hmac.compare_digest(password_bytes, expected.encode("utf-8"))
            if not (username_ok and password_ok):
                self.send_json(401, {"ok": False, "message": "The username or password is incorrect."})
                return
            token = create_session()
            cookie_value = (
                f"{SESSION_COOKIE}={token}; Path=/; Max-Age={SESSION_TTL_SECONDS}; "
                "HttpOnly; SameSite=Strict"
            )
            self.send_json(200, {"ok": True}, [("Set-Cookie", cookie_value)])
            return

        if path == "/api/logout":
            token = self.session_token()
            with SESSION_LOCK:
                SESSIONS.pop(token, None)
            clear_cookie = f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"
            self.send_json(200, {"ok": True}, [("Set-Cookie", clear_cookie)])
            return

        if not self.require_authentication():
            return

        try:
            payload = self.read_json()
            if path == "/api/config":
                was_vpn_enabled = CONFIG["vpn_enabled"]
                settings = persist_settings(payload)
                if not settings["vpn_enabled"]:
                    set_vpn_status(
                        state="disabled", enabled=False,
                        tripped=False, consecutive_failures=0,
                        stopped_containers=[],
                        last_message="VPN Guard is turned off.",
                    )
                if settings["vpn_enabled"] != was_vpn_enabled:
                    log("VPN GUARD: " + ("Enabled." if settings["vpn_enabled"] else "Disabled."))
                self.send_json(200, {"ok": True, "settings": settings})
                return

            if path == "/api/vpn/check":
                result = run_vpn_check(trigger=CONFIG["vpn_enabled"])
                self.send_json(200, {"ok": result["state"] not in ("unavailable", "warning", "tripped"), "status": result})
                return

            if path == "/api/vpn/rearm":
                set_vpn_status(
                    state="waiting" if CONFIG["vpn_enabled"] else "disabled",
                    enabled=CONFIG["vpn_enabled"], tripped=False,
                    consecutive_failures=0, stopped_containers=[],
                    last_message="VPN Guard re-armed. It will not restart stopped containers.",
                )
                log("VPN GUARD: Manually re-armed. Stopped containers were not restarted.")
                self.send_json(200, {"ok": True, "status": read_vpn_status()})
                return

            candidate = request_settings(payload)
            if path == "/api/test/paths":
                result = test_paths(candidate)
            elif path == "/api/test/clamav":
                result = test_clamav_connection(candidate)
            elif path == "/api/test/radarr":
                result = test_arr_connection("Radarr", candidate)
            elif path == "/api/test/sonarr":
                result = test_arr_connection("Sonarr", candidate)
            else:
                self.send_json(404, {"ok": False, "message": "Not found."})
                return
            self.send_json(200, result)
        except ValueError as exc:
            self.send_json(400, {"ok": False, "message": str(exc)})
        except Exception:
            # Do not return exceptions that might contain request or credential data.
            self.send_json(500, {"ok": False, "message": "The request could not be completed."})


def log(msg):
    timestamp = datetime.now().isoformat(timespec="seconds")
    print(f"[{timestamp}] {msg}", flush=True)

    upper = msg.upper()
    if upper.startswith(("ERROR", "REJECTED", "IMPORT RETRY")):
        level = "error"
    elif upper.startswith(("APPROVED", "IMPORT COMPLETE", "VPN GUARD: CONNECTION VERIFIED", "VPN GUARD: CONNECTION CHECK RECOVERED")):
        level = "success"
    elif upper.startswith(("CHECKING", "VPN GUARD: CHECK FAILED")):
        level = "working"
    else:
        level = "info"

    try:
        with DB_LOCK:
            cursor = db.execute(
                "INSERT INTO activity_events (timestamp, level, message) VALUES (?, ?, ?)",
                (timestamp, level, msg[:2000]),
            )
            if cursor.lastrowid % 500 == 0:
                db.execute(
                    """
                    DELETE FROM activity_events
                    WHERE id < (
                        SELECT id FROM activity_events
                        ORDER BY id DESC LIMIT 1 OFFSET 4999
                    )
                    """
                )
            db.commit()
    except sqlite3.Error:
        # Console logging still works if the history database is unavailable.
        pass


def safe_name(name):
    cleaned = "".join(
        c if c.isalnum() or c in " ._-" else "_"
        for c in name
    ).strip()

    return cleaned[:180] or "unnamed"


def get_files(entry):
    if entry.is_file():
        return [entry]

    return sorted(
        p for p in entry.rglob("*")
        if p.is_file()
    )


def job_signature(category, entry, files):
    h = hashlib.sha256()

    h.update(category.encode())
    h.update(entry.name.encode("utf-8", "replace"))

    for p in files:
        try:
            st = p.stat()
        except FileNotFoundError:
            continue

        h.update(
            str(p.relative_to(INPUT)).encode(
                "utf-8", "replace"
            )
        )

        h.update(str(st.st_size).encode())
        h.update(str(st.st_mtime_ns).encode())

    return h.hexdigest()


def already_processed(signature):
    with DB_LOCK:
        row = db.execute(
            "SELECT 1 FROM jobs WHERE signature=?",
            (signature,)
        ).fetchone()

    return row is not None


def remember(signature, job, result):
    with DB_LOCK:
        db.execute(
            """
            INSERT OR REPLACE INTO jobs
            (signature, job, result, timestamp)
            VALUES (?, ?, ?, ?)
            """,
            (
                signature,
                job,
                result,
                datetime.now().isoformat()
            )
        )
        db.commit()


def activity_query_params(query_string):
    params = parse_qs(query_string)
    try:
        limit = int(params.get("limit", ["15"])[0])
        offset = int(params.get("offset", ["0"])[0])
    except (TypeError, ValueError):
        raise ValueError("Activity paging values must be numbers.")
    if limit < 1 or offset < 0:
        raise ValueError("Activity paging values are outside the allowed range.")
    return min(limit, 50), min(offset, 100000), params.get("q", [""])[0][:100].strip()


def get_media_history(limit, offset, search):
    pattern = f"%{search}%"
    with DB_LOCK:
        total = db.execute(
            """
            SELECT COUNT(*)
            FROM jobs
            WHERE (? = '' OR job LIKE ? COLLATE NOCASE OR result LIKE ? COLLATE NOCASE)
            """,
            (search, pattern, pattern),
        ).fetchone()[0]
        rows = db.execute(
            """
            SELECT j.job, j.result, j.timestamp, i.state, i.attempts
            FROM jobs AS j
            LEFT JOIN imports AS i ON i.signature = j.signature
            WHERE (? = '' OR j.job LIKE ? COLLATE NOCASE OR j.result LIKE ? COLLATE NOCASE)
            ORDER BY j.timestamp DESC, j.rowid DESC
            LIMIT ? OFFSET ?
            """,
            (search, pattern, pattern, limit, offset),
        ).fetchall()
        stats = db.execute(
            """
            SELECT
                COUNT(*),
                COALESCE(SUM(CASE WHEN j.result = 'REJECTED' THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN i.state = 'IMPORTED' OR j.result = 'IMPORTED' THEN 1 ELSE 0 END), 0),
                COALESCE(SUM(CASE WHEN j.result != 'REJECTED' AND COALESCE(i.state, 'PENDING') != 'IMPORTED' THEN 1 ELSE 0 END), 0)
            FROM jobs AS j
            LEFT JOIN imports AS i ON i.signature = j.signature
            """
        ).fetchone()

    items = [
        {
            "job": row[0],
            "result": row[1],
            "timestamp": row[2],
            "import_state": row[3],
            "attempts": row[4] or 0,
        }
        for row in rows
    ]
    return {
        "items": items,
        "total": total,
        "stats": {
            "processed": stats[0],
            "rejected": stats[1],
            "imported": stats[2],
            "waiting": stats[3],
        },
    }


def get_activity_events(limit, offset, search):
    pattern = f"%{search}%"
    with DB_LOCK:
        total = db.execute(
            """
            SELECT COUNT(*) FROM activity_events
            WHERE (? = '' OR message LIKE ? COLLATE NOCASE)
            """,
            (search, pattern),
        ).fetchone()[0]
        rows = db.execute(
            """
            SELECT id, timestamp, level, message
            FROM activity_events
            WHERE (? = '' OR message LIKE ? COLLATE NOCASE)
            ORDER BY id DESC
            LIMIT ? OFFSET ?
            """,
            (search, pattern, limit, offset),
        ).fetchall()
    return {
        "items": [
            {"id": row[0], "timestamp": row[1], "level": row[2], "message": row[3]}
            for row in rows
        ],
        "total": total,
    }


def clamav_scan(path):
    rel = path.relative_to(INPUT).as_posix()
    daemon_path = f"{CLAMAV_SCAN_ROOT}/{rel}"

    command = (
        "zSCAN " + daemon_path
    ).encode("utf-8") + b"\0"

    with socket.create_connection(
        (CLAMAV_HOST, CLAMAV_PORT),
        timeout=30
    ) as sock:

        sock.sendall(command)

        response = b""

        while b"\0" not in response:
            data = sock.recv(4096)

            if not data:
                break

            response += data

    text = response.split(
        b"\0", 1
    )[0].decode(
        "utf-8", "replace"
    )

    if " FOUND" in text:
        return False, f"MALWARE DETECTED: {text}"

    if text.endswith(" OK"):
        return True, text

    return False, f"CLAMAV ERROR: {text}"


def ffprobe_video(path):
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries",
                "stream=codec_type",
                "-of", "json",
                str(path)
            ],
            capture_output=True,
            text=True,
            timeout=45
        )

    except subprocess.TimeoutExpired:
        return False, "ffprobe timeout"

    if result.returncode != 0:
        return False, (
            result.stderr.strip()
            or "ffprobe failed"
        )

    try:
        data = json.loads(result.stdout)

    except json.JSONDecodeError:
        return False, "invalid ffprobe response"

    streams = data.get("streams", [])

    has_video = any(
        s.get("codec_type") == "video"
        for s in streams
    )

    if not has_video:
        return False, "no video stream"

    return True, "valid video stream"


def rejected_report(category, entry, reason):
    name = safe_name(
        f"{category}-{entry.name}"
    )

    stamp = datetime.now().strftime(
        "%Y%m%d-%H%M%S"
    )

    report = (
        QUARANTINE
        / "Rejected"
        / f"{name}-{stamp}.txt"
    )

    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        f"Rejected: {entry}\n"
        f"Time: {datetime.now().isoformat()}\n"
        f"Reason: {reason}\n",
        encoding="utf-8"
    )


def hardlink_approved(category, entry, approved):
    job_name = safe_name(
        entry.stem
        if entry.is_file()
        else entry.name
    )

    destination = (
        QUARANTINE
        / "Pending"
        / category
        / job_name
    )

    if destination.exists():
        shutil.rmtree(destination)

    destination.mkdir(
        parents=True,
        exist_ok=True
    )

    if category == "Movies":

        source = max(
            approved,
            key=lambda p: p.stat().st_size
        )

        os.link(
            source,
            destination / source.name
        )

        log(
            f"APPROVED MOVIE: {source.name}"
        )

        return destination

    for source in approved:

        if entry.is_file():
            relative = Path(source.name)

        else:
            relative = source.relative_to(
                entry
            )

        target = destination / relative

        target.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        os.link(
            source,
            target
        )

    log(
        f"APPROVED TV: "
        f"{len(approved)} video file(s) "
        f"from {entry.name}"
    )

    return destination


def queue_import(
    signature,
    category,
    job,
    destination
):
    with DB_LOCK:
        db.execute(
            """
            INSERT OR IGNORE INTO imports
            (
                signature,
                category,
                job,
                destination,
                state,
                attempts,
                last_attempt
            )
            VALUES (?, ?, ?, ?, 'PENDING', 0, 0)
            """,
            (
                signature,
                category,
                job,
                str(destination)
            )
        )
        db.commit()


def arr_details(category, destination):
    if category == "Movies":

        return (
            "Radarr",
            RADARR_URL,
            RADARR_API_KEY,
            "DownloadedMoviesScan",
            f"{RADARR_IMPORT_ROOT}/"
            f"{destination.name}"
        )

    return (
        "Sonarr",
        SONARR_URL,
        SONARR_API_KEY,
        "DownloadedEpisodesScan",
        f"{SONARR_IMPORT_ROOT}/"
        f"{destination.name}"
    )


def trigger_arr_import(
    category,
    destination
):
    (
        app,
        base_url,
        api_key,
        command_name,
        import_path
    ) = arr_details(
        category,
        destination
    )

    if not base_url or not api_key:
        raise RuntimeError(
            f"{app} URL/API key is not configured"
        )

    payload = json.dumps({
        "name": command_name,
        "path": import_path,
        "importMode": "Move"
    }).encode("utf-8")

    request = urllib.request.Request(
        f"{base_url}/api/v3/command",
        data=payload,
        method="POST",
        headers={
            "X-Api-Key": api_key,
            "Content-Type": "application/json",
            "Accept": "application/json"
        }
    )

    with urllib.request.urlopen(
        request,
        timeout=20
    ) as response:

        body = response.read().decode(
            "utf-8",
            "replace"
        )

    command_id = None

    try:
        command_id = json.loads(
            body
        ).get("id")

    except Exception:
        pass

    message = (
        f"QUEUED {app.upper()} IMPORT: "
        f"{import_path}"
    )

    if command_id:
        message += (
            f" (command {command_id})"
        )

    log(message)


def process_pending_imports():

    with DB_LOCK:
        rows = db.execute(
            """
            SELECT
                signature,
                category,
                job,
                destination,
                state,
                attempts,
                last_attempt
            FROM imports
            WHERE state != 'IMPORTED'
            """
        ).fetchall()

    now = time.time()

    for (
        signature,
        category,
        job,
        dest_text,
        state,
        attempts,
        last_attempt
    ) in rows:

        destination = Path(dest_text)

        # If Radarr/Sonarr successfully moved
        # the staged file into the final library,
        # the Pending job folder disappears.
        if not destination.exists():
            with DB_LOCK:
                db.execute(
                    """
                    UPDATE imports
                    SET state='IMPORTED'
                    WHERE signature=?
                    """,
                    (signature,)
                )
                db.execute(
                    """
                    UPDATE jobs
                    SET result='IMPORTED'
                    WHERE signature=?
                    """,
                    (signature,)
                )
                db.commit()

            log(
                f"IMPORT COMPLETE: {job}"
            )

            continue

        files = [
            p for p
            in destination.rglob("*")
            if p.is_file()
        ]

        if not files:

            try:
                destination.rmdir()
            except OSError:
                pass

            with DB_LOCK:
                db.execute(
                    """
                    UPDATE imports
                    SET state='IMPORTED'
                    WHERE signature=?
                    """,
                    (signature,)
                )
                db.execute(
                    """
                    UPDATE jobs
                    SET result='IMPORTED'
                    WHERE signature=?
                    """,
                    (signature,)
                )
                db.commit()

            log(
                f"IMPORT COMPLETE: {job}"
            )

            continue

        if (
            now - last_attempt
            < IMPORT_RETRY_SECONDS
        ):
            continue

        try:

            trigger_arr_import(
                category,
                destination
            )

            with DB_LOCK:
                db.execute(
                    """
                    UPDATE imports
                    SET
                        state='QUEUED',
                        attempts=attempts+1,
                        last_attempt=?
                    WHERE signature=?
                    """,
                    (
                        now,
                        signature
                    )
                )
                db.commit()

        except Exception as exc:

            with DB_LOCK:
                db.execute(
                    """
                    UPDATE imports
                    SET
                        state='RETRY',
                        attempts=attempts+1,
                        last_attempt=?
                    WHERE signature=?
                    """,
                    (
                        now,
                        signature
                    )
                )
                db.commit()

            log(
                f"IMPORT RETRY {job}: {exc}"
            )


def process_job(category, entry):

    files = get_files(entry)

    if not files:
        return

    newest = max(
        p.stat().st_mtime
        for p in files
    )

    if (
        time.time() - newest
        < SETTLE_SECONDS
    ):
        return

    signature = job_signature(
        category,
        entry,
        files
    )

    if already_processed(signature):
        return

    label = (
        f"{category}/{entry.name}"
    )

    log(
        f"Checking {label}"
    )

    # Scan EVERY file using ClamAV.
    for p in files:

        try:
            clean, reason = clamav_scan(p)

        except Exception as exc:

            reason = (
                "ClamAV communication failure: "
                f"{exc}"
            )

            rejected_report(
                category,
                entry,
                reason
            )

            remember(
                signature,
                label,
                "REJECTED"
            )

            log(
                f"REJECTED {label}: "
                f"{reason}"
            )

            return

        if not clean:

            rejected_report(
                category,
                entry,
                reason
            )

            remember(
                signature,
                label,
                "REJECTED"
            )

            log(
                f"REJECTED {label}: "
                f"{reason}"
            )

            return

    risky = [
        p for p in files
        if p.suffix.lower()
        in RISKY_EXTENSIONS
    ]

    if risky:

        reason = (
            "Risky extra file(s): "
            + ", ".join(
                str(
                    p.relative_to(INPUT)
                )
                for p in risky
            )
        )

        rejected_report(
            category,
            entry,
            reason
        )

        remember(
            signature,
            label,
            "REJECTED"
        )

        log(
            f"REJECTED {label}: "
            f"{reason}"
        )

        return

    approved = []

    for p in files:

        if (
            p.suffix.lower()
            not in VIDEO_EXTENSIONS
        ):
            continue

        lower_name = p.name.lower()

        if any(
            word in lower_name
            for word in JUNK_VIDEO_WORDS
        ):
            log(
                f"Ignoring sample/extra: "
                f"{p.name}"
            )

            continue

        valid, reason = ffprobe_video(p)

        if valid:
            approved.append(p)

        else:
            log(
                f"Video candidate rejected: "
                f"{p.name}: {reason}"
            )

    if not approved:

        reason = (
            "No validated video files were found"
        )

        rejected_report(
            category,
            entry,
            reason
        )

        remember(
            signature,
            label,
            "REJECTED"
        )

        log(
            f"REJECTED {label}: "
            f"{reason}"
        )

        return

    try:

        destination = hardlink_approved(
            category,
            entry,
            approved
        )

    except Exception as exc:

        reason = (
            f"Hardlink failure: {exc}"
        )

        rejected_report(
            category,
            entry,
            reason
        )

        remember(
            signature,
            label,
            "REJECTED"
        )

        log(
            f"REJECTED {label}: "
            f"{reason}"
        )

        return

    remember(
        signature,
        label,
        "APPROVED"
    )

    queue_import(
        signature,
        category,
        label,
        destination
    )


def scan_category(category):

    root = INPUT / category

    if not root.exists():
        return

    for entry in sorted(
        root.iterdir()
    ):

        try:
            process_job(
                category,
                entry
            )

        except Exception as exc:

            log(
                f"ERROR processing "
                f"{entry}: {exc}"
            )


def run_setup_server():
    host = os.getenv("WEB_HOST", "0.0.0.0")
    try:
        port = int(os.getenv("WEB_PORT", "8080"))
    except ValueError:
        raise RuntimeError("WEB_PORT must be a number.")
    if not 1 <= port <= 65535:
        raise RuntimeError("WEB_PORT must be between 1 and 65535.")

    server = ThreadingHTTPServer((host, port), SetupHandler)
    server.daemon_threads = True
    log(f"Setup UI listening on {host}:{port}")
    server.serve_forever()


log("MediaValidator starting")
log("ClamAV endpoint: configured" if CLAMAV_HOST else "ClamAV endpoint: not configured")
log(f"Radarr: {'configured' if RADARR_URL else 'not configured'}")
log(f"Sonarr: {'configured' if SONARR_URL else 'not configured'}")

web_thread = threading.Thread(
    target=run_setup_server,
    name="setup-ui",
    daemon=True,
)
web_thread.start()

vpn_thread = threading.Thread(
    target=vpn_guard_worker,
    name="vpn-guard",
    daemon=True,
)
vpn_thread.start()

while True:
    try:
        scan_category("Movies")
        scan_category("TV")
        process_pending_imports()
    except Exception as exc:
        log(f"Scan-cycle error: {exc}")
    time.sleep(SCAN_INTERVAL)
