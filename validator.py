import base64
import binascii
import hmac
import os
import time
import json
import socket
import sqlite3
import hashlib
import shutil
import subprocess
import tempfile
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from pathlib import Path
from datetime import datetime
from urllib.parse import urlsplit

STATE = Path(os.getenv("STATE_DIR", "/state")).expanduser()
CONFIG_FILE = STATE / "config.json"
CONFIG_LOCK = threading.RLock()


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

db = sqlite3.connect(STATE / "validator.db")

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

db.commit()


SECRET_FIELDS = ("radarr_api_key", "sonarr_api_key")


def public_settings():
    with CONFIG_LOCK:
        result = {key: value for key, value in CONFIG.items() if key not in SECRET_FIELDS}
        result["radarr_api_key_set"] = bool(CONFIG["radarr_api_key"])
        result["sonarr_api_key_set"] = bool(CONFIG["sonarr_api_key"])
        return result


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


class SetupHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Do not log setup request bodies or connection-test details.
        return

    def send_content(self, status, body, content_type):
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
            "base-uri 'self'; frame-ancestors 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, data):
        self.send_content(
            status,
            json.dumps(data).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def authenticated(self):
        expected = os.getenv("WEB_PASSWORD", "")
        if not expected:
            self.send_json(503, {
                "ok": False,
                "message": "Set WEB_PASSWORD in the container settings to enable the setup page.",
            })
            return False

        authorization = self.headers.get("Authorization", "")
        scheme, _, encoded = authorization.partition(" ")
        if scheme.lower() == "basic" and encoded:
            try:
                credentials = base64.b64decode(encoded, validate=True).decode("utf-8")
                username, separator, password = credentials.partition(":")
            except (binascii.Error, UnicodeDecodeError):
                username, separator, password = "", "", ""
            if separator and username == "admin" and hmac.compare_digest(password, expected):
                return True

        body = json.dumps({"ok": False, "message": "Authentication required."}).encode("utf-8")
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Media Manager setup"')
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)
        return False

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
        if not self.authenticated():
            return
        path = urlsplit(self.path).path
        if path == "/api/config":
            self.send_json(200, {"ok": True, "settings": public_settings()})
            return
        if path == "/api/health":
            self.send_json(200, {"ok": True})
            return

        assets = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/index.html": ("index.html", "text/html; charset=utf-8"),
            "/styles.css": ("styles.css", "text/css; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
        }
        asset = assets.get(path)
        if asset is None:
            self.send_json(404, {"ok": False, "message": "Not found."})
            return
        filename, content_type = asset
        try:
            body = (WEB_ROOT / filename).read_bytes()
        except OSError:
            self.send_json(500, {"ok": False, "message": "Setup page files are missing."})
            return
        self.send_content(200, body, content_type)

    def do_POST(self):
        if not self.authenticated():
            return
        path = urlsplit(self.path).path
        try:
            payload = self.read_json()
            if path == "/api/config":
                settings = persist_settings(payload)
                self.send_json(200, {"ok": True, "settings": settings})
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
    print(
        f"[{datetime.now().isoformat(timespec='seconds')}] {msg}",
        flush=True
    )


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
    row = db.execute(
        "SELECT 1 FROM jobs WHERE signature=?",
        (signature,)
    ).fetchone()

    return row is not None


def remember(signature, job, result):
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

while True:
    try:
        scan_category("Movies")
        scan_category("TV")
        process_pending_imports()
    except Exception as exc:
        log(f"Scan-cycle error: {exc}")
    time.sleep(SCAN_INTERVAL)
