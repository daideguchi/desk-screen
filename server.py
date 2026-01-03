#!/usr/bin/env python3
import base64
import datetime
import errno
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional, Tuple
from urllib.parse import urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
try:
    UI_REV = int(os.path.getmtime(__file__))
except Exception:
    UI_REV = int(time.time())


def _load_dotenv(path: str) -> None:
    """
    Minimal .env loader (no deps).
    - Supports: KEY=VALUE, optional quotes, optional leading `export `
    - Does NOT override existing os.environ
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except Exception:
        return

    for raw in lines:
        line = (raw or "").strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = (k or "").strip()
        if not k or k in os.environ:
            continue
        v = (v or "").strip()
        if len(v) >= 2 and ((v[0] == v[-1] == '"') or (v[0] == v[-1] == "'")):
            v = v[1:-1]
        os.environ[k] = v


_load_dotenv(os.path.join(BASE_DIR, ".env"))
TODO_PATH = os.path.join(BASE_DIR, "todo.txt")
MEMO_PATH = os.path.join(BASE_DIR, "memo.txt")
SCPT_PATH = os.path.join(BASE_DIR, "calendar_export.scpt")
PORT_FILE = os.path.join(BASE_DIR, "port.txt")
LOCK_FILE = os.path.join(BASE_DIR, "server.lock")
DDPORTS_PATH = os.path.expanduser("~/.ddports.json")

HOST = os.environ.get("DESK_SCREEN_HOST", "0.0.0.0")
PORT_ENV = os.environ.get("DESK_SCREEN_PORT", "").strip()
CALENDAR_SOURCE_ENV = os.environ.get("DESK_SCREEN_CALENDAR_SOURCE", "auto").strip().lower()
CALENDAR_RANGE_ENV = os.environ.get("DESK_SCREEN_CALENDAR_RANGE", "").strip().lower()
CALENDAR_DAYS_ENV = os.environ.get("DESK_SCREEN_CALENDAR_DAYS", "").strip().lower()

REFRESH_SEC = 60
CALENDAR_CACHE_TTL_SEC = 60
CALENDAR_LOOKAHEAD_DAYS_DEFAULT = 30
MAX_TODO_LINES = 8
MAX_EVENTS = 120

GOOGLE_CONFIG_PATH = os.environ.get("DESK_SCREEN_GOOGLE_CONFIG", os.path.join(BASE_DIR, "google_calendar.json"))
GOOGLE_SERVICE_ACCOUNT_DEFAULT = os.path.join(BASE_DIR, "credentials", "service_account.json")
GOOGLE_CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
GOOGLE_TIMEOUT_SEC = 10
GOOGLE_QUOTA_PROJECT = (os.environ.get("DESK_SCREEN_GOOGLE_QUOTA_PROJECT") or "").strip()

GOOGLE_USER_OAUTH_TOKEN_PATH = os.environ.get(
    "DESK_SCREEN_GOOGLE_USER_TOKEN", os.path.join(BASE_DIR, "credentials", "user_oauth_token.json")
)
GOOGLE_TASKLIST_ID = (os.environ.get("DESK_SCREEN_GOOGLE_TASKLIST_ID", "@default") or "").strip() or "@default"
GOOGLE_TASKS_CACHE_TTL_SEC = 60
GOOGLE_TASKS_MAX = 30

WEATHER_CACHE_TTL_SEC = 600
WEATHER_GEO_CACHE_TTL_SEC = 86400
WEATHER_TIMEOUT_SEC = 8
WEATHER_DEFAULT_TZ = "Asia/Tokyo"
WEATHER_DEFAULT_LAT = 35.681236
WEATHER_DEFAULT_LON = 139.767125

TODO_MAX_WRITE_LINES = 200
TODO_TEXT_MAX_LEN = 200
TODO_TOKEN_FILE = os.path.join(BASE_DIR, "todo_token.txt")
MEMO_MAX_CHARS = 8000

PORT_RANGE_MIN = 49152
PORT_RANGE_MAX = 65535

_calendar_cache_lock = threading.Lock()
_calendar_cache = {"ts": 0, "events": [], "source": "none", "error": "", "meta": {}}

_google_token_lock = threading.Lock()
_google_token_cache = {"key": "", "token": "", "exp": 0}

_tasks_cache_lock = threading.Lock()
_tasks_cache = {"ts": 0, "tasks": [], "error": ""}

_weather_cache_lock = threading.Lock()
_weather_cache = {"ts": 0, "key": "", "weather": {}, "error": ""}

_weather_geo_cache_lock = threading.Lock()
_weather_geo_cache: dict[str, dict] = {}

_todo_lock = threading.Lock()
_memo_lock = threading.Lock()


def _parse_port(value: str) -> Optional[int]:
    try:
        p = int(value)
    except Exception:
        return None
    if 1 <= p <= 65535:
        return p
    return None


def _parse_lookahead_days(value: str) -> Optional[int]:
    s = (value or "").strip().lower()
    if not s:
        return None
    mult = 1
    if s.endswith("d"):
        s = s[:-1]
    elif s.endswith("w"):
        mult = 7
        s = s[:-1]
    elif s.endswith("m"):
        mult = 30
        s = s[:-1]
    try:
        n = int(s)
    except Exception:
        return None
    days = n * mult
    if days < 1:
        days = 1
    if days > 365:
        days = 365
    return days


def calendar_lookahead_days() -> int:
    # Flexible & readable:
    # - DESK_SCREEN_CALENDAR_RANGE: "30d" / "4w" / "1m" / "30"
    # - DESK_SCREEN_CALENDAR_DAYS: "30"
    d = _parse_lookahead_days(os.environ.get("DESK_SCREEN_CALENDAR_RANGE", ""))
    if d is None:
        d = _parse_lookahead_days(os.environ.get("DESK_SCREEN_CALENDAR_DAYS", ""))
    return d if d is not None else CALENDAR_LOOKAHEAD_DAYS_DEFAULT


def _read_port_file() -> Optional[int]:
    try:
        with open(PORT_FILE, "r", encoding="utf-8") as f:
            return _parse_port(f.read().strip())
    except Exception:
        return None


def _write_port_file(port: int) -> None:
    try:
        with open(PORT_FILE, "w", encoding="utf-8") as f:
            f.write(f"{port}\n")
    except Exception:
        pass


def _reserved_ports() -> set[int]:
    ports: set[int] = set()
    try:
        with open(DDPORTS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k in data.keys():
            p = _parse_port(str(k))
            if p is not None:
                ports.add(p)
    except Exception:
        pass
    return ports


def _stable_seed_port() -> int:
    # Stable-ish per machine+dir, in a high range to avoid common dev ports.
    seed = f"{os.getuid()}:{socket.gethostname()}:{BASE_DIR}".encode("utf-8", errors="ignore")
    h = 0
    for b in seed:
        h = (h * 131 + b) & 0xFFFFFFFF
    span = (PORT_RANGE_MAX - PORT_RANGE_MIN) + 1
    return PORT_RANGE_MIN + (h % span)


def _acquire_lock_or_exit() -> None:
    try:
        import fcntl
    except Exception:
        return

    os.makedirs(BASE_DIR, exist_ok=True)
    fd = os.open(LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # Already running (likely). Surface the last known port for convenience.
        p = _read_port_file()
        msg = "Desk Screen: already running"
        if p:
            msg += f" (port {p})"
        print(msg)
        raise SystemExit(1)


def _create_server(host: str, preferred_port: Optional[int]) -> Tuple[ThreadingHTTPServer, int]:
    reserved = _reserved_ports()
    candidates: list[int] = []

    if preferred_port is not None:
        candidates.append(preferred_port)

    port_from_file = _read_port_file()
    if port_from_file is not None and port_from_file not in candidates:
        candidates.append(port_from_file)

    seed = _stable_seed_port()
    # Try up to 512 ports deterministically from the seed, skipping reserved.
    span = (PORT_RANGE_MAX - PORT_RANGE_MIN) + 1
    for i in range(512):
        p = PORT_RANGE_MIN + ((seed - PORT_RANGE_MIN + i) % span)
        if p in reserved:
            continue
        if p not in candidates:
            candidates.append(p)

    # Finally, ask the OS for a free port (guaranteed non-collision at bind time).
    candidates.append(0)

    last_err: Optional[OSError] = None
    for port in candidates:
        try:
            httpd = ThreadingHTTPServer((host, port), Handler)
            actual_port = int(httpd.server_address[1])
            return httpd, actual_port
        except OSError as e:
            last_err = e
            if e.errno in (errno.EADDRINUSE, errno.EACCES):
                continue
            raise

    if last_err:
        raise last_err
    raise RuntimeError("Failed to bind server port")


def read_todo(max_lines: int = MAX_TODO_LINES) -> list[str]:
    if not os.path.exists(TODO_PATH):
        return []
    lines: list[str] = []
    try:
        with open(TODO_PATH, "r", encoding="utf-8") as f:
            for raw in f:
                s = raw.strip()
                if not s:
                    continue
                lines.append(s)
                if len(lines) >= max_lines:
                    break
    except Exception:
        return []
    return lines


def _todo_expected_token() -> str:
    token = os.environ.get("DESK_SCREEN_TODO_TOKEN", "").strip()
    if token:
        return token
    try:
        with open(TODO_TOKEN_FILE, "r", encoding="utf-8") as f:
            return (f.read() or "").strip()
    except Exception:
        return ""


def _todo_check_auth(header_token: str) -> Tuple[bool, str]:
    """
    - Always requires `X-Desk-Token` header to be present (prevents simple CSRF).
    - If `DESK_SCREEN_TODO_TOKEN` (or todo_token.txt) is set, it must match.
    """
    tok = (header_token or "").strip()
    if not tok:
        return False, "missing X-Desk-Token"
    expected = _todo_expected_token()
    if expected and tok != expected:
        return False, "invalid token"
    return True, ""


def _sanitize_todo_text(text: str) -> str:
    s = (text or "").strip()
    s = s.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    while "  " in s:
        s = s.replace("  ", " ")
    if len(s) > TODO_TEXT_MAX_LEN:
        s = s[:TODO_TEXT_MAX_LEN].rstrip()
    return s


def _read_todo_all() -> list[str]:
    if not os.path.exists(TODO_PATH):
        return []
    out: list[str] = []
    try:
        with open(TODO_PATH, "r", encoding="utf-8") as f:
            for raw in f:
                s = raw.strip()
                if s:
                    out.append(s)
    except Exception:
        return []
    return out


def _write_todo_all(lines: list[str]) -> None:
    os.makedirs(BASE_DIR, exist_ok=True)
    tmp_path = TODO_PATH + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        for line in lines:
            s = (line or "").strip()
            if not s:
                continue
            f.write(s + "\n")
    os.replace(tmp_path, TODO_PATH)


def todo_add(text: str, top: bool = True) -> list[str]:
    s = _sanitize_todo_text(text)
    if not s:
        raise ValueError("empty todo")

    with _todo_lock:
        lines = _read_todo_all()
        if top:
            lines = [s] + lines
        else:
            lines.append(s)
        lines = lines[:TODO_MAX_WRITE_LINES]
        _write_todo_all(lines)
        return lines


def todo_delete(index: int) -> list[str]:
    with _todo_lock:
        lines = _read_todo_all()
        if index < 0 or index >= len(lines):
            return lines
        lines.pop(index)
        _write_todo_all(lines)
        return lines


def memo_read(max_chars: int = MEMO_MAX_CHARS) -> Tuple[str, int]:
    if not os.path.exists(MEMO_PATH):
        return "", 0
    try:
        with open(MEMO_PATH, "r", encoding="utf-8") as f:
            s = f.read()
    except Exception:
        return "", 0
    if len(s) > max_chars:
        s = s[:max_chars]
    try:
        mtime = int(os.path.getmtime(MEMO_PATH))
    except Exception:
        mtime = 0
    return s, mtime


def memo_write(text: str, max_chars: int = MEMO_MAX_CHARS) -> int:
    s = text or ""
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    if len(s) > max_chars:
        s = s[:max_chars]

    with _memo_lock:
        os.makedirs(BASE_DIR, exist_ok=True)
        tmp_path = MEMO_PATH + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(s)
        os.replace(tmp_path, MEMO_PATH)

    try:
        return int(os.path.getmtime(MEMO_PATH))
    except Exception:
        return int(time.time())


def _parse_epoch_field(s: str) -> Optional[int]:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        try:
            return int(float(s))
        except ValueError:
            return None


def _run_calendar_scpt() -> Tuple[list[dict], str]:
    """
    AppleScript(log) output (often on stderr).
    Format: start_epoch<TAB>end_epoch<TAB>title<TAB>location
    """
    if not os.path.exists(SCPT_PATH):
        return [], "missing calendar_export.scpt"

    days = calendar_lookahead_days()
    p = subprocess.run(
        ["osascript", SCPT_PATH, str(days)],
        capture_output=True,
        text=True,
        check=False,
    )

    out = (p.stdout or "") + "\n" + (p.stderr or "")
    if p.returncode != 0:
        lines = [ln.strip() for ln in out.splitlines() if (ln or "").strip()]
        msg = lines[-1] if lines else f"osascript failed (exit {p.returncode})"
        if len(msg) > 220:
            msg = msg[:220] + "…"
        return [], msg

    events: list[dict] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        start = _parse_epoch_field(parts[0])
        end = _parse_epoch_field(parts[1])
        if start is None or end is None:
            continue
        title = parts[2].strip()
        loc = parts[3].strip() if len(parts) >= 4 else ""
        events.append({"start": start, "end": end, "title": title, "location": loc})

    events.sort(key=lambda x: x["start"])
    return events, ""


def _resolve_path(path: str) -> str:
    p = (path or "").strip()
    if not p:
        return ""
    if os.path.isabs(p):
        return p
    return os.path.join(BASE_DIR, p)


def _read_json_file(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _read_google_calendar_config() -> dict:
    cfg_path = _resolve_path(os.environ.get("DESK_SCREEN_GOOGLE_CONFIG", GOOGLE_CONFIG_PATH))
    cfg = _read_json_file(cfg_path) if cfg_path and os.path.exists(cfg_path) else {}

    enabled = cfg.get("enabled", True)
    if enabled is False:
        return {"enabled": False}

    calendar_ids: list[str] = []
    env_ids = os.environ.get("DESK_SCREEN_GOOGLE_CALENDAR_IDS", "").strip()
    env_id = os.environ.get("DESK_SCREEN_GOOGLE_CALENDAR_ID", "").strip()
    if env_ids:
        calendar_ids = [s.strip() for s in env_ids.split(",") if s.strip()]
    elif env_id:
        calendar_ids = [env_id]
    else:
        raw_ids = cfg.get("calendar_ids")
        raw_id = cfg.get("calendar_id")
        if isinstance(raw_ids, list):
            calendar_ids = [str(x).strip() for x in raw_ids if str(x).strip()]
        elif isinstance(raw_ids, str) and raw_ids.strip():
            calendar_ids = [raw_ids.strip()]
        elif isinstance(raw_id, str) and raw_id.strip():
            calendar_ids = [raw_id.strip()]

    sa_path = (
        os.environ.get("DESK_SCREEN_GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
        or cfg.get("service_account_json")
        or GOOGLE_SERVICE_ACCOUNT_DEFAULT
    )
    sa_path = _resolve_path(sa_path)

    impersonate = os.environ.get("DESK_SCREEN_GOOGLE_IMPERSONATE", "").strip() or cfg.get("impersonate") or ""

    if not calendar_ids or not sa_path or not os.path.exists(sa_path):
        return {
            "enabled": False,
            "calendar_ids": calendar_ids,
            "service_account_json": sa_path,
            "impersonate": impersonate,
        }

    return {
        "enabled": True,
        "calendar_ids": calendar_ids,
        "service_account_json": sa_path,
        "impersonate": impersonate,
    }


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _openssl_rs256_sign(private_key_pem: str, message: bytes) -> bytes:
    with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as f:
        key_path = f.name
        f.write(private_key_pem)

    try:
        p = subprocess.run(
            ["/usr/bin/openssl", "dgst", "-sha256", "-sign", key_path],
            input=message,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if p.returncode != 0:
            err = (p.stderr or b"").decode("utf-8", errors="ignore")
            raise RuntimeError(f"openssl sign failed: {err}".strip())
        return p.stdout
    finally:
        try:
            os.unlink(key_path)
        except Exception:
            pass


def _jwt_rs256(private_key_pem: str, claim: dict) -> str:
    header = {"alg": "RS256", "typ": "JWT"}
    header_b64 = _b64url(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    claim_b64 = _b64url(json.dumps(claim, separators=(",", ":")).encode("utf-8"))
    signing_input = f"{header_b64}.{claim_b64}".encode("ascii")
    sig = _openssl_rs256_sign(private_key_pem, signing_input)
    return f"{header_b64}.{claim_b64}.{_b64url(sig)}"


def _http_post_form_json(url: str, form: dict, timeout_sec: int = GOOGLE_TIMEOUT_SEC) -> dict:
    data = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "desk_screen/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout_sec) as r:
        return json.loads(r.read().decode("utf-8"))


def _http_get_json(url: str, headers: dict, timeout_sec: int = GOOGLE_TIMEOUT_SEC) -> dict:
    h = {"Accept": "application/json", "User-Agent": "desk_screen/1.0", **(headers or {})}
    if GOOGLE_QUOTA_PROJECT and not any(str(k).lower() == "x-goog-user-project" for k in h.keys()):
        h["X-Goog-User-Project"] = GOOGLE_QUOTA_PROJECT
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout_sec) as r:
        return json.loads(r.read().decode("utf-8"))


def _http_request_json(
    url: str,
    method: str,
    headers: dict,
    body_obj: Optional[dict] = None,
    timeout_sec: int = GOOGLE_TIMEOUT_SEC,
) -> dict:
    data = None
    h = {"Accept": "application/json", "User-Agent": "desk_screen/1.0", **(headers or {})}
    if GOOGLE_QUOTA_PROJECT and not any(str(k).lower() == "x-goog-user-project" for k in h.keys()):
        h["X-Goog-User-Project"] = GOOGLE_QUOTA_PROJECT
    if body_obj is not None:
        data = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")
        h["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    with urllib.request.urlopen(req, timeout=timeout_sec) as r:
        return json.loads(r.read().decode("utf-8"))


def _http_error_message(e: Exception) -> str:
    if isinstance(e, urllib.error.HTTPError):
        try:
            body = e.read().decode("utf-8", errors="ignore")
            j = json.loads(body or "{}")
            err = j.get("error") or {}
            if isinstance(err, dict):
                msg = (err.get("message") or "").strip()
                status = (err.get("status") or "").strip()
                if msg and status:
                    return f"HTTP {e.code} {status}: {msg}"
                if msg:
                    return f"HTTP {e.code}: {msg}"
        except Exception:
            pass
        return f"HTTP {e.code}: {getattr(e, 'reason', '')}".strip()
    return str(e)


def _google_user_oauth_path() -> str:
    p = os.environ.get("DESK_SCREEN_GOOGLE_USER_TOKEN", GOOGLE_USER_OAUTH_TOKEN_PATH)
    return _resolve_path(p)


def _read_google_user_oauth_token() -> dict:
    path = _google_user_oauth_path()
    if not path or not os.path.exists(path):
        return {}
    return _read_json_file(path)


def _google_user_missing_scopes(token_obj: dict, required_scopes: list[str]) -> list[str]:
    scopes = token_obj.get("scopes")
    if not isinstance(scopes, list):
        return []
    have = {str(s) for s in scopes if str(s).strip()}
    return [s for s in required_scopes if s not in have]


def _google_user_is_configured() -> bool:
    tok = _read_google_user_oauth_token()
    return bool(tok.get("refresh_token") and tok.get("client_id") and tok.get("client_secret"))


def _google_user_get_access_token() -> Tuple[str, int]:
    path = _google_user_oauth_path()
    if not path or not os.path.exists(path):
        raise RuntimeError("missing user_oauth_token.json (run setup_google_oauth.py)")

    tok = _read_json_file(path)
    client_id = (tok.get("client_id") or "").strip()
    client_secret = (tok.get("client_secret") or "").strip()
    refresh_token = (tok.get("refresh_token") or "").strip()
    token_uri = (tok.get("token_uri") or "https://oauth2.googleapis.com/token").strip()
    if not client_id or not client_secret or not refresh_token:
        raise RuntimeError("user_oauth_token.json missing client_id/client_secret/refresh_token")

    try:
        mtime = int(os.path.getmtime(path))
    except Exception:
        mtime = 0

    cache_key = f"user|{path}|{mtime}"
    now = int(time.time())
    with _google_token_lock:
        if _google_token_cache.get("key") == cache_key:
            token = str(_google_token_cache.get("token") or "")
            exp = int(_google_token_cache.get("exp") or 0)
            if token and now < (exp - 60):
                return token, exp

    try:
        resp = _http_post_form_json(
            token_uri,
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
    except Exception as e:
        raise RuntimeError(_http_error_message(e))

    token = (resp.get("access_token") or "").strip()
    expires_in = int(resp.get("expires_in") or 3600)
    if not token:
        raise RuntimeError("failed to refresh google access_token")

    exp = now + expires_in
    with _google_token_lock:
        _google_token_cache["key"] = cache_key
        _google_token_cache["token"] = token
        _google_token_cache["exp"] = exp

    return token, exp


def _google_get_access_token(service_account_json: str, impersonate: str) -> Tuple[str, int]:
    scopes = " ".join(GOOGLE_CALENDAR_SCOPES)
    cache_key = f"{service_account_json}|{impersonate}|{scopes}"
    now = int(time.time())

    with _google_token_lock:
        if _google_token_cache.get("key") == cache_key:
            token = str(_google_token_cache.get("token") or "")
            exp = int(_google_token_cache.get("exp") or 0)
            if token and now < (exp - 60):
                return token, exp

    sa = _read_json_file(service_account_json)
    private_key = sa.get("private_key")
    client_email = sa.get("client_email")
    token_uri = sa.get("token_uri") or "https://oauth2.googleapis.com/token"
    if not private_key or not client_email:
        raise RuntimeError("service_account_json missing private_key/client_email")

    claim = {
        "iss": client_email,
        "scope": scopes,
        "aud": token_uri,
        "iat": now,
        "exp": now + 3600,
    }
    if impersonate:
        claim["sub"] = impersonate

    assertion = _jwt_rs256(private_key, claim)
    resp = _http_post_form_json(
        token_uri,
        {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        },
    )
    token = resp.get("access_token")
    expires_in = int(resp.get("expires_in") or 3600)
    if not token:
        raise RuntimeError("failed to obtain google access_token")

    exp = now + expires_in
    with _google_token_lock:
        _google_token_cache["key"] = cache_key
        _google_token_cache["token"] = token
        _google_token_cache["exp"] = exp

    return token, exp


def _parse_rfc3339_epoch(s: str) -> Optional[int]:
    v = (s or "").strip()
    if not v:
        return None
    if v.endswith("Z"):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.datetime.fromisoformat(v)
        return int(dt.timestamp())
    except Exception:
        return None


def _parse_date_midnight_local_epoch(s: str) -> Optional[int]:
    v = (s or "").strip()
    if not v:
        return None
    try:
        d = datetime.date.fromisoformat(v)
        dt = datetime.datetime(d.year, d.month, d.day, 0, 0, 0)
        return int(time.mktime(dt.timetuple()))
    except Exception:
        return None


def _google_list_events(access_token: str, calendar_id: str, time_min: str, time_max: str, max_results: int = 50) -> list[dict]:
    cid = urllib.parse.quote(calendar_id, safe="")
    q = urllib.parse.urlencode(
        {
            "timeMin": time_min,
            "timeMax": time_max,
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": str(max_results),
        }
    )
    url = f"https://www.googleapis.com/calendar/v3/calendars/{cid}/events?{q}"
    data = _http_get_json(url, headers={"Authorization": f"Bearer {access_token}"})

    out: list[dict] = []
    for item in data.get("items", []) or []:
        start_obj = item.get("start") or {}
        end_obj = item.get("end") or {}

        all_day = False
        start_epoch: Optional[int] = None
        end_epoch: Optional[int] = None

        if "dateTime" in start_obj:
            start_epoch = _parse_rfc3339_epoch(str(start_obj.get("dateTime") or ""))
            end_epoch = _parse_rfc3339_epoch(str(end_obj.get("dateTime") or ""))
        elif "date" in start_obj:
            all_day = True
            start_epoch = _parse_date_midnight_local_epoch(str(start_obj.get("date") or ""))
            end_epoch = _parse_date_midnight_local_epoch(str(end_obj.get("date") or ""))

        if start_epoch is None or end_epoch is None:
            continue

        out.append(
            {
                "start": start_epoch,
                "end": end_epoch,
                "title": (item.get("summary") or "").strip() or "(no title)",
                "location": (item.get("location") or "").strip(),
                "all_day": all_day,
            }
        )

    out.sort(key=lambda x: x["start"])
    return out


def _calendar_ids_for_user_oauth_env() -> list[str]:
    env_ids = os.environ.get("DESK_SCREEN_GOOGLE_CALENDAR_IDS", "").strip()
    env_id = os.environ.get("DESK_SCREEN_GOOGLE_CALENDAR_ID", "").strip()
    if env_ids:
        return [s.strip() for s in env_ids.split(",") if s.strip()]
    if env_id:
        return [env_id]
    return []


def _google_calendar_mode() -> str:
    mode = (os.environ.get("DESK_SCREEN_GOOGLE_CALENDAR_MODE") or "").strip().lower()
    if mode in ("primary", "selected", "all"):
        return mode
    return "selected"


def _google_calendar_list_calendars(access_token: str, max_results: int = 250) -> list[dict]:
    out: list[dict] = []
    page_token = ""
    limit = max(1, min(int(max_results or 250), 250))
    while True:
        q = {
            "maxResults": str(limit),
            "fields": "items(id,summary,accessRole,selected,primary,hidden),nextPageToken",
        }
        if page_token:
            q["pageToken"] = page_token
        url = "https://www.googleapis.com/calendar/v3/users/me/calendarList?" + urllib.parse.urlencode(q)
        data = _http_get_json(url, headers={"Authorization": f"Bearer {access_token}"})
        items = data.get("items", []) or []
        for it in items:
            if isinstance(it, dict):
                out.append(it)
        page_token = str(data.get("nextPageToken") or "").strip()
        if not page_token:
            break
        if len(out) >= max_results:
            break
    return out[: max(1, int(max_results or 250))]


def _google_user_calendar_ids(access_token: str) -> Tuple[list[str], str, str]:
    """
    Decide which calendar IDs to query for OAuth user flow.
    Priority:
      1) Explicit env override: DESK_SCREEN_GOOGLE_CALENDAR_IDS / _ID
      2) Auto-detect via calendarList.list (mode=selected/all)
      3) Fallback: primary
    """
    env_ids = _calendar_ids_for_user_oauth_env()
    if env_ids:
        return env_ids, "explicit", ""

    mode = _google_calendar_mode()
    if mode == "primary":
        return ["primary"], mode, ""

    try:
        items = _google_calendar_list_calendars(access_token, max_results=250)
    except Exception as e:
        return ["primary"], mode, f"calendarList error: {_http_error_message(e)}"

    ids: list[str] = []
    for it in items:
        cid = str(it.get("id") or "").strip()
        if not cid:
            continue
        if bool(it.get("hidden")):
            continue
        role = str(it.get("accessRole") or "").strip().lower()
        if not role or role == "none":
            continue
        if mode == "selected":
            if not (bool(it.get("selected")) or bool(it.get("primary"))):
                continue
        ids.append(cid)

    uniq: list[str] = []
    seen: set[str] = set()
    for cid in ids:
        if cid in seen:
            continue
        seen.add(cid)
        uniq.append(cid)

    if not uniq:
        return ["primary"], mode, f"no calendars matched mode={mode} (fallback to primary)"
    return uniq, mode, ""


def _run_google_calendar_user_oauth() -> Tuple[list[dict], str, dict]:
    tok = _read_google_user_oauth_token()
    if not tok:
        return [], "google oauth not configured (missing user_oauth_token.json)", {}
    missing = _google_user_missing_scopes(tok, GOOGLE_CALENDAR_SCOPES)
    if missing:
        return [], "google oauth missing calendar scope (rerun setup_google_oauth.py)", {}

    lookahead_days = calendar_lookahead_days()
    now_dt = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    end_dt = now_dt + datetime.timedelta(days=lookahead_days)
    time_min = now_dt.isoformat().replace("+00:00", "Z")
    time_max = end_dt.isoformat().replace("+00:00", "Z")

    try:
        token, _exp = _google_user_get_access_token()
        calendar_ids, mode, ids_err = _google_user_calendar_ids(token)
        merged: list[dict] = []
        seen: set[tuple] = set()
        failures = 0
        for cid in calendar_ids:
            try:
                events = _google_list_events(token, str(cid), time_min, time_max, max_results=MAX_EVENTS * 2)
                for ev in events:
                    k = (ev.get("start"), ev.get("end"), ev.get("title"), ev.get("location"), ev.get("all_day"))
                    if k in seen:
                        continue
                    seen.add(k)
                    merged.append(ev)
            except Exception:
                failures += 1
        merged.sort(key=lambda x: x["start"])
        meta = {"mode": mode, "ids_count": len(calendar_ids)}
        if ids_err and not merged:
            return [], f"google oauth error: {ids_err}", meta
        if not merged and failures >= max(1, len(calendar_ids)):
            return [], "google oauth error: failed to read any calendars", meta
        return merged, "", meta
    except Exception as e:
        return [], f"google oauth error: {_http_error_message(e)}", {}


def _run_google_calendar() -> Tuple[list[dict], str, dict]:
    cfg = _read_google_calendar_config()
    if not cfg.get("enabled"):
        return [], "google not configured (missing google_calendar.json or calendar_ids)", {}

    service_account_json = str(cfg.get("service_account_json") or "")
    calendar_ids = cfg.get("calendar_ids") or []
    impersonate = str(cfg.get("impersonate") or "")

    lookahead_days = calendar_lookahead_days()
    now_dt = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    end_dt = now_dt + datetime.timedelta(days=lookahead_days)
    time_min = now_dt.isoformat().replace("+00:00", "Z")
    time_max = end_dt.isoformat().replace("+00:00", "Z")

    try:
        token, _exp = _google_get_access_token(service_account_json, impersonate)
        merged: list[dict] = []
        seen: set[tuple] = set()
        for cid in calendar_ids:
            events = _google_list_events(token, str(cid), time_min, time_max, max_results=MAX_EVENTS * 2)
            for ev in events:
                k = (ev.get("start"), ev.get("end"), ev.get("title"), ev.get("location"), ev.get("all_day"))
                if k in seen:
                    continue
                seen.add(k)
                merged.append(ev)
        merged.sort(key=lambda x: x["start"])
        return merged, "", {"mode": "service_account", "ids_count": len(calendar_ids)}
    except Exception as e:
        return [], f"google error: {e}", {"mode": "service_account", "ids_count": len(calendar_ids)}


def _fetch_calendar_events() -> Tuple[list[dict], str, str, dict]:
    source = (CALENDAR_SOURCE_ENV or "auto").lower()

    if source in ("apple", "mac", "osascript"):
        events, err = _run_calendar_scpt()
        return events, "apple", err, {}

    if source in ("google_oauth", "gcal_oauth", "google_user", "oauth"):
        events, err, meta = _run_google_calendar_user_oauth()
        return events, "google_oauth", err, meta

    if source in ("google_sa", "gcal_sa", "google_service", "service"):
        events, err, meta = _run_google_calendar()
        return events, "google_sa", err, meta

    if source in ("google", "gcal"):
        if _google_user_is_configured():
            events, err, meta = _run_google_calendar_user_oauth()
            if not err:
                return events, "google_oauth", "", meta
            cfg = _read_google_calendar_config()
            if cfg.get("enabled"):
                events2, err2, meta2 = _run_google_calendar()
                if not err2:
                    return events2, "google_sa", err, meta2
            apple_events, apple_err = _run_calendar_scpt()
            combined = " / ".join([e for e in [err, apple_err] if e])
            return apple_events, "apple", combined, {}

        cfg = _read_google_calendar_config()
        if cfg.get("enabled"):
            events, err, meta = _run_google_calendar()
            if not err:
                return events, "google_sa", "", meta
            apple_events, apple_err = _run_calendar_scpt()
            combined = " / ".join([e for e in [err, apple_err] if e])
            return apple_events, "apple", combined, {}

        apple_events, apple_err = _run_calendar_scpt()
        msg = "google oauth not configured (run: ~/desk oauth)"
        combined = " / ".join([e for e in [msg, apple_err] if e])
        return apple_events, "apple", combined, {}

    # auto
    oauth_err = ""
    sa_err = ""
    if _google_user_is_configured():
        events, err, meta = _run_google_calendar_user_oauth()
        if not err:
            return events, "google_oauth", "", meta
        oauth_err = err

    cfg = _read_google_calendar_config()
    if cfg.get("enabled"):
        events, err, meta = _run_google_calendar()
        if not err:
            return events, "google_sa", oauth_err, meta
        sa_err = err

    apple_events, apple_err = _run_calendar_scpt()
    combined_err = " / ".join([e for e in [oauth_err, sa_err, apple_err] if e])
    return apple_events, "apple", combined_err, {}


def read_calendar_events_cached(ttl_sec: int = CALENDAR_CACHE_TTL_SEC) -> Tuple[list[dict], str, str, dict]:
    now = int(time.time())
    with _calendar_cache_lock:
        if now - int(_calendar_cache.get("ts", 0)) < ttl_sec:
            return (
                list(_calendar_cache.get("events", [])),
                str(_calendar_cache.get("source", "none")),
                str(_calendar_cache.get("error", "")),
                dict(_calendar_cache.get("meta", {}) or {}),
            )

    events, src, err, meta = _fetch_calendar_events()
    with _calendar_cache_lock:
        _calendar_cache["ts"] = now
        _calendar_cache["events"] = events
        _calendar_cache["source"] = src
        _calendar_cache["error"] = err
        _calendar_cache["meta"] = meta or {}
        return list(events), src, err, dict(meta or {})


def _google_list_tasks(access_token: str, tasklist_id: str, max_results: int = 50) -> list[dict]:
    tl = urllib.parse.quote(tasklist_id, safe="")
    q = urllib.parse.urlencode(
        {
            "showCompleted": "false",
            "showDeleted": "false",
            "showHidden": "false",
            "maxResults": str(max_results),
            "fields": "items(id,title,notes,due,status,updated)",
        }
    )
    url = f"https://tasks.googleapis.com/tasks/v1/lists/{tl}/tasks?{q}"
    data = _http_get_json(url, headers={"Authorization": f"Bearer {access_token}"})

    out: list[dict] = []
    for item in data.get("items", []) or []:
        if str(item.get("status") or "").lower() == "completed":
            continue
        tid = (item.get("id") or "").strip()
        title = (item.get("title") or "").strip() or "(no title)"
        notes = (item.get("notes") or "").strip()
        due = (item.get("due") or "").strip()
        out.append({"id": tid, "title": title, "notes": notes, "due": due})
    return out


def _tasks_cache_bust() -> None:
    with _tasks_cache_lock:
        _tasks_cache["ts"] = 0


def _run_google_tasks() -> Tuple[list[dict], str]:
    tok = _read_google_user_oauth_token()
    if not tok:
        return [], "google todo not configured (missing user_oauth_token.json)"
    missing = _google_user_missing_scopes(tok, ["https://www.googleapis.com/auth/tasks"])
    if missing:
        return [], "google oauth missing tasks scope (rerun setup_google_oauth.py)"

    try:
        token, _exp = _google_user_get_access_token()
        tasks = _google_list_tasks(token, GOOGLE_TASKLIST_ID, max_results=GOOGLE_TASKS_MAX * 2)
        return tasks, ""
    except Exception as e:
        return [], f"google todo error: {_http_error_message(e)}"


def read_google_tasks_cached(ttl_sec: int = GOOGLE_TASKS_CACHE_TTL_SEC) -> Tuple[list[dict], str]:
    now = int(time.time())
    with _tasks_cache_lock:
        if now - int(_tasks_cache.get("ts", 0)) < ttl_sec:
            return list(_tasks_cache.get("tasks", [])), str(_tasks_cache.get("error", ""))

    tasks, err = _run_google_tasks()
    with _tasks_cache_lock:
        _tasks_cache["ts"] = now
        _tasks_cache["tasks"] = tasks
        _tasks_cache["error"] = err
        return list(tasks), err


def _google_tasks_insert(access_token: str, tasklist_id: str, title: str, notes: str = "") -> dict:
    tl = urllib.parse.quote(tasklist_id, safe="")
    url = f"https://tasks.googleapis.com/tasks/v1/lists/{tl}/tasks"
    body = {"title": title}
    if notes:
        body["notes"] = notes
    return _http_request_json(url, "POST", headers={"Authorization": f"Bearer {access_token}"}, body_obj=body)


def _google_tasks_patch(access_token: str, tasklist_id: str, task_id: str, patch_obj: dict) -> dict:
    tl = urllib.parse.quote(tasklist_id, safe="")
    tid = urllib.parse.quote(task_id, safe="")
    url = f"https://tasks.googleapis.com/tasks/v1/lists/{tl}/tasks/{tid}"
    return _http_request_json(url, "PATCH", headers={"Authorization": f"Bearer {access_token}"}, body_obj=patch_obj)


def gtasks_add(title: str, notes: str = "") -> dict:
    tok = _read_google_user_oauth_token()
    if not tok:
        raise RuntimeError("google todo not configured (missing user_oauth_token.json)")
    missing = _google_user_missing_scopes(tok, ["https://www.googleapis.com/auth/tasks"])
    if missing:
        raise RuntimeError("google oauth missing tasks scope (rerun setup_google_oauth.py)")
    t = _sanitize_todo_text(title)
    if not t:
        raise ValueError("empty title")
    n = (notes or "").replace("\r\n", "\n").replace("\r", "\n")
    if len(n) > 2000:
        n = n[:2000]
    token, _exp = _google_user_get_access_token()
    try:
        out = _google_tasks_insert(token, GOOGLE_TASKLIST_ID, t, n)
    except Exception as e:
        raise RuntimeError(_http_error_message(e))
    _tasks_cache_bust()
    return out


def gtasks_complete(task_id: str) -> dict:
    tok = _read_google_user_oauth_token()
    if not tok:
        raise RuntimeError("google todo not configured (missing user_oauth_token.json)")
    missing = _google_user_missing_scopes(tok, ["https://www.googleapis.com/auth/tasks"])
    if missing:
        raise RuntimeError("google oauth missing tasks scope (rerun setup_google_oauth.py)")
    tid = (task_id or "").strip()
    if not tid:
        raise ValueError("empty id")
    token, _exp = _google_user_get_access_token()
    now_dt = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    now_z = now_dt.isoformat().replace("+00:00", "Z")
    try:
        out = _google_tasks_patch(token, GOOGLE_TASKLIST_ID, tid, {"status": "completed", "completed": now_z})
    except Exception as e:
        raise RuntimeError(_http_error_message(e))
    _tasks_cache_bust()
    return out


def gtasks_uncomplete(task_id: str) -> dict:
    tok = _read_google_user_oauth_token()
    if not tok:
        raise RuntimeError("google todo not configured (missing user_oauth_token.json)")
    missing = _google_user_missing_scopes(tok, ["https://www.googleapis.com/auth/tasks"])
    if missing:
        raise RuntimeError("google oauth missing tasks scope (rerun setup_google_oauth.py)")
    tid = (task_id or "").strip()
    if not tid:
        raise ValueError("empty id")
    token, _exp = _google_user_get_access_token()
    try:
        out = _google_tasks_patch(token, GOOGLE_TASKLIST_ID, tid, {"status": "needsAction", "completed": None})
    except Exception as e:
        raise RuntimeError(_http_error_message(e))
    _tasks_cache_bust()
    return out


def _env_bool(name: str, default: bool = False) -> bool:
    v = (os.environ.get(name) or "").strip().lower()
    if not v:
        return default
    return v not in ("0", "false", "no", "off")


def _parse_float(value: str) -> Optional[float]:
    try:
        return float(str(value).strip())
    except Exception:
        return None


def _weather_code_to_ja(code: int) -> str:
    try:
        c = int(code)
    except Exception:
        return "不明"

    if c == 0:
        return "晴れ"
    if c == 1:
        return "ほぼ晴れ"
    if c == 2:
        return "晴れ/くもり"
    if c == 3:
        return "くもり"
    if c in (45, 48):
        return "霧"
    if c in (51, 53, 55):
        return "霧雨"
    if c in (56, 57):
        return "着氷性霧雨"
    if c in (61, 63):
        return "雨"
    if c == 65:
        return "大雨"
    if c in (66, 67):
        return "みぞれ"
    if c in (71, 73, 77):
        return "雪"
    if c == 75:
        return "大雪"
    if c in (80, 81):
        return "にわか雨"
    if c == 82:
        return "激しいにわか雨"
    if c in (85, 86):
        return "にわか雪"
    if c == 95:
        return "雷雨"
    if c in (96, 99):
        return "ひょう雷雨"
    return "不明"


def _weather_geocode_city(city: str) -> Tuple[Optional[float], Optional[float], str, str]:
    q = (city or "").strip()
    if not q:
        return None, None, "", "empty city"

    now = int(time.time())
    with _weather_geo_cache_lock:
        cached = _weather_geo_cache.get(q)
        if cached and (now - int(cached.get("ts", 0)) < WEATHER_GEO_CACHE_TTL_SEC):
            return (
                _parse_float(cached.get("lat")),
                _parse_float(cached.get("lon")),
                str(cached.get("name") or ""),
                str(cached.get("error") or ""),
            )

    url = "https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode(
        {"name": q, "count": "1", "language": "ja", "format": "json"}
    )
    try:
        data = _http_get_json(url, headers={}, timeout_sec=WEATHER_TIMEOUT_SEC)
    except Exception as e:
        err = _http_error_message(e)
        with _weather_geo_cache_lock:
            _weather_geo_cache[q] = {"ts": now, "lat": None, "lon": None, "name": "", "error": err}
        return None, None, "", err

    results = data.get("results") or []
    if not isinstance(results, list) or not results:
        with _weather_geo_cache_lock:
            _weather_geo_cache[q] = {"ts": now, "lat": None, "lon": None, "name": "", "error": "not found"}
        return None, None, "", "not found"

    r0 = results[0] if isinstance(results[0], dict) else {}
    lat = _parse_float(r0.get("latitude"))
    lon = _parse_float(r0.get("longitude"))
    name = str(r0.get("name") or q).strip()
    admin1 = str(r0.get("admin1") or "").strip()
    country = str(r0.get("country") or "").strip()
    label = name
    if admin1 and admin1 not in label:
        label = f"{label} {admin1}".strip()
    if country and country not in label:
        label = f"{label} {country}".strip()

    err = "" if (lat is not None and lon is not None) else "invalid location"
    with _weather_geo_cache_lock:
        _weather_geo_cache[q] = {"ts": now, "lat": lat, "lon": lon, "name": label, "error": err}
    return lat, lon, label, err


def _weather_location() -> Tuple[float, float, str]:
    label = (os.environ.get("DESK_SCREEN_WEATHER_LABEL") or "").strip()
    city = (os.environ.get("DESK_SCREEN_WEATHER_CITY") or "").strip()
    lat = _parse_float(os.environ.get("DESK_SCREEN_WEATHER_LAT") or "")
    lon = _parse_float(os.environ.get("DESK_SCREEN_WEATHER_LON") or "")

    if lat is not None and lon is not None:
        name = label or city or "天気"
        return lat, lon, name

    if city:
        glat, glon, gname, _err = _weather_geocode_city(city)
        if glat is not None and glon is not None:
            return glat, glon, label or gname or city

    return WEATHER_DEFAULT_LAT, WEATHER_DEFAULT_LON, label or "東京"


def _fetch_weather(lat: float, lon: float, tz: str) -> Tuple[dict, str]:
    q = urllib.parse.urlencode(
        {
            "latitude": f"{lat:.6f}",
            "longitude": f"{lon:.6f}",
            "current": "temperature_2m,weather_code",
            "timezone": tz,
        }
    )
    url = f"https://api.open-meteo.com/v1/forecast?{q}"
    try:
        data = _http_get_json(url, headers={}, timeout_sec=WEATHER_TIMEOUT_SEC)
    except Exception as e:
        return {}, _http_error_message(e)

    cur = data.get("current")
    if not isinstance(cur, dict):
        cur = data.get("current_weather") if isinstance(data.get("current_weather"), dict) else {}

    temp = cur.get("temperature_2m")
    if temp is None:
        temp = cur.get("temperature")
    code = cur.get("weather_code")
    if code is None:
        code = cur.get("weathercode")
    obs_time = str(cur.get("time") or "").strip()

    try:
        temp_c = float(temp)
    except Exception:
        return {}, "invalid temperature"

    try:
        wcode = int(code)
    except Exception:
        wcode = -1

    return (
        {
            "ok": True,
            "temp_c": round(temp_c, 1),
            "temp_c_int": int(round(temp_c)),
            "code": wcode,
            "text": _weather_code_to_ja(wcode),
            "obs_time": obs_time,
        },
        "",
    )


def read_weather_cached(ttl_sec: int = WEATHER_CACHE_TTL_SEC) -> Tuple[dict, str]:
    if not _env_bool("DESK_SCREEN_WEATHER_ENABLE", True):
        return {"enabled": False}, ""

    tz = (os.environ.get("DESK_SCREEN_WEATHER_TZ") or WEATHER_DEFAULT_TZ).strip() or WEATHER_DEFAULT_TZ
    lat, lon, name = _weather_location()
    key = f"{lat:.6f},{lon:.6f},{tz}"

    now = int(time.time())
    with _weather_cache_lock:
        if _weather_cache.get("key") == key and (now - int(_weather_cache.get("ts", 0)) < ttl_sec):
            w = dict(_weather_cache.get("weather") or {})
            err = str(_weather_cache.get("error") or "")
            if w:
                w["enabled"] = True
                w.setdefault("name", name)
            return w or {"enabled": True, "name": name}, err

        prev = dict(_weather_cache.get("weather") or {}) if _weather_cache.get("key") == key else {}

    w, err = _fetch_weather(lat, lon, tz)
    if not w and prev:
        w = prev
    w = dict(w or {})
    w["enabled"] = True
    w.setdefault("name", name)
    w["updated"] = now

    with _weather_cache_lock:
        _weather_cache["ts"] = now
        _weather_cache["key"] = key
        _weather_cache["weather"] = w
        _weather_cache["error"] = err
    return dict(w), err


def fmt_hm(epoch_sec: int) -> str:
    t = time.localtime(epoch_sec)
    return f"{t.tm_hour:02d}:{t.tm_min:02d}"


def fmt_md(epoch_sec: int) -> str:
    t = time.localtime(epoch_sec)
    return f"{t.tm_mon:02d}/{t.tm_mday:02d}"


def _day_start_epoch(epoch_sec: int) -> int:
    t = time.localtime(epoch_sec)
    try:
        return int(time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1)))
    except Exception:
        return int(epoch_sec)


def build_payload() -> dict:
    now = int(time.time())
    events_raw, cal_src, cal_err, cal_meta = read_calendar_events_cached()
    gtasks_raw, gtasks_err = read_google_tasks_cached()
    weather, weather_err = read_weather_cached()
    todo_token_required = bool(_todo_expected_token())

    cal_mode = str((cal_meta or {}).get("mode") or "")
    try:
        cal_ids_count = int((cal_meta or {}).get("ids_count") or 0)
    except Exception:
        cal_ids_count = 0

    events: list[dict] = []
    for ev in events_raw[:MAX_EVENTS]:
        all_day = bool(ev.get("all_day"))
        start_epoch = int(ev["start"])
        end_epoch = int(ev["end"])
        events.append(
            {
                "date": fmt_md(start_epoch),
                "date_epoch": _day_start_epoch(start_epoch),
                "start_epoch": start_epoch,
                "end_epoch": end_epoch,
                "all_day": all_day,
                "start": "終日" if all_day else fmt_hm(start_epoch),
                "end": "" if all_day else fmt_hm(end_epoch),
                "title": ev["title"],
                "location": ev["location"],
                }
            )

    gtasks: list[dict] = []
    for t in gtasks_raw[:GOOGLE_TASKS_MAX]:
        due_epoch = _parse_rfc3339_epoch(str(t.get("due") or ""))
        gtasks.append(
            {
                "id": (t.get("id") or "").strip(),
                "title": (t.get("title") or "").strip(),
                "notes": (t.get("notes") or "").strip(),
                "due_date": fmt_md(due_epoch) if due_epoch else "",
                "due_time": fmt_hm(due_epoch) if due_epoch else "",
            }
        )

    return {
        "now": now,
        "ui_rev": UI_REV,
        "weather": weather,
        "weather_error": weather_err,
        "todo": read_todo(),
        "gtasks": gtasks,
        "events": events,
        "calendar_days": calendar_lookahead_days(),
        "calendar_mode": cal_mode,
        "calendar_ids_count": cal_ids_count,
        "calendar_source": cal_src,
        "calendar_error": cal_err,
        "gtasks_error": gtasks_err,
        "todo_token_required": todo_token_required,
    }


def _demo_month_days(year: int, month: int) -> int:
    try:
        if month == 12:
            nm = datetime.datetime(year + 1, 1, 1)
        else:
            nm = datetime.datetime(year, month + 1, 1)
        thism = datetime.datetime(year, month, 1)
        return int((nm - thism).days)
    except Exception:
        return 30


def _demo_local_epoch(dt: datetime.datetime) -> int:
    # `time.mktime` interprets tuple as local time (good for this app's display).
    try:
        return int(time.mktime(dt.timetuple()))
    except Exception:
        try:
            return int(dt.timestamp())
        except Exception:
            return int(time.time())


def build_demo_payload() -> dict:
    now = int(time.time())
    now_dt = datetime.datetime.fromtimestamp(now).replace(second=0, microsecond=0)
    year, month, day = now_dt.year, now_dt.month, now_dt.day
    dim = _demo_month_days(year, month)

    def clamp_day(d: int) -> int:
        if d < 1:
            return 1
        if d > dim:
            return dim
        return d

    events: list[dict] = []

    def add_timed(d: int, hh: int, mm: int, dur_min: int, title: str, location: str = "") -> None:
        start_dt = datetime.datetime(year, month, clamp_day(d), hh, mm)
        end_dt = start_dt + datetime.timedelta(minutes=int(dur_min))
        start_epoch = _demo_local_epoch(start_dt)
        end_epoch = _demo_local_epoch(end_dt)
        events.append(
            {
                "date": fmt_md(start_epoch),
                "date_epoch": _day_start_epoch(start_epoch),
                "start_epoch": start_epoch,
                "end_epoch": end_epoch,
                "all_day": False,
                "start": fmt_hm(start_epoch),
                "end": fmt_hm(end_epoch),
                "title": title,
                "location": location,
            }
        )

    def add_allday(d: int, title: str) -> None:
        start_dt = datetime.datetime(year, month, clamp_day(d), 0, 0)
        start_epoch = _demo_local_epoch(start_dt)
        end_epoch = start_epoch + 86400
        events.append(
            {
                "date": fmt_md(start_epoch),
                "date_epoch": _day_start_epoch(start_epoch),
                "start_epoch": start_epoch,
                "end_epoch": end_epoch,
                "all_day": True,
                "start": "終日",
                "end": "",
                "title": title,
                "location": "",
            }
        )

    # Ensure at least one "next" event exists in the current month.
    next_hour = (now_dt + datetime.timedelta(hours=1)).replace(minute=0)
    if next_hour.month != month or next_hour.year != year:
        next_hour = datetime.datetime(year, month, clamp_day(day), 20, 0)
    add_timed(next_hour.day, next_hour.hour, next_hour.minute, 45, "デモ：次の予定", "オンライン")

    # A few nice-looking sample events across the month.
    add_timed(clamp_day(day + 1), 10, 0, 30, "チーム定例", "Zoom")
    add_timed(clamp_day(day + 1), 13, 0, 60, "デザインレビュー", "会議室A")
    add_allday(clamp_day(day + 3), "集中作業（終日）")
    add_timed(clamp_day(day + 4), 19, 0, 60, "運動", "")

    # Fixed anchors (safe within month) so the month grid looks populated.
    add_timed(5, 9, 30, 20, "朝会", "")
    add_timed(12, 15, 0, 60, "振り返り", "")
    add_allday(20, "締切")
    add_timed(26, 11, 0, 30, "1on1", "")

    # Demo tasks (Google tab) + Local todo (Local tab).
    def due_epoch(days_ahead: int, hh: int, mm: int) -> int:
        dt = (now_dt + datetime.timedelta(days=int(days_ahead))).replace(hour=hh, minute=mm)
        return _demo_local_epoch(dt)

    gtasks = [
        {"id": "demo-1", "title": "提案資料のたたき台", "notes": "", "due_date": fmt_md(due_epoch(1, 18, 0)), "due_time": "18:00"},
        {"id": "demo-2", "title": "来週の予定調整", "notes": "", "due_date": fmt_md(due_epoch(2, 12, 0)), "due_time": "12:00"},
        {"id": "demo-3", "title": "買い出しリスト更新", "notes": "", "due_date": fmt_md(due_epoch(3, 9, 0)), "due_time": "09:00"},
        {"id": "demo-4", "title": "請求書チェック", "notes": "", "due_date": fmt_md(due_epoch(4, 17, 0)), "due_time": "17:00"},
        {"id": "demo-5", "title": "週次レビュー", "notes": "", "due_date": fmt_md(due_epoch(5, 10, 0)), "due_time": "10:00"},
    ]

    weather_name = (
        (os.environ.get("DESK_SCREEN_WEATHER_LABEL") or "")
        or (os.environ.get("DESK_SCREEN_WEATHER_CITY") or "")
        or "福岡市 中央区"
    ).strip() or "福岡市 中央区"

    weather = {
        "enabled": True,
        "ok": True,
        "name": weather_name,
        "temp_c": 12.3,
        "temp_c_int": 12,
        "code": 2,
        "text": "くもり",
        "obs_time": now_dt.isoformat(),
        "updated": now,
    }

    return {
        "now": now,
        "ui_rev": UI_REV,
        "weather": weather,
        "weather_error": "",
        "todo": ["デモ：ゴミ出し", "デモ：水やり", "デモ：メール返信", "デモ：ストレッチ", "デモ：読書"],
        "gtasks": gtasks,
        "events": events,
        "calendar_days": calendar_lookahead_days(),
        "calendar_mode": "demo",
        "calendar_ids_count": 0,
        "calendar_source": "demo",
        "calendar_error": "",
        "gtasks_error": "",
        "todo_token_required": False,
    }


INDEX_HTML = f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#060a11">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>Desk Screen</title>
<style>
  :root {{
    --bg: #060a11;
    --bg2: #0b1220;
    --card: #0f1b2e;
    --card2: #0a1324;
    --bd: rgba(255,255,255,.10);
    --bd2: rgba(255,255,255,.16);
    --fg: #e7eefc;
    --muted: #a8b8d3;
    --accent: #5eead4;
    --accent2: #a78bfa;
    --warn: #fbbf24;
  }}
  *, *:before, *:after {{ box-sizing: border-box; }}
  html, body {{ height:100%; }}
	  body {{
	    margin:0;
	    font-family: -apple-system,BlinkMacSystemFont,"Helvetica Neue",Arial,"Hiragino Kaku Gothic ProN","Noto Sans JP",sans-serif;
	    background:
        radial-gradient(900px 600px at -10% -10%, rgba(94,234,212,.14), transparent 60%),
        radial-gradient(900px 600px at 110% 15%, rgba(167,139,250,.11), transparent 55%),
        radial-gradient(800px 560px at 60% 120%, rgba(96,165,250,.08), transparent 55%),
        linear-gradient(180deg, var(--bg2), var(--bg));
	    background-color:var(--bg);
	    color:#e7eefc;
	    color:var(--fg);
      color-scheme: dark;
      -webkit-font-smoothing: antialiased;
      text-rendering: optimizeLegibility;
	    min-height: calc(100vh + 2px);
	  }}
  body::-webkit-scrollbar {{ width:0; height:0; }}

	  .wrap {{
	    position:fixed;
	    top:0; left:0; right:0; bottom:0;
	    padding:14px;
	    box-sizing:border-box;
	    display:flex;
	    flex-direction:column;
	  }}

  .topbar {{ display:flex; align-items:center; flex-wrap:wrap; margin-bottom:12px; }}
  .topbar > * {{ margin-right:10px; }}
  .topbar > *:last-child {{ margin-right:0; }}
  .clockbox {{ display:flex; flex-direction:column; line-height:1; }}
  .now {{ font-size:34px; font-weight:900; letter-spacing:1px; font-variant-numeric: tabular-nums; }}
	  .date {{ font-size:13px; color:#b8c6da; color:var(--muted); margin-top:4px; display:flex; flex-wrap:wrap; gap:10px; align-items:baseline; }}
	  .date .sep {{ opacity:.55; }}
	  .date .wx {{ color:#e7eefc; color:var(--fg); font-weight:800; }}
	  .date .wx.next {{
	    padding:2px 8px;
	    border-radius:999px;
	    border:1px solid rgba(167,139,250,.35);
	    background:rgba(167,139,250,.10);
	    font-weight:900;
	    letter-spacing:.2px;
	    min-width:0;
	    max-width:56vw;
	    max-width:min(56vw, 620px);
	    overflow:hidden;
	    text-overflow:ellipsis;
	    white-space:nowrap;
	  }}
	  .date .wx.next.nowev {{
	    border-color:rgba(94,234,212,.55);
	    background:rgba(94,234,212,.14);
	  }}
	  .date .wx.next.allday {{
	    border-color:rgba(251,191,36,.60);
	    background:rgba(251,191,36,.14);
	  }}
	  .date .wx.next.nonev {{
	    border-color:rgba(148,163,184,.22);
	    background:rgba(0,0,0,.10);
	    color:var(--muted);
	    font-weight:800;
	  }}
  .sp {{ flex:1; min-width:10px; }}

	  .btn {{
	    padding:10px 12px;
	    background:rgba(255,255,255,.06);
      background:linear-gradient(180deg, rgba(255,255,255,.08), rgba(255,255,255,.04));
	    color:#e7eefc;
	    color:var(--fg);
	    border:1px solid var(--bd2);
	    border-radius:12px;
	    text-decoration:none;
	    display:inline-block;
	    white-space:nowrap;
	  }}
  .btn:active {{ opacity:0.92; transform: translateY(1px); }}
  .btn.sm {{ padding:6px 10px; border-radius:10px; font-size:12px; }}

	  .muted {{ color:var(--muted); font-size:13px; }}
	  .status {{ max-width:520px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}

	  .main {{
	    flex:1;
	    min-height:0;
	    display:flex;
	  }}
		  @media (max-width: 720px) {{
		    /* Phones: allow natural page scroll. (Tablets keep 2-column layout.) */
		    .wrap {{
		      position:relative;
		      top:auto; left:auto; right:auto; bottom:auto;
		      min-height:100vh;
		    }}
		    .main {{ display:block; }}
		    .topbar > * {{ margin-bottom:10px; }}
		    .col.left {{ margin-right:0; }}
		    .col.right {{ margin-top:12px; }}
		    .row > * {{ margin-bottom:10px; }}
		  }}
		  .col {{ display:flex; flex-direction:column; min-height:0; min-width:0; flex:1; }}
		  .col.left {{ flex:1; }}
		  .col.right {{ flex:1.7; }}
		  .col.left {{ margin-right:12px; }}
		  .col > .card {{ margin-bottom:12px; }}
		  .col > .card:last-child {{ margin-bottom:0; }}

	  .card {{
	    background:linear-gradient(180deg, var(--card), var(--card2));
	    border:1px solid var(--bd);
	    border-radius:16px;
	    padding:12px;
	    overflow:hidden;
	    min-height:0;
    min-width:0;
	    flex:1;
	    display:flex;
	    flex-direction:column;
	  }}
		  .card.todo {{
		    flex: 1.1;
		    border-color: rgba(251,191,36,.42);
		    border-left: 5px solid rgba(251,191,36,.62);
		    background:
		      radial-gradient(720px 360px at 0% 0%, rgba(251,191,36,.10), transparent 62%),
		      linear-gradient(180deg, rgba(251,191,36,.06), rgba(94,234,212,.03)),
		      linear-gradient(180deg, var(--card), var(--card2));
		    box-shadow:
		      0 18px 46px rgba(0,0,0,.34),
		      0 0 0 1px rgba(251,191,36,.14),
		      0 0 34px rgba(251,191,36,.10);
		  }}
		  .card.todo .h {{ font-size:17px; }}
		  .card.todo .h:before {{
		    background:linear-gradient(180deg, var(--warn), var(--accent));
		  }}
		  .card.todo #todoHint {{
		    display:inline-block;
		    align-self:flex-start;
		    padding:3px 10px;
		    border-radius:999px;
		    border:1px solid rgba(251,191,36,.28);
		    background:rgba(251,191,36,.10);
		    color:var(--fg);
		    font-weight:900;
		    font-size:12px;
		  }}
		  .card.todo .item {{
		    margin:8px 0;
		    padding:10px 10px;
		    border:1px solid rgba(255,255,255,.11);
		    border-radius:14px;
		    background:linear-gradient(180deg, rgba(0,0,0,.18), rgba(0,0,0,.10));
		    box-shadow: 0 10px 22px rgba(0,0,0,.20);
		    border-bottom:none;
		  }}
		  .card.todo .chk {{
		    width:38px;
		    height:38px;
		    border-radius:14px;
		    border:1px solid rgba(251,191,36,.55);
		    background:rgba(251,191,36,.16);
		    box-shadow: 0 10px 22px rgba(0,0,0,.22);
		    font-size:16px;
		  }}
		  .card.todo .text .t {{ font-weight:900; font-size:16px; }}
		  .card.todo .text .s {{ font-size:13px; opacity:.92; }}
		  .card.memo {{ flex: 0.65; }}
		  .card.cal {{ flex: 1.7; }}
		  .head {{ display:flex; align-items:center; justify-content:space-between; margin-bottom:10px; }}
	  .h {{ font-size:16px; font-weight:900; margin:0; letter-spacing:.2px; }}
    .h:before {{
      content:'';
      display:inline-block;
      width:8px; height:8px;
      border-radius:999px;
      background:linear-gradient(180deg, var(--accent), var(--accent2));
      margin-right:8px;
      transform: translateY(-1px);
    }}

	  .tabs {{ display:flex; }}
	  .tabs .tab + .tab {{ margin-left:6px; }}
	  .tab {{
	    padding:6px 10px;
	    border-radius:999px;
	    border:1px solid var(--bd2);
	    background:transparent;
	    color:var(--muted);
	    font-size:12px;
	  }}
	  .tab.on {{
	    background:rgba(94,234,212,.16);
	    color:#e7eefc;
	    color:var(--fg);
	    border-color:rgba(94,234,212,.55);
	  }}

	  .row {{ display:flex; align-items:center; flex-wrap:wrap; }}
	  .row > * {{ margin-right:10px; }}
	  .row > *:last-child {{ margin-right:0; }}
	  .in {{
	    flex:1;
	    min-width: 180px;
	    padding:10px 12px;
	    border-radius:12px;
	    border:1px solid var(--bd2);
	    background:rgba(0,0,0,.12);
	    background:linear-gradient(180deg, rgba(0,0,0,.18), rgba(0,0,0,.10));
	    color:#e7eefc;
	    color:var(--fg);
	    font-size:14px;
	    outline:none;
	  }}
	  .ta {{
	    flex:1;
	    min-height:0;
	    width:100%;
	    padding:10px 12px;
	    border-radius:14px;
	    border:1px solid var(--bd2);
	    background:rgba(0,0,0,.12);
	    background:linear-gradient(180deg, rgba(0,0,0,.18), rgba(0,0,0,.10));
	    color:#e7eefc;
	    color:var(--fg);
	    font-size:14px;
	    line-height:1.35;
	    resize:none;
	    outline:none;
	  }}
	  .memoView {{
	    flex:1;
	    min-height:0;
	    width:100%;
	    padding:10px 12px;
	    border-radius:14px;
	    border:1px solid var(--bd2);
	    background:rgba(0,0,0,.12);
	    background:linear-gradient(180deg, rgba(0,0,0,.18), rgba(0,0,0,.10));
	    color:var(--fg);
	    font-size:14px;
	    line-height:1.35;
	    overflow:auto;
	    -webkit-overflow-scrolling:touch;
	    white-space:pre-wrap;
	    word-break:break-word;
	  }}
	  .memoView::-webkit-scrollbar {{ width:0; height:0; }}
  .hint {{ margin-top:8px; }}
  .list {{ flex:1; min-height:0; overflow:auto; -webkit-overflow-scrolling:touch; }}
  .list::-webkit-scrollbar {{ width:0; height:0; }}
	  body.kiosk #events.list {{ overflow:hidden; }}
	  body.kiosk .ev .loc {{ display:none; }}
	  body.kiosk #todoAddRow {{ display:none; }}
	  body.kiosk #todoQuickRow {{ display:none; }}
	  body.kiosk #memoEditRow {{ display:none; }}
	  body.kiosk #memoInput {{ display:none; }}
	  body.kiosk #nextEvents {{ display:none; }}
	  body.kiosk .card.todo {{ flex: 1.4; }}
	  body.kiosk .card.memo {{ flex: 0.45; }}
	  body.kiosk .card.todo .tabs {{ display:none; }}
	  body.kiosk .card.todo .head {{ margin-bottom:6px; }}
	  body.kiosk .card.todo #todoHint {{ display:none; }}
	  body.kiosk .card.todo .item {{ margin:4px 0; padding:6px 10px; align-items:center; }}
	  body.kiosk .card.todo .chk {{ width:32px; height:32px; border-radius:12px; font-size:15px; }}
	  body.kiosk .card.todo .text .t {{
	    font-size:15px;
	    line-height:1.12;
	    white-space:nowrap;
	    overflow:hidden;
	    text-overflow:ellipsis;
	  }}
	  body.kiosk .card.todo .text .s {{ display:none; }}
	  .caltabs .tab {{ padding:5px 9px; }}

  /* Month calendar (default) */
  .calMonthWrap {{ flex:1; min-height:0; display:flex; flex-direction:column; gap:8px; }}
  .monthTop {{ display:flex; align-items:center; gap:8px; }}
  .monthTitle {{ font-weight:900; font-size:16px; letter-spacing:.2px; }}
  .weekRow {{ display:grid; grid-template-columns:repeat(7, 1fr); gap:6px; padding:0 2px; }}
  .wk {{ text-align:center; font-size:12px; font-weight:900; color:var(--muted); }}
  .wk.sun {{ color: rgba(251, 113, 133, .95); }}
  .wk.sat {{ color: rgba(96, 165, 250, .95); }}
  .monthGrid {{ flex:1; min-height:0; display:grid; grid-template-columns:repeat(7, 1fr); grid-template-rows:repeat(6, 1fr); gap:6px; }}
  .day {{
    appearance:none;
    -webkit-appearance:none;
    border:1px solid var(--bd);
    border-radius:14px;
    padding:8px;
    background:rgba(255,255,255,.03);
    color:var(--fg);
    text-align:left;
    cursor:pointer;
    overflow:hidden;
    position:relative;
    display:flex;
    flex-direction:column;
    box-shadow: 0 10px 24px rgba(0,0,0,.22);
  }}
  .day:active {{ transform: translateY(1px); opacity:.95; }}
  .day.out {{ opacity:.35; }}
	  .day.today {{
	    border-width:2px;
	    border-color: rgba(94,234,212,.88);
	    background:
	      radial-gradient(360px 220px at 15% 12%, rgba(94,234,212,.22), transparent 60%),
	      linear-gradient(180deg, rgba(94,234,212,.16), rgba(94,234,212,.07));
	    box-shadow:
	      0 0 0 1px rgba(94,234,212,.26),
	      0 0 38px rgba(94,234,212,.18),
	      0 14px 30px rgba(0,0,0,.30);
	  }}
	  .day.today .num {{
	    color: rgba(94,234,212,.98);
	    font-size:15px;
	    text-shadow: 0 1px 0 rgba(0,0,0,.35);
	  }}
	  .day.sun .num {{ color: rgba(251, 113, 133, .95); }}
	  .day.sat .num {{ color: rgba(96, 165, 250, .95); }}
	  .day.holiday:not(.today) {{
	    border-color: rgba(251, 113, 133, .36);
	    background: rgba(251, 113, 133, .06);
	  }}
	  .day.holiday .num {{ color: rgba(251, 113, 133, .95); }}
	  .day .num {{ font-weight:900; font-size:14px; font-variant-numeric: tabular-nums; }}
	  .day .badges {{ position:absolute; top:6px; right:6px; display:flex; gap:4px; align-items:center; }}
	  .day .cnt {{
	    font-size:11px;
    padding:2px 7px;
    border-radius:999px;
    border:1px solid var(--bd2);
    background:rgba(0,0,0,.22);
    color:var(--fg);
    font-variant-numeric: tabular-nums;
  }}
  .day.busy {{ border-color: rgba(255,255,255,.14); }}
  body.kiosk .day .dots {{ display:none; }}
  .day .lines {{
    margin-top:6px;
    display:flex;
    flex-direction:column;
    gap:4px;
    min-height:0;
  }}
  .day .line {{
    display:flex;
    gap:6px;
    align-items:baseline;
    overflow:hidden;
  }}
  .day .line .lt {{
    flex:0 0 auto;
    padding:1px 6px;
    border-radius:999px;
    border:1px solid rgba(94,234,212,.35);
    background:rgba(94,234,212,.08);
    color:rgba(94,234,212,.95);
    font-weight:900;
    font-size:11px;
    line-height:1.1;
    font-variant-numeric: tabular-nums;
    letter-spacing:.2px;
	  }}
  .day .line.allday .lt {{
    border-color:rgba(251,191,36,.45);
    background:rgba(251,191,36,.10);
    color:rgba(251,191,36,.95);
	  }}
  .day .line .ln {{
    flex:1;
    min-width:0;
    white-space:nowrap;
    overflow:hidden;
    text-overflow:ellipsis;
    font-size:12px;
    line-height:1.15;
    font-weight:800;
    opacity:.92;
	  }}
  .day .dots {{ margin-top:auto; display:flex; gap:4px; flex-wrap:wrap; align-items:center; }}
  .dot {{ width:6px; height:6px; border-radius:999px; background:rgba(94,234,212,.9); box-shadow:0 0 0 2px rgba(0,0,0,.16); }}
  .dot.allday {{ background:rgba(251,191,36,.95); }}
  .dot.more {{
    width:auto;
    height:auto;
    padding:1px 6px;
    border-radius:999px;
    border:1px solid var(--bd2);
    background:rgba(0,0,0,.16);
    color:var(--muted);
    font-size:11px;
    font-weight:900;
  }}
  .mini {{ border-top:1px solid var(--bd); padding-top:8px; }}
  .miniTitle {{ font-weight:900; color:var(--muted); font-size:12px; letter-spacing:.2px; }}
  .miniList {{ margin-top:6px; display:flex; flex-direction:column; gap:6px; }}
  .miniEv {{ display:flex; gap:8px; align-items:flex-start; }}
  .miniEv .body {{ flex:1; min-width:0; }}
  .miniEv .t {{ font-weight:800; font-size:14px; line-height:1.25; word-break:break-word; }}
  .miniEv .s {{ color:var(--muted); font-size:12px; line-height:1.25; margin-top:2px; word-break:break-word; }}

	  .item {{
	    display:flex;
	    align-items:flex-start;
	    padding:8px 0;
	    border-bottom:1px solid var(--bd);
	  }}
	  .item:last-child {{ border-bottom:none; }}
	  .chk {{
	    width:32px; height:32px;
	    border-radius:12px;
	    border:1px solid rgba(94,234,212,.28);
	    background:rgba(94,234,212,.10);
	    color:#e7eefc;
	    color:var(--fg);
	    font-weight:900;
	    margin-right:10px;
	  }}
	  .text {{ flex:1; min-width:0; }}
	  .text .t {{ font-weight:800; font-size:15px; line-height:1.3; word-break:break-word; }}
	  .text .s {{ color:#a9b6c7; color:var(--muted); font-size:13px; margin-top:4px; line-height:1.3; white-space:pre-wrap; word-break:break-word; }}
	  .pill {{
	    display:inline-block;
	    padding:2px 8px;
	    border-radius:999px;
	    border:1px solid var(--bd2);
	    color:var(--muted);
	    font-size:11px;
	    margin-right:6px;
	  }}

	  .ev {{
	    display:flex;
	    align-items:flex-start;
	    gap:10px;
	    padding:10px 10px;
	    margin:8px 0;
	    border-radius:14px;
	    border:1px solid var(--bd);
	    border-left:3px solid rgba(94,234,212,.55);
	    background:rgba(255,255,255,.04);
	    box-shadow: 0 10px 24px rgba(0,0,0,.28);
	  }}
	  .ev.allDay {{ border-left-color: rgba(251,191,36,.72); }}
	  .ev .meta {{ width:96px; flex:0 0 96px; display:flex; align-items:flex-start; }}
	  .ev .timepill {{
	    display:inline-block;
	    padding:5px 9px;
	    border-radius:999px;
	    border:1px solid rgba(94,234,212,.45);
	    background:rgba(94,234,212,.12);
	    color:var(--fg);
	    font-weight:900;
	    font-size:13px;
	    line-height:1;
	    font-variant-numeric: tabular-nums;
	    letter-spacing:.2px;
	  }}
	  .ev.allDay .timepill {{
	    border-color:rgba(251,191,36,.55);
	    background:rgba(251,191,36,.16);
	  }}
	  .timepill {{
	    display:inline-block;
	    padding:5px 9px;
	    border-radius:999px;
	    border:1px solid rgba(94,234,212,.45);
	    background:rgba(94,234,212,.12);
	    color:var(--fg);
	    font-weight:900;
	    font-size:13px;
	    line-height:1;
	    font-variant-numeric: tabular-nums;
	    letter-spacing:.2px;
	  }}
	  .timepill.allday {{
	    border-color:rgba(251,191,36,.55);
	    background:rgba(251,191,36,.16);
	  }}
	  .ev .body {{ flex:1; min-width:0; padding-top:1px; }}
	  .ev > div:last-child {{ flex:1; min-width:0; }}
	  .ev .title {{ font-weight:900; font-size:16px; line-height:1.25; word-break:break-word; }}
	  .ev .loc {{ color:var(--muted); font-size:13px; margin-top:4px; line-height:1.25; word-break:break-word; }}
	  .ev-day {{
	    margin:10px 0 6px;
	    padding:10px 12px;
	    border-radius:14px;
	    border:1px solid var(--bd);
	    background:rgba(255,255,255,.04);
	    color:var(--fg);
	    font-size:14px;
	    font-weight:900;
	    letter-spacing:.2px;
	  }}
		  .ev-day.today {{
		    border-width:2px;
		    border-color:rgba(94,234,212,.78);
		    background:
		      radial-gradient(520px 240px at 20% 10%, rgba(94,234,212,.20), transparent 60%),
		      linear-gradient(180deg, rgba(94,234,212,.16), rgba(94,234,212,.08));
		    box-shadow:
		      0 0 0 1px rgba(94,234,212,.22),
		      0 0 28px rgba(94,234,212,.14);
		  }}

	  .overlay {{
	    position:fixed; top:0; left:0; right:0; bottom:0;
	    display:none;
    align-items:center;
    justify-content:center;
    background:rgba(0,0,0,.65);
    z-index:9999;
  }}
	  .overlay .box {{
	    max-width:420px;
	    margin:20px;
	    padding:18px;
	    border-radius:16px;
	    background:linear-gradient(180deg, var(--card), var(--card2));
	    border:1px solid var(--bd2);
	    text-align:center;
	  }}
	  .overlay .big {{ font-size:18px; font-weight:900; margin-bottom:8px; }}
	  .overlay .small {{ color:#a9b6c7; color:var(--muted); font-size:12px; line-height:1.4; }}
    #dayOverlay .box {{ max-width:560px; text-align:left; }}
    #dayOverlay .big {{ display:flex; align-items:baseline; justify-content:space-between; gap:10px; margin-bottom:10px; }}
    #dayOverlay .dayList {{ margin-top:10px; max-height:60vh; overflow:auto; -webkit-overflow-scrolling:touch; }}
    #dayOverlay .dayList::-webkit-scrollbar {{ width:0; height:0; }}
    .dayItem {{ display:flex; gap:10px; padding:10px 0; border-bottom:1px solid var(--bd); }}
    .dayItem:last-child {{ border-bottom:none; }}
    .dayItem .body {{ flex:1; min-width:0; }}
    .dayItem .t {{ font-weight:900; font-size:15px; line-height:1.25; word-break:break-word; }}
    .dayItem .s {{ color:var(--muted); font-size:12px; line-height:1.25; margin-top:3px; word-break:break-word; }}

  .toast {{
    position:fixed;
    left:0; right:0; bottom:0;
    padding:12px;
    z-index:9998;
    display:flex;
    justify-content:center;
    pointer-events:none;
  }}
	  .toast-inner {{
	    pointer-events:auto;
	    width:calc(100% - 24px);
	    max-width:720px;
	    background:rgba(10,14,26,.92);
	    border:1px solid var(--bd2);
	    border-radius:16px;
	    padding:10px 12px;
	    display:flex;
	    align-items:center;
	    transform: translateY(120%);
	    opacity:0;
	    transition: transform .18s ease, opacity .18s ease;
	  }}
	  .toast-inner button {{ margin-left:10px; }}
  .toast.show .toast-inner {{
    transform: translateY(0);
    opacity:1;
  }}
	  .toast-msg {{ flex:1; min-width:0; font-size:13px; color:#e8eef6; color:var(--fg); }}

  body.fs .topbar {{
    position:fixed;
    top:0; left:0; right:0;
    z-index:1000;
    background:rgba(6,10,17,.82);
    padding:12px 14px;
    backdrop-filter: blur(6px);
    -webkit-backdrop-filter: blur(6px);
    transition: transform .2s ease, opacity .2s ease;
  }}
  body.fs .main {{
    padding-top: 78px;
    transition: padding-top .2s ease;
  }}
	  /* Fullscreen: keep time/date/weather visible, hide controls. */
	  body.fs.hidebar .topbar {{
	    transform:none;
	    opacity:1;
	    pointer-events:auto;
	    padding:10px 14px;
	    background:rgba(6,10,17,.64);
	  }}
	  body.fs.hidebar .topbar a.btn {{ display:none; }}
	  body.fs.hidebar .topbar #status {{ display:none; }}
	  body.fs.hidebar .main {{ padding-top: 56px; }}

    /* Anti burn-in micro shift (fullscreen only) */
    @keyframes drift {{
      0% {{ transform: translate(0,0); }}
      25% {{ transform: translate(1px,0); }}
      50% {{ transform: translate(1px,1px); }}
      75% {{ transform: translate(0,1px); }}
      100% {{ transform: translate(0,0); }}
    }}
    body.fs #app {{
      animation: drift 240s steps(1,end) infinite;
    }}

	  /* d-01G（1280x800 / 16:10）を想定した密度調整 */
	  @media (min-width: 1200px) and (max-width: 1400px) and (max-height: 900px) {{
	    .wrap {{ padding:12px; }}
	    .card {{ padding:10px; }}
	    .now {{ font-size:32px; }}
	    .col.left {{ margin-right:10px; }}
	    .col > .card {{ margin-bottom:10px; }}
	  }}

	  /* iPhone 12 mini など小画面向け */
	  @media (max-width: 420px) {{
	    .wrap {{ padding:10px; }}
	    .now {{ font-size:26px; }}
	    .btn.sm {{ padding:6px 8px; }}
	    .status {{ max-width: 100%; }}
	  }}
	</style>
</head>
<body>
<div class="wrap" id="app">
  <div class="topbar">
    <div class="clockbox">
      <div class="now" id="nowTime">--:--</div>
      <div class="date">
        <span id="nowDate">----</span>
        <span class="sep">・</span>
        <span id="wxText" class="wx">天気 --</span>
        <span class="sep">・</span>
        <span id="nextText" class="wx next">次 --</span>
      </div>
    </div>
    <a class="btn sm" href="/">予定+ToDo</a>
    <a class="btn sm" href="/timer">時計+タイマー</a>
    <div class="sp"></div>
    <button class="btn sm" id="fsBtn" onclick="toggleFullscreen()">全画面</button>
    <span id="status" class="muted status"></span>
  </div>

	  <div class="main" id="main">
	    <div class="col left">
	      <div class="card todo">
	        <div class="head">
	          <h2 class="h">ToDo</h2>
	          <div class="tabs">
            <button class="tab" id="tabGoogle" onclick="setTodoMode('google')">Google</button>
            <button class="tab" id="tabLocal" onclick="setTodoMode('local')">Local</button>
          </div>
        </div>
	        <div class="row" id="todoAddRow">
	          <input id="todoInput" class="in" placeholder="ToDoを追加（Enterで追加）" />
	          <button class="btn sm" onclick="addTodo('')">追加</button>
	        </div>
	        <div class="row" id="todoQuickRow" style="margin-top:8px;">
	          <button class="btn sm" onclick="addTodo('今日：')">今日</button>
	          <button class="btn sm" onclick="addTodo('待ち：')">待ち</button>
	          <button class="btn sm" onclick="addTodo('いつか：')">いつか</button>
	          <span id="todoMsg" class="muted"></span>
	        </div>
	        <div id="todoHint" class="muted hint">自動更新：{REFRESH_SEC}秒</div>
	        <div class="list" id="todoList"></div>
	      </div>
	
		      <div class="card memo">
		        <div class="head">
		          <h2 class="h">メモ</h2>
		          <span class="muted" id="memoMsg"></span>
	        </div>
	        <div class="row" id="memoEditRow" style="margin-bottom:8px;">
	          <button class="btn sm" id="memoEditBtn" onclick="memoStartEdit()">編集</button>
	          <button class="btn sm" id="memoSaveBtn" onclick="memoSave()" style="display:none;">保存</button>
	          <button class="btn sm" id="memoCancelBtn" onclick="memoCancel()" style="display:none;">取消</button>
	          <span class="muted" id="memoHint"></span>
	        </div>
	        <textarea id="memoInput" class="ta" style="display:none;" placeholder="メモ（PC/スマホから編集できます）"></textarea>
	        <div id="memoView" class="memoView"></div>
		      </div>
	    </div>
	
		    <div class="col right">
		      <div class="card cal">
		        <div class="head">
              <div style="display:flex; align-items:baseline; gap:10px; min-width:0;">
                <h2 class="h">カレンダー</h2>
                <div class="tabs caltabs">
                  <button class="tab" id="calTabMonth" onclick="setCalView('month')">月</button>
                  <button class="tab" id="calTabAgenda" onclick="setCalView('agenda')">一覧</button>
                </div>
              </div>
		          <span class="muted" id="calHint"></span>
	        </div>
            <div id="calMonthWrap" class="calMonthWrap">
              <div class="monthTop">
                <div class="monthTitle" id="monthTitle">----</div>
                <div class="sp"></div>
                <button class="btn sm" onclick="monthNav(-1)">◀</button>
                <button class="btn sm" onclick="monthNav(0)">今月</button>
                <button class="btn sm" onclick="monthNav(1)">▶</button>
              </div>
              <div class="weekRow" aria-hidden="true">
                <div class="wk sun">日</div>
                <div class="wk">月</div>
                <div class="wk">火</div>
                <div class="wk">水</div>
                <div class="wk">木</div>
                <div class="wk">金</div>
                <div class="wk sat">土</div>
              </div>
              <div class="monthGrid" id="monthGrid"></div>
              <div class="mini" id="nextEvents"></div>
            </div>
			        <div class="list" id="events"></div>
			      </div>
		    </div>
		  </div>
		</div>

<div id="fsOverlay" class="overlay" onclick="overlayTap()">
  <div class="box">
    <div class="big">タップで全画面</div>
    <div class="small">全画面にするとアドレスバーが消えて安定します。</div>
    <div class="small" style="margin-top:10px;">もう一度タップで解除（戻る/ESCでもOK）</div>
  </div>
</div>

<div id="toast" class="toast" onclick="hideToast()">
  <div class="toast-inner">
    <div id="toastMsg" class="toast-msg"></div>
    <button id="toastBtn" class="btn sm" style="display:none;"></button>
  </div>
</div>

<div id="dayOverlay" class="overlay" onclick="closeDayOverlay()">
  <div class="box" onclick="event.stopPropagation()">
    <div class="big">
      <div id="dayTitle">----</div>
      <button class="btn sm" onclick="closeDayOverlay()">閉じる</button>
    </div>
    <div class="small" id="daySub"></div>
    <div class="dayList" id="dayEvents"></div>
  </div>
</div>

<script>
function pad(n){{ return (n<10?'0':'')+n; }}
function tickClock(){{
  var d = new Date();
  document.getElementById('nowTime').textContent = pad(d.getHours()) + ':' + pad(d.getMinutes());
  document.getElementById('nowDate').textContent = (d.getMonth()+1) + '/' + d.getDate() + ' (' + '日月火水木金土'[d.getDay()] + ')';
}}
setInterval(tickClock, 1000 * 5);
tickClock();

function qparam(name){{
  var m = location.search.match(new RegExp('(?:\\\\?|&)' + name + '=([^&]+)'));
  return m ? decodeURIComponent(m[1].replace(/\\+/g, ' ')) : '';
}}
function getToken(){{
  var t = '';
  try{{ t = localStorage.getItem('desk_token') || ''; }}catch(e){{}}
  if (!t){{
    t = String((new Date()).getTime());
    try{{ localStorage.setItem('desk_token', t); }}catch(e){{}}
  }}
  return t;
}}
(function(){{
  var t = qparam('token');
  if (t){{
    try{{ localStorage.setItem('desk_token', t); }}catch(e){{}}
    if (history && history.replaceState) history.replaceState(null, '', location.pathname);
  }}
}})();

function _truthy(v){{
  v = (v || '').toString().trim().toLowerCase();
  return (v === '1' || v === 'true' || v === 'yes' || v === 'on');
}}
var demoMode = false;
try{{
  demoMode = _truthy(qparam('demo'));
  if (demoMode) document.body.classList.add('demo');
}}catch(e){{ demoMode = false; }}
function withDemoUrl(url){{
  if (!demoMode) return url;
  try{{ if ((url || '').indexOf('demo=') >= 0) return url; }}catch(e){{}}
  return url + ((url || '').indexOf('?') >= 0 ? '&' : '?') + 'demo=1';
}}

// toast (no dialogs: fullscreen-friendly)
var toastTimer = 0;
function showToast(msg, actionText, actionFn, timeoutMs){{
  var wrap = document.getElementById('toast');
  var m = document.getElementById('toastMsg');
  var b = document.getElementById('toastBtn');
  if (!wrap || !m || !b) return;
  m.textContent = msg || '';
  if (actionText && actionFn){{
    b.style.display = 'inline-block';
    b.textContent = actionText;
    b.onclick = function(ev){{ ev.stopPropagation(); hideToast(); actionFn(); }};
  }} else {{
    b.style.display = 'none';
    b.onclick = null;
  }}
  wrap.classList.add('show');
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(hideToast, timeoutMs || 2500);
}}
function hideToast(){{
  var wrap = document.getElementById('toast');
  if (!wrap) return;
  wrap.classList.remove('show');
}}

// fullscreen
function isFullscreen(){{
  return !!(document.fullscreenElement || document.webkitFullscreenElement || document.mozFullScreenElement || document.msFullscreenElement);
}}
function requestFullscreen(){{
  var el = document.documentElement;
  if (el.requestFullscreen) return el.requestFullscreen();
  if (el.webkitRequestFullscreen) return el.webkitRequestFullscreen();
  if (el.mozRequestFullScreen) return el.mozRequestFullScreen();
  if (el.msRequestFullscreen) return el.msRequestFullscreen();
}}
function exitFullscreen(){{
  if (document.exitFullscreen) return document.exitFullscreen();
  if (document.webkitExitFullscreen) return document.webkitExitFullscreen();
  if (document.mozCancelFullScreen) return document.mozCancelFullScreen();
  if (document.msExitFullscreen) return document.msExitFullscreen();
}}
function isStandalone(){{
  try{{
    if (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches) return true;
    if (window.navigator && window.navigator.standalone) return true;
  }}catch(e){{}}
  return false;
}}
function isTouchDevice(){{
  return ('ontouchstart' in window) || (navigator.maxTouchPoints && navigator.maxTouchPoints > 0);
}}
function getKioskMode(){{
  var v = '';
  try{{ v = (qparam('kiosk') || '').toString().toLowerCase(); }}catch(e){{ v = ''; }}
  if (v === '1' || v === 'on' || v === 'true') return true;
  if (v === '0' || v === 'off' || v === 'false') return false;
  // Default heuristics: tablets => kiosk, phones => normal.
  if (!isTouchDevice()) return false;
  var w = 0, h = 0;
  try{{ w = parseInt(window.innerWidth || '0', 10) || 0; }}catch(e2){{ w = 0; }}
  try{{ h = parseInt(window.innerHeight || '0', 10) || 0; }}catch(e3){{ h = 0; }}
  var m = Math.min(w || 0, h || 0);
  return m >= 520;
}}
var kioskMode = false;
try{{
  kioskMode = getKioskMode();
  if (kioskMode) document.body.classList.add('kiosk');
}}catch(e){{}}
function getFsPref(){{
  var v = '';
  try{{ v = localStorage.getItem('desk_fs_pref') || ''; }}catch(e){{}}
  if (!v){{
    v = isTouchDevice() ? '1' : '0';
    try{{ localStorage.setItem('desk_fs_pref', v); }}catch(e){{}}
  }}
  return v === '1';
}}
function setOverlay(show){{
  var o = document.getElementById('fsOverlay');
  if (!o) return;
  o.style.display = show ? 'flex' : 'none';
}}
function updateFsUi(){{
  var fs = isFullscreen();
  if (fs) document.body.classList.add('fs');
  else document.body.classList.remove('fs');
  document.body.classList.remove('hidebar');
  var btn = document.getElementById('fsBtn');
  if (btn) btn.textContent = fs ? '全画面解除' : '全画面';
  if (!fs && getFsPref() && !isStandalone()) setOverlay(true);
  else setOverlay(false);
  kickBarTimer();
}}
function toggleFullscreen(){{
  if (isFullscreen()){{ exitFullscreen(); return; }}
  try{{
    var p = requestFullscreen();
    if (p && p.catch) p.catch(function(){{}});
  }}catch(e){{}}
}}
function kickBarTimer(){{
  if (!document.body.classList.contains('fs')) return;
  if (window._barTimer) clearTimeout(window._barTimer);
  window._barTimer = setTimeout(function(){{ document.body.classList.add('hidebar'); }}, 5000);
}}
function showBar(){{
  document.body.classList.remove('hidebar');
  kickBarTimer();
}}
function hideAddressBar(){{
  setTimeout(function(){{ try{{ window.scrollTo(0, 1); }}catch(e){{}} }}, 50);
}}
;['click','touchstart','mousemove','keydown'].forEach(function(ev){{ document.addEventListener(ev, showBar, false); }});
document.addEventListener('fullscreenchange', updateFsUi, false);
document.addEventListener('webkitfullscreenchange', updateFsUi, false);
document.addEventListener('mozfullscreenchange', updateFsUi, false);
document.addEventListener('MSFullscreenChange', updateFsUi, false);
window.addEventListener('orientationchange', hideAddressBar, false);
window.addEventListener('load', hideAddressBar, false);
setTimeout(hideAddressBar, 500);

function overlayTap(){{
  setOverlay(false);
  toggleFullscreen();
}}
updateFsUi();

// todo mode
var lastData = null;
var todoMode = 'google';
function getTodoMode(){{
  var v = '';
  try{{ v = localStorage.getItem('desk_todo_mode') || ''; }}catch(e){{}}
  if (v === 'local' || v === 'google') return v;
  return 'google';
}}
function setTodoMode(mode){{
  todoMode = (mode === 'local') ? 'local' : 'google';
  try{{ localStorage.setItem('desk_todo_mode', todoMode); }}catch(e){{}}
  updateTodoTabs();
  if (lastData) renderTodo(lastData);
}}
function updateTodoTabs(){{
  var g = document.getElementById('tabGoogle');
  var l = document.getElementById('tabLocal');
  if (g){{
    if (todoMode === 'google') g.classList.add('on');
    else g.classList.remove('on');
  }}
  if (l){{
    if (todoMode === 'local') l.classList.add('on');
    else l.classList.remove('on');
  }}
}}
todoMode = getTodoMode();
updateTodoTabs();

// calendar view (default: month)
var calView = 'month';
function getCalView(){{
  var v = '';
  try{{ v = localStorage.getItem('desk_cal_view') || ''; }}catch(e){{ v = ''; }}
  if (v === 'agenda' || v === 'month') return v;
  return 'month';
}}
function updateCalTabs(){{
  var m = document.getElementById('calTabMonth');
  var a = document.getElementById('calTabAgenda');
  if (m){{ if (calView === 'month') m.classList.add('on'); else m.classList.remove('on'); }}
  if (a){{ if (calView === 'agenda') a.classList.add('on'); else a.classList.remove('on'); }}
}}
function applyCalView(){{
  var mw = document.getElementById('calMonthWrap');
  var el = document.getElementById('events');
  if (mw) mw.style.display = (calView === 'month') ? 'flex' : 'none';
  if (el) el.style.display = (calView === 'agenda') ? 'block' : 'none';
  updateCalTabs();
}}
function setCalView(v){{
  calView = (v === 'agenda') ? 'agenda' : 'month';
  try{{ localStorage.setItem('desk_cal_view', calView); }}catch(e){{}}
  if (calView !== 'agenda') {{
    try{{ if (calPagerTimer) clearInterval(calPagerTimer); }}catch(e2){{}}
    calPagerTimer = 0;
  }}
  applyCalView();
  if (lastData) renderEvents(lastData);
}}
calView = getCalView();
if (typeof kioskMode !== 'undefined' && kioskMode) {{
  calView = 'month';
  try{{ localStorage.setItem('desk_cal_view', 'month'); }}catch(e){{}}
}}
applyCalView();

var calMonthOff = 0;
function getMonthOff(){{
  var v = '';
  try{{ v = localStorage.getItem('desk_cal_month_off') || ''; }}catch(e){{ v = ''; }}
  var n = parseInt(v || '0', 10) || 0;
  if (n < -24) n = -24;
  if (n > 24) n = 24;
  return n;
}}
function setMonthOff(n){{
  calMonthOff = parseInt(n || '0', 10) || 0;
  if (calMonthOff < -24) calMonthOff = -24;
  if (calMonthOff > 24) calMonthOff = 24;
  try{{ localStorage.setItem('desk_cal_month_off', String(calMonthOff)); }}catch(e){{}}
}}
function monthNav(delta){{
  if (delta === 0) setMonthOff(0);
  else setMonthOff(calMonthOff + (parseInt(delta, 10) || 0));
  if (calView !== 'month') setCalView('month');
  if (lastData) renderEvents(lastData);
}}
calMonthOff = getMonthOff();

function shortErr(s){{
  s = (s || '').toString();
  if (s.length > 90) return s.slice(0, 90) + '...';
  return s;
}}

function renderTodo(data){{
  var list = document.getElementById('todoList');
  var hint = document.getElementById('todoHint');
  if (!list) return;
  list.innerHTML = '';

  if (todoMode === 'google'){{
    var err = (data.gtasks_error || '').toString();
    var items = data.gtasks || [];
    var maxItems = (typeof kioskMode !== 'undefined' && kioskMode) ? 5 : 50;
    var shown = 0;
    if (hint){{
      if (err) hint.textContent = 'Google ToDo: ' + shortErr(err);
      else hint.textContent = 'Google ToDo: ' + (items.length || 0) + '件 / 自動更新：{REFRESH_SEC}秒';
    }}

	    (items || []).forEach(function(t){{
	      if (shown >= maxItems) return;
	      var it = document.createElement('div');
	      it.className = 'item';

      var chk = document.createElement('button');
      chk.className = 'chk';
      chk.textContent = '✓';
      chk.onclick = function(){{ completeGTask(t.id); }};
      it.appendChild(chk);

      var tx = document.createElement('div');
      tx.className = 'text';
	      var title = document.createElement('div');
	      title.className = 't';
	      title.textContent = t.title || '';
	      try{{ title.title = title.textContent || ''; }}catch(e1){{}}
	      tx.appendChild(title);

      var sub = '';
      if (t.due_date){{
        sub += '期限 ' + t.due_date + (t.due_time ? (' ' + t.due_time) : '');
      }}
      if (t.notes){{
        if (sub) sub += '\\n';
        sub += t.notes;
      }}
      if (sub){{
        var s = document.createElement('div');
        s.className = 's';
        s.textContent = sub;
        tx.appendChild(s);
      }}
      it.appendChild(tx);

      list.appendChild(it);
      shown++;
    }});

    if (!(typeof kioskMode !== 'undefined' && kioskMode) && items && items.length > maxItems){{
      var more = document.createElement('div');
      more.className = 'muted';
      more.textContent = '＋' + String(items.length - maxItems) + '件';
      list.appendChild(more);
    }}

    if (!items || items.length === 0){{
      var it = document.createElement('div');
      it.className = 'muted';
      it.textContent = err ? 'Google ToDoが読めません（設定/権限を確認）' : 'ToDoなし';
      list.appendChild(it);
    }}
    return;
  }}

  // local
  var items2 = data.todo || [];
  var maxItems2 = (typeof kioskMode !== 'undefined' && kioskMode) ? 5 : 50;
  if (hint) hint.textContent = 'Local: ' + (items2.length || 0) + '件 / 自動更新：{REFRESH_SEC}秒';
	  (items2 || []).forEach(function(t, i){{
	    if (i >= maxItems2) return;
	    var it = document.createElement('div');
	    it.className = 'item';

    var chk = document.createElement('button');
    chk.className = 'chk';
    chk.textContent = '✓';
    chk.onclick = function(){{ completeLocal(i); }};
    it.appendChild(chk);

    var tx = document.createElement('div');
    tx.className = 'text';
	    var title = document.createElement('div');
	    title.className = 't';
	    title.textContent = t;
	    try{{ title.title = title.textContent || ''; }}catch(e2){{}}
	    tx.appendChild(title);
    it.appendChild(tx);

    list.appendChild(it);
  }});
  if (!(typeof kioskMode !== 'undefined' && kioskMode) && items2 && items2.length > maxItems2){{
    var more2 = document.createElement('div');
    more2.className = 'muted';
    more2.textContent = '＋' + String(items2.length - maxItems2) + '件';
    list.appendChild(more2);
  }}
  if (!items2 || items2.length === 0){{
    var it = document.createElement('div');
    it.className = 'muted';
    it.textContent = 'todo.txt にタスクを書いてください';
    list.appendChild(it);
  }}
}}

// calendar pager (kiosk-friendly)
var calRows = [];
var calPage = 0;
var calPageSize = 12;
var calPageCount = 1;
var calPagerTimer = 0;
var calHintBase = '';

var _todayStartMs = 0;
function getTodayStartMs(){{
  if (_todayStartMs) return _todayStartMs;
  var d = new Date();
  _todayStartMs = (new Date(d.getFullYear(), d.getMonth(), d.getDate())).getTime();
  return _todayStartMs;
}}
function isTodayEpoch(epoch){{
  if (!epoch) return false;
  try{{
    var diff = Math.round(((epoch * 1000) - getTodayStartMs()) / 86400000);
    return diff === 0;
  }}catch(e){{}}
  return false;
}}
function dayLabel(epoch, fallback){{
  var fb = (fallback || '').toString();
  if (!epoch) return fb;
  try{{
    var d = new Date(epoch * 1000);
    var base = (d.getMonth()+1) + '/' + d.getDate() + ' (' + '日月火水木金土'[d.getDay()] + ')';
    var diff = Math.round(((epoch * 1000) - getTodayStartMs()) / 86400000);
    var tag = '';
    if (diff === 0) tag = '今日';
    else if (diff === 1) tag = '明日';
    else if (diff === 2) tag = '明後日';
    else if (diff === -1) tag = '昨日';
    return tag ? (tag + '  ' + base) : base;
  }}catch(e){{}}
  return fb;
}}

// month calendar helpers
var calByDay = {{}};
var jpHolidayCache = {{}};
function ymdKey(y, m, d){{ return String(y) + '-' + pad(m) + '-' + pad(d); }}
function nthWeekdayDay(y, m, weekday, nth){{
  // weekday: 0=Sun..6=Sat, m: 1..12
  var first = new Date(y, (m || 1) - 1, 1);
  var offset = (weekday - first.getDay() + 7) % 7;
  return 1 + offset + ((nth || 1) - 1) * 7;
}}
function vernalEquinoxDay(y){{
  // Approximation valid for 1900-2150 (JP public holiday calc)
  if (y <= 1979) return Math.floor(20.8357 + 0.242194 * (y - 1980) - Math.floor((y - 1983) / 4));
  if (y <= 2099) return Math.floor(20.8431 + 0.242194 * (y - 1980) - Math.floor((y - 1980) / 4));
  return Math.floor(21.8510 + 0.242194 * (y - 1980) - Math.floor((y - 1980) / 4));
}}
function autumnEquinoxDay(y){{
  if (y <= 1979) return Math.floor(23.2588 + 0.242194 * (y - 1980) - Math.floor((y - 1983) / 4));
  if (y <= 2099) return Math.floor(23.2488 + 0.242194 * (y - 1980) - Math.floor((y - 1980) / 4));
  return Math.floor(24.2488 + 0.242194 * (y - 1980) - Math.floor((y - 1980) / 4));
}}
function computeJpHolidays(year){{
  year = parseInt(year || '0', 10) || 0;
  if (!year) return {{}};
  if (jpHolidayCache[year]) return jpHolidayCache[year];
  var map = {{}};
  function add(m, d, name){{
    d = parseInt(d || '0', 10) || 0;
    if (!d) return;
    map[ymdKey(year, m, d)] = name || '祝日';
  }}

  // Fixed / moveable holidays (modern rules + key exceptions)
  add(1, 1, '元日');
  add(1, (year >= 2000) ? nthWeekdayDay(year, 1, 1, 2) : 15, '成人の日');
  add(2, 11, '建国記念の日');
  if (year >= 2020) add(2, 23, '天皇誕生日');
  else if (year >= 1989 && year <= 2018) add(12, 23, '天皇誕生日');
  add(3, vernalEquinoxDay(year), '春分の日');
  add(4, 29, (year >= 2007) ? '昭和の日' : (year >= 1989 ? 'みどりの日' : '天皇誕生日'));
  add(5, 3, '憲法記念日');
  add(5, 4, (year >= 2007) ? 'みどりの日' : '国民の休日');
  add(5, 5, 'こどもの日');

  if (year === 2020) add(7, 23, '海の日');
  else if (year === 2021) add(7, 22, '海の日');
  else if (year >= 2003) add(7, nthWeekdayDay(year, 7, 1, 3), '海の日');
  else if (year >= 1996) add(7, 20, '海の日');

  if (year === 2020) add(8, 10, '山の日');
  else if (year === 2021) add(8, 8, '山の日');
  else if (year >= 2016) add(8, 11, '山の日');

  add(9, (year >= 2003) ? nthWeekdayDay(year, 9, 1, 3) : 15, '敬老の日');
  add(9, autumnEquinoxDay(year), '秋分の日');

  if (year === 2020) add(7, 24, 'スポーツの日');
  else if (year === 2021) add(7, 23, 'スポーツの日');
  else add(10, (year >= 2000) ? nthWeekdayDay(year, 10, 1, 2) : 10, (year >= 2020) ? 'スポーツの日' : '体育の日');

  add(11, 3, '文化の日');
  add(11, 23, '勤労感謝の日');

  // One-off holidays (recent)
  if (year === 2019){{
    add(4, 30, '国民の休日');
    add(5, 1, '天皇の即位の日');
    add(5, 2, '国民の休日');
    add(10, 22, '即位礼正殿の儀');
  }}

  function hasKey(k){{ return !!map[k]; }}
  function keyOfDate(dt){{ return ymdKey(year, dt.getMonth() + 1, dt.getDate()); }}

  // National holiday: weekday between two holidays becomes a holiday.
  var dt0 = new Date(year, 0, 1);
  for (; dt0.getFullYear() === year; dt0.setDate(dt0.getDate() + 1)) {{
    var k0 = keyOfDate(dt0);
    if (hasKey(k0)) continue;
    var prev = new Date(dt0.getTime()); prev.setDate(prev.getDate() - 1);
    var next = new Date(dt0.getTime()); next.setDate(next.getDate() + 1);
    if (prev.getFullYear() !== year || next.getFullYear() !== year) continue;
    if (hasKey(keyOfDate(prev)) && hasKey(keyOfDate(next))) {{
      map[k0] = '国民の休日';
    }}
  }}

  // Substitute holiday: if a holiday falls on Sunday, next weekday becomes holiday.
  Object.keys(map).sort().forEach(function(k){{
    var parts = (k || '').split('-');
    if (parts.length !== 3) return;
    var y = parseInt(parts[0] || '0', 10) || 0;
    var m = parseInt(parts[1] || '0', 10) || 0;
    var d = parseInt(parts[2] || '0', 10) || 0;
    if (y !== year || !m || !d) return;
    var dt = new Date(y, m - 1, d);
    if (dt.getDay() !== 0) return;
    var sub = new Date(dt.getTime());
    for (var i = 0; i < 14; i++) {{
      sub.setDate(sub.getDate() + 1);
      if (sub.getFullYear() !== year) break;
      var kk = keyOfDate(sub);
      if (!hasKey(kk)) {{
        map[kk] = '振替休日';
        break;
      }}
    }}
  }});

  // Re-run national-holiday rule once (after substitutes)
  var dt1 = new Date(year, 0, 1);
  for (; dt1.getFullYear() === year; dt1.setDate(dt1.getDate() + 1)) {{
    var k1 = keyOfDate(dt1);
    if (hasKey(k1)) continue;
    var prev1 = new Date(dt1.getTime()); prev1.setDate(prev1.getDate() - 1);
    var next1 = new Date(dt1.getTime()); next1.setDate(next1.getDate() + 1);
    if (prev1.getFullYear() !== year || next1.getFullYear() !== year) continue;
    if (hasKey(keyOfDate(prev1)) && hasKey(keyOfDate(next1))) {{
      map[k1] = '国民の休日';
    }}
  }}

  jpHolidayCache[year] = map;
  return map;
}}
function jpHolidayNameForDate(dt){{
  if (!dt) return '';
  var y = 0;
  try{{ y = dt.getFullYear(); }}catch(e){{ y = 0; }}
  if (!y) return '';
  var map = computeJpHolidays(y);
  return map[ymdKey(y, dt.getMonth() + 1, dt.getDate())] || '';
}}
function jpHolidayNameForEpoch(dayStart){{
  try{{
    var d = new Date((dayStart || 0) * 1000);
    return jpHolidayNameForDate(d) || '';
  }}catch(e){{}}
  return '';
}}
function dayStartSec(epochSec){{
  try{{
    var d = new Date((epochSec || 0) * 1000);
    return Math.floor((new Date(d.getFullYear(), d.getMonth(), d.getDate())).getTime() / 1000);
  }}catch(e){{}}
  return 0;
}}
function buildCalByDay(events){{
  var map = {{}};
  (events || []).forEach(function(ev){{
    if (!ev) return;
    var s = 0, e = 0;
    try{{ s = parseInt(ev.start_epoch || ev.date_epoch || '0', 10) || 0; }}catch(_e){{ s = 0; }}
    try{{ e = parseInt(ev.end_epoch || '0', 10) || 0; }}catch(_e2){{ e = 0; }}
    if (!s) return;
    if (!e) e = s + 1;
    var from = dayStartSec(s);
    var to = dayStartSec(Math.max(s, e - 1));
    var guard = 0;
    for (var t = from; t <= to; t += 86400){{
      var k = String(t);
      if (!map[k]) map[k] = [];
      map[k].push(ev);
      guard++;
      if (guard > 40) break;
    }}
  }});
  for (var k in map){{
    if (!Object.prototype.hasOwnProperty.call(map, k)) continue;
    map[k].sort(function(a, b){{
      var aa = a && a.all_day ? 0 : 1;
      var bb = b && b.all_day ? 0 : 1;
      if (aa !== bb) return aa - bb;
      var sa = 0, sb = 0;
      try{{ sa = parseInt(a.start_epoch || '0', 10) || 0; }}catch(e1){{ sa = 0; }}
      try{{ sb = parseInt(b.start_epoch || '0', 10) || 0; }}catch(e2){{ sb = 0; }}
      return sa - sb;
    }});
  }}
  return map;
}}

function getNowDate(data){{
  try{{
    var n = parseInt((data && data.now) ? data.now : '0', 10) || 0;
    if (n) return new Date(n * 1000);
  }}catch(e){{}}
  return new Date();
}}

function renderMonth(data){{
  var wrap = document.getElementById('calMonthWrap');
  var grid = document.getElementById('monthGrid');
  var titleEl = document.getElementById('monthTitle');
  if (!wrap || !grid || !titleEl) return;

  var now = getNowDate(data);
  var base = new Date(now.getFullYear(), now.getMonth() + (calMonthOff || 0), 1);
  titleEl.textContent = base.getFullYear() + '年 ' + (base.getMonth() + 1) + '月';

  var start = new Date(base.getFullYear(), base.getMonth(), 1 - base.getDay());
  var nowY = now.getFullYear();
  var nowM = now.getMonth();
  var nowD = now.getDate();

  grid.innerHTML = '';
  for (var i = 0; i < 42; i++) {{
    var d = new Date(start.getFullYear(), start.getMonth(), start.getDate() + i);
    var inMonth = (d.getMonth() === base.getMonth());
    var dow = d.getDay();

	    var cell = document.createElement('button');
	    cell.type = 'button';
	    cell.className = 'day' + (inMonth ? '' : ' out') + (dow === 0 ? ' sun' : (dow === 6 ? ' sat' : ''));
	    if (d.getFullYear() === nowY && d.getMonth() === nowM && d.getDate() === nowD) cell.className += ' today';
	    var hol = '';
	    try{{ hol = jpHolidayNameForDate(d) || ''; }}catch(eh){{ hol = ''; }}
	    if (hol) {{
	      cell.className += ' holiday';
	      try{{ cell.dataset.holiday = hol; }}catch(eh2){{}}
	      try{{ cell.title = hol; }}catch(eh3){{}}
	    }}

    var dayStart = Math.floor((new Date(d.getFullYear(), d.getMonth(), d.getDate())).getTime() / 1000);
    cell.dataset.day = String(dayStart);

    var num = document.createElement('div');
    num.className = 'num';
	    num.textContent = String(d.getDate());
	    cell.appendChild(num);

	    var evs = calByDay[String(dayStart)] || [];
	    if (evs && evs.length) {{
	      cell.className += ' busy';
	      var badges = document.createElement('div');
	      badges.className = 'badges';
	      var cnt = document.createElement('span');
	      cnt.className = 'cnt';
	      cnt.textContent = String(evs.length);
	      badges.appendChild(cnt);
	      cell.appendChild(badges);

	      var lines = document.createElement('div');
	      lines.className = 'lines';
	      var vw = 0, vh = 0;
	      try{{ vw = parseInt(window.innerWidth || '0', 10) || 0; }}catch(_e0){{ vw = 0; }}
	      try{{ vh = parseInt(window.innerHeight || '0', 10) || 0; }}catch(_e1){{ vh = 0; }}
	      var minSide = Math.min(vw || 0, vh || 0);
	      var maxLines = (typeof kioskMode !== 'undefined' && kioskMode) ? 2 : 3;
	      if (minSide && minSide < 520) maxLines = 2;
	      if (minSide && minSide < 420) maxLines = 1;
	      for (var j2 = 0; j2 < evs.length && j2 < maxLines; j2++) {{
	        var ev2 = evs[j2] || {{}};
	        var line = document.createElement('div');
	        line.className = 'line' + ((ev2 && ev2.all_day) ? ' allday' : '');

	        var label = '';
	        if (ev2 && ev2.all_day) {{
	          label = '終日';
	        }} else {{
	          label = (ev2 && ev2.start) ? ('' + ev2.start) : '';
	          if (!label) {{
	            var se = 0;
	            try{{ se = parseInt(ev2.start_epoch || '0', 10) || 0; }}catch(_e3){{ se = 0; }}
	            if (se) {{
	              try{{
	                var dd = new Date(se * 1000);
	                label = pad(dd.getHours()) + ':' + pad(dd.getMinutes());
	              }}catch(_e4){{}}
	            }}
	          }}
	        }}
	        if (!label) label = '—';

	        var lt = document.createElement('span');
	        lt.className = 'lt';
	        lt.textContent = label;
	        line.appendChild(lt);

	        var ln = document.createElement('span');
	        ln.className = 'ln';
	        ln.textContent = (ev2 && ev2.title) ? ('' + ev2.title) : '';
	        line.appendChild(ln);

	        lines.appendChild(line);
	      }}
	      cell.appendChild(lines);

	      var dots = document.createElement('div');
	      dots.className = 'dots';
	      var maxDots = 4;
	      for (var j = 0; j < evs.length && j < maxDots; j++) {{
	        var dot = document.createElement('span');
        dot.className = 'dot' + ((evs[j] && evs[j].all_day) ? ' allday' : '');
        dots.appendChild(dot);
      }}
      if (evs.length > maxDots) {{
        var more = document.createElement('span');
        more.className = 'dot more';
        more.textContent = '+' + String(evs.length - maxDots);
        dots.appendChild(more);
      }}
      cell.appendChild(dots);
    }}

    cell.onclick = function() {{
      var ep = parseInt(this.dataset.day || '0', 10) || 0;
      openDayOverlay(ep);
    }};
    grid.appendChild(cell);
  }}
}}

function renderNextEvents(data){{
  var box = document.getElementById('nextEvents');
  if (!box) return;
  box.innerHTML = '';

  var title = document.createElement('div');
  title.className = 'miniTitle';
  title.textContent = '次の予定';
  box.appendChild(title);

  var list = document.createElement('div');
  list.className = 'miniList';
  box.appendChild(list);

  var now = 0;
  try{{ now = parseInt((data && data.now) ? data.now : '0', 10) || 0; }}catch(e){{ now = 0; }}
  if (!now) now = Math.floor(Date.now() / 1000);
  var todayStart = dayStartSec(now);

  var allday = [];
  var timed = [];
  (data.events || []).forEach(function(ev){{
    if (!ev) return;
    if (ev.all_day) {{
      var de = 0;
      try{{ de = parseInt(ev.date_epoch || '0', 10) || 0; }}catch(e1){{ de = 0; }}
      if (de === todayStart) allday.push(ev);
      return;
    }}
    var s = 0, e = 0;
    try{{ s = parseInt(ev.start_epoch || '0', 10) || 0; }}catch(e2){{ s = 0; }}
    try{{ e = parseInt(ev.end_epoch || '0', 10) || 0; }}catch(e3){{ e = 0; }}
    if (e && now > e) return;
    if (s && s >= (now - 600)) timed.push(ev);
  }});

  allday.sort(function(a, b){{ return String(a.title || '').localeCompare(String(b.title || '')); }});
  timed.sort(function(a, b){{
    var sa = parseInt(a.start_epoch || '0', 10) || 0;
    var sb = parseInt(b.start_epoch || '0', 10) || 0;
    return sa - sb;
  }});

  var merged = [];
  (allday || []).forEach(function(x){{ merged.push(x); }});
  (timed || []).forEach(function(x){{ merged.push(x); }});

  var max = kioskMode ? 3 : 4;
  if (!merged.length) {{
    var it0 = document.createElement('div');
    it0.className = 'muted';
    it0.textContent = '近日の予定なし';
    list.appendChild(it0);
    return;
  }}

  for (var i = 0; i < merged.length && i < max; i++) {{
    var ev = merged[i] || {{}};
    var row = document.createElement('div');
    row.className = 'miniEv';

    var tp = document.createElement('span');
    tp.className = 'timepill' + (ev.all_day ? ' allday' : '');
    tp.textContent = ev.all_day ? '終日' : ((ev.end ? (ev.start + '–' + ev.end) : ev.start) || '');
    row.appendChild(tp);

    var body = document.createElement('div');
    body.className = 'body';
    var t = document.createElement('div');
    t.className = 't';
    t.textContent = ev.title || '';
    body.appendChild(t);
    row.appendChild(body);

    list.appendChild(row);
  }}
}}

function closeDayOverlay(){{
  var o = document.getElementById('dayOverlay');
  if (o) o.style.display = 'none';
}}
function openDayOverlay(dayStart){{
  var o = document.getElementById('dayOverlay');
  var t = document.getElementById('dayTitle');
  var s = document.getElementById('daySub');
  var list = document.getElementById('dayEvents');
  if (!o || !t || !s || !list) return;

  var fb = '';
  try{{
    var d = new Date((dayStart || 0) * 1000);
    fb = pad(d.getMonth() + 1) + '/' + pad(d.getDate());
  }}catch(e){{ fb = ''; }}
  t.textContent = dayLabel(dayStart, fb);

  var evs = calByDay[String(dayStart)] || [];
  var hol = '';
  try{{ hol = jpHolidayNameForEpoch(dayStart) || ''; }}catch(eh){{ hol = ''; }}
  var base = evs.length ? (String(evs.length) + '件') : '予定なし';
  s.textContent = hol ? (hol + ' ・ ' + base) : base;
  list.innerHTML = '';

  if (!evs.length) {{
    var it0 = document.createElement('div');
    it0.className = 'muted';
    it0.textContent = '予定なし';
    list.appendChild(it0);
    o.style.display = 'flex';
    return;
  }}

  (evs || []).forEach(function(ev){{
    var row = document.createElement('div');
    row.className = 'dayItem';

    var tp = document.createElement('span');
    tp.className = 'timepill' + (ev && ev.all_day ? ' allday' : '');
    tp.textContent = ev && ev.all_day ? '終日' : ((ev && ev.end ? (ev.start + '–' + ev.end) : (ev.start || '')) || '');
    row.appendChild(tp);

    var body = document.createElement('div');
    body.className = 'body';
    var tt = document.createElement('div');
    tt.className = 't';
    tt.textContent = (ev && ev.title) ? ev.title : '';
    body.appendChild(tt);
    if (ev && ev.location) {{
      var loc = document.createElement('div');
      loc.className = 's';
      loc.textContent = ev.location;
      body.appendChild(loc);
    }}
    row.appendChild(body);
    list.appendChild(row);
  }});

  o.style.display = 'flex';
}}

function buildCalRows(events){{
  var rows = [];
  var lastDate = '';
  (events || []).forEach(function(ev){{
    if (ev && ev.date && ev.date !== lastDate){{
      var ep = 0;
      try{{ ep = parseInt(ev.date_epoch || '0', 10) || 0; }}catch(e){{ ep = 0; }}
      rows.push({{ t:'day', date: ev.date, epoch: ep }});
      lastDate = ev.date;
    }}
    rows.push({{ t:'ev', ev: ev || {{}} }});
  }});
  return rows;
}}

function calcCalPageSize(list){{
  var h = 0;
  try{{ h = list ? (list.clientHeight || 0) : 0; }}catch(e){{ h = 0; }}
  var n = h ? Math.floor(h / 64) : 10;
  if (n < 6) n = 6;
  if (n > 18) n = 18;
  return n;
}}

function updateCalHint(){{
  var hint = document.getElementById('calHint');
  if (!hint) return;
  var s = calHintBase || '';
  if (calView === 'agenda' && kioskMode && calPageCount > 1) s = (s ? (s + ' / ') : '') + ('p ' + (calPage + 1) + '/' + calPageCount);
  hint.textContent = s;
}}

function renderCalPage(){{
  var list = document.getElementById('events');
  if (!list) return;
  list.innerHTML = '';

  if (!calRows || calRows.length === 0){{
    var it = document.createElement('div');
    it.className = 'muted';
    it.textContent = '予定なし';
    list.appendChild(it);
    calPageCount = 1;
    calPage = 0;
    updateCalHint();
    return;
  }}

  calPageSize = calcCalPageSize(list);
  calPageCount = Math.max(1, Math.ceil(calRows.length / calPageSize));
  if (calPage >= calPageCount) calPage = 0;

  var start = calPage * calPageSize;
  if (start >= calRows.length){{ calPage = 0; start = 0; }}

  var budget = calPageSize;
  var prependDay = null;
  try{{
    if (start > 0 && calRows[start] && calRows[start].t !== 'day') {{
      for (var j = start; j >= 0; j--) {{
        if (calRows[j] && calRows[j].t === 'day') {{ prependDay = calRows[j]; break; }}
      }}
    }}
  }}catch(e){{ prependDay = null; }}
  if (prependDay) {{
    var hd0 = document.createElement('div');
    hd0.className = 'ev-day';
    if (isTodayEpoch(prependDay.epoch)) hd0.className += ' today';
    hd0.textContent = dayLabel(prependDay.epoch || 0, prependDay.date || '');
    list.appendChild(hd0);
    budget = Math.max(1, budget - 1);
  }}

  var end = Math.min(calRows.length, start + budget);

  for (var i = start; i < end; i++) {{
    var r = calRows[i] || {{}};
    if (r.t === 'day') {{
      var hd = document.createElement('div');
      hd.className = 'ev-day';
      if (isTodayEpoch(r.epoch)) hd.className += ' today';
      hd.textContent = dayLabel(r.epoch || 0, r.date || '');
      list.appendChild(hd);
      continue;
    }}
    var ev = r.ev || {{}};

    var row = document.createElement('div');
    row.className = 'ev' + (ev.all_day ? ' allDay' : '');

    var meta = document.createElement('div');
    meta.className = 'meta';
    var tp = document.createElement('span');
    tp.className = 'timepill';
    tp.textContent = ((ev.end ? (ev.start + '–' + ev.end) : ev.start) || '');
    meta.appendChild(tp);
    row.appendChild(meta);

    var body = document.createElement('div');
    var title = document.createElement('div');
    title.className = 'title';
    title.textContent = ev.title || '';
    body.appendChild(title);
    if (ev.location) {{
      var loc = document.createElement('div');
      loc.className = 'loc';
      loc.textContent = ev.location;
      body.appendChild(loc);
    }}
    row.appendChild(body);
    list.appendChild(row);
  }}
  updateCalHint();
}}

function startCalPager(){{
  if (!kioskMode) return;
  if (calPagerTimer) return;
  calPagerTimer = setInterval(function(){{
    if (!kioskMode) return;
    if (!calRows || calRows.length === 0) return;
    if (calPageCount <= 1) return;
    calPage = (calPage + 1) % calPageCount;
    renderCalPage();
  }}, 18 * 1000);
  try{{
    var list = document.getElementById('events');
    if (list && !list._tapBound) {{
      list._tapBound = true;
      list.addEventListener('click', function(){{
        if (!kioskMode) return;
        if (calPageCount <= 1) return;
        calPage = (calPage + 1) % calPageCount;
        renderCalPage();
      }}, false);
    }}
  }}catch(e){{}}
}}

function renderEvents(data){{
  var list = document.getElementById('events');
  if (!list) return;

  var src = (data.calendar_source || '').toString();
  var err = (data.calendar_error || '').toString();
  var mode = (data.calendar_mode || '').toString();
  var n = parseInt(data.calendar_ids_count || '0', 10) || 0;
  var days = parseInt(data.calendar_days || '0', 10) || 0;

  var s = days ? ('range: ' + days + 'd') : '';
  if (src) s = (s ? (s + ' / ') : '') + ('source: ' + src);
  if (n) s = (s ? (s + ' / ') : '') + ('cals: ' + n + (mode ? (' ' + mode) : ''));
  if (err) s = (s ? (s + ' / ') : '') + shortErr(err);
  calHintBase = s;

  try{{ calByDay = buildCalByDay(data.events || []); }}catch(e){{ calByDay = {{}}; }}
  renderMonth(data);
  renderNextEvents(data);
  applyCalView();
  updateCalHint();
  if (calView === 'month') return;

  if (!kioskMode) {{
    list.innerHTML = '';
    updateCalHint();

    var lastDate = '';
    (data.events || []).forEach(function(ev){{
      if (ev.date && ev.date !== lastDate){{
        var ep = 0;
        try{{ ep = parseInt(ev.date_epoch || '0', 10) || 0; }}catch(e){{ ep = 0; }}
        var hd = document.createElement('div');
        hd.className = 'ev-day';
        if (isTodayEpoch(ep)) hd.className += ' today';
        hd.textContent = dayLabel(ep, ev.date);
        list.appendChild(hd);
        lastDate = ev.date;
      }}

      var row = document.createElement('div');
      row.className = 'ev' + (ev.all_day ? ' allDay' : '');

      var meta = document.createElement('div');
      meta.className = 'meta';
      var tp = document.createElement('span');
      tp.className = 'timepill';
      tp.textContent = ((ev.end ? (ev.start + '–' + ev.end) : ev.start) || '');
      meta.appendChild(tp);
      row.appendChild(meta);

      var body = document.createElement('div');
      var title = document.createElement('div');
      title.className = 'title';
      title.textContent = ev.title || '';
      body.appendChild(title);
      if (ev.location){{
        var loc = document.createElement('div');
        loc.className = 'loc';
        loc.textContent = ev.location;
        body.appendChild(loc);
      }}
      row.appendChild(body);
      list.appendChild(row);
    }});

    if (!data.events || data.events.length === 0){{
      var it = document.createElement('div');
      it.className = 'muted';
      it.textContent = '予定なし';
      list.appendChild(it);
    }}
    return;
  }}

  calRows = buildCalRows(data.events || []);
  renderCalPage();
  startCalPager();
}}

	function renderWeather(data){{
	  var el = document.getElementById('wxText');
	  if (!el) return;
	  var w = (data && data.weather) ? data.weather : {{}};
  var err = (data && data.weather_error) ? ('' + data.weather_error) : '';
  if (w && w.enabled === false){{
    el.textContent = '天気 OFF';
    el.style.opacity = '0.6';
    return;
  }}
  el.style.opacity = '1';
  var name = (w && w.name) ? ('' + w.name) : '';
  var text = (w && w.text) ? ('' + w.text) : '';
  var temp = '';
  if (w && typeof w.temp_c_int !== 'undefined' && w.temp_c_int !== null){{
    temp = '' + w.temp_c_int + '℃';
  }} else if (w && typeof w.temp_c !== 'undefined' && w.temp_c !== null){{
    temp = '' + w.temp_c + '℃';
  }}
  var line = '';
  if (text) line = text;
  if (temp) line = line ? (line + ' ' + temp) : temp;
  if (name && name !== '天気') line = line ? (name + ' ' + line) : name;
  if (!line) line = err ? ('天気: ' + shortErr(err)) : '天気 --';
  el.textContent = line;
}}

function renderNextText(data){{
  var el = document.getElementById('nextText');
  if (!el) return;

  try{{
    el.classList.remove('nowev');
    el.classList.remove('allday');
    el.classList.remove('nonev');
  }}catch(e0){{}}

  var now = Math.floor(Date.now() / 1000);
  try{{
    var serverNow = parseInt((data && data.now) ? data.now : '0', 10) || 0;
    if (serverNow && Math.abs(serverNow - now) > 600) now = serverNow;
  }}catch(e1){{}}

  var todayStart = dayStartSec(now);
  var timedNow = null;
  var timedNowEnd = 0;
  var timedNextToday = null;
  var timedNextTodayStart = 0;
  var timedNext = null;
  var timedNextStart = 0;
  var allToday = null;
  var allNext = null;
  var allNextDay = 0;

  (data && data.events ? data.events : []).forEach(function(ev){{
    if (!ev) return;
    if (ev.all_day) {{
      var de = 0;
      try{{ de = parseInt(ev.date_epoch || '0', 10) || 0; }}catch(_e2){{ de = 0; }}
      if (!de) return;
      if (de === todayStart) {{
        if (!allToday) allToday = ev;
      }} else if (de > todayStart) {{
        if (!allNext || (allNextDay && de < allNextDay) || !allNextDay) {{
          allNext = ev;
          allNextDay = de;
        }}
      }}
      return;
    }}

    var s = 0, e = 0;
    try{{ s = parseInt(ev.start_epoch || '0', 10) || 0; }}catch(_e3){{ s = 0; }}
    try{{ e = parseInt(ev.end_epoch || '0', 10) || 0; }}catch(_e4){{ e = 0; }}
    if (!s) return;
    if (!e) e = s + 1;

    if (now >= s && now < e) {{
      if (!timedNow || (e && timedNowEnd && e < timedNowEnd) || !timedNowEnd) {{
        timedNow = ev;
        timedNowEnd = e;
      }}
      return;
    }}

    if (s >= now) {{
      var ds = dayStartSec(s);
      if (ds === todayStart) {{
        if (!timedNextToday || (s && timedNextTodayStart && s < timedNextTodayStart) || !timedNextTodayStart) {{
          timedNextToday = ev;
          timedNextTodayStart = s;
        }}
      }}
      if (!timedNext || (s && timedNextStart && s < timedNextStart) || !timedNextStart) {{
        timedNext = ev;
        timedNextStart = s;
      }}
    }}
  }});

  function dayTag(dayStart){{
    if (!dayStart || !todayStart) return '';
    var diff = Math.round((dayStart - todayStart) / 86400);
    if (diff === 0) return '';
    if (diff === 1) return '明日 ';
    if (diff === 2) return '明後日 ';
    if (diff === -1) return '昨日 ';
    return '';
  }}

  function timeRange(ev){{
    if (!ev) return '';
    if (ev.all_day) return '終日';
    var s = (ev.start || '').toString();
    var e = (ev.end || '').toString();
    if (s && e) return s + '–' + e;
    return s || e || '';
  }}

  function setText(kind, txt, full){{
    el.textContent = txt || '次 --';
    try{{ el.title = full || ''; }}catch(_e5){{}}
    try{{ if (kind) el.classList.add(kind); }}catch(_e6){{}}
  }}

  if (timedNow) {{
    var tr0 = timeRange(timedNow);
    var base0 = 'いま ' + (tr0 ? (tr0 + ' ') : '') + ((timedNow.title || '').toString());
    setText('nowev', base0, base0 + ((timedNow.location ? (' / ' + timedNow.location) : '')));
    return;
  }}

  if (timedNextToday) {{
    var tr1 = (timedNextToday.start || '').toString();
    if (!tr1) tr1 = timeRange(timedNextToday);
    var base1 = '次 ' + (tr1 ? (tr1 + ' ') : '') + ((timedNextToday.title || '').toString());
    setText('', base1, base1 + ((timedNextToday.location ? (' / ' + timedNextToday.location) : '')));
    return;
  }}

  if (timedNext) {{
    var se = 0;
    try{{ se = parseInt(timedNext.start_epoch || '0', 10) || 0; }}catch(_e7){{ se = 0; }}
    var tag = dayTag(dayStartSec(se));
    var tr3 = (timedNext.start || '').toString();
    if (!tr3) tr3 = timeRange(timedNext);
    var base3 = (tag ? tag : '') + '次 ' + (tr3 ? (tr3 + ' ') : '') + ((timedNext.title || '').toString());
    setText('', base3, base3 + ((timedNext.location ? (' / ' + timedNext.location) : '')));
    return;
  }}

  if (allToday) {{
    var base2 = '今日 終日 ' + ((allToday.title || '').toString());
    setText('allday', base2, base2);
    return;
  }}

  if (allNext) {{
    var tag2 = dayTag(allNextDay);
    var base4 = (tag2 ? tag2 : '') + '終日 ' + ((allNext.title || '').toString());
    setText('allday', base4, base4);
    return;
  }}

  setText('nonev', '予定なし', '予定なし');
}}

	function render(data){{
	  lastData = data || {{}};
	  renderTodo(lastData);
	  renderEvents(lastData);
	  updateTodoTabs();
	  renderWeather(lastData);
	  renderNextText(lastData);
  try{{
    var rev = parseInt(lastData.ui_rev || '0', 10) || 0;
    if (!window._uiRev) window._uiRev = rev;
    else if (rev && window._uiRev && rev !== window._uiRev) location.reload();
  }}catch(e){{}}

  var st = document.getElementById('status');
  if (st){{
    var msg = '更新: ' + new Date().toLocaleTimeString() + ' / ' + location.host;
	    try{{ if (lastData && lastData.ui_rev) msg += ' / rev:' + String(lastData.ui_rev).slice(-4); }}catch(e){{}}
	    try{{ if (demoMode) msg += ' / DEMO'; }}catch(e0){{}}
	    if (lastData.calendar_error) msg += ' / CAL: ' + shortErr(lastData.calendar_error);
	    if (lastData.gtasks_error) msg += ' / TODO: ' + shortErr(lastData.gtasks_error);
	    st.textContent = msg;
	  }}
	}}

function nowMs(){{ return (new Date()).getTime(); }}
function _trim(s){{ return (s || '').replace(/^\\s+|\\s+$/g, ''); }}

function _xhrJson(method, url, obj, cb){{
  var xhr = null;
  try{{ xhr = new XMLHttpRequest(); }}catch(e){{ cb(0, {{}}); return; }}
  try{{ xhr.open(method, url, true); }}catch(e){{ cb(0, {{}}); return; }}
  try{{ xhr.timeout = 8000; }}catch(e){{}}
  xhr.onreadystatechange = function(){{
    if (xhr.readyState !== 4) return;
    var out = {{}};
    try{{ out = JSON.parse(xhr.responseText || '{{}}'); }}catch(e){{ out = {{}}; }}
    cb(xhr.status || 0, out);
  }};
  xhr.onerror = function(){{ cb(0, {{}}); }};
  xhr.ontimeout = function(){{ cb(0, {{}}); }};
  try{{ xhr.setRequestHeader('Accept', 'application/json'); }}catch(e){{}}
  if (method !== 'GET') {{
    try{{
      xhr.setRequestHeader('Content-Type', 'application/json; charset=utf-8');
      xhr.setRequestHeader('X-Desk-Token', getToken());
    }}catch(e){{}}
  }}
  var body = null;
  if (method !== 'GET') {{
    try{{ body = JSON.stringify(obj || {{}}); }}catch(e){{ body = '{{}}'; }}
  }}
  try{{ xhr.send(body); }}catch(e){{ cb(0, {{}}); }}
}}

function _okStatus(status){{ return status >= 200 && status < 300; }}
function getJson(url, cb){{ _xhrJson('GET', withDemoUrl(url), null, cb); }}
function postJson(path, obj, cb){{
  if (demoMode){{
    try{{ showToast('DEMO: 書き込みは無効です', '', null, 1800); }}catch(e){{}}
    try{{ cb(403, {{ ok:false, error:'demo mode' }}); }}catch(e2){{}}
    return;
  }}
  _xhrJson('POST', path, obj, cb);
}}

function refresh(){{
  getJson('/data.json?_=' + nowMs(), function(status, data){{
    if (_okStatus(status)){{
      render(data);
      if (typeof loadMemo !== 'undefined') loadMemo(false);
      return;
    }}
    var st = document.getElementById('status');
    if (st) st.textContent = '取得失敗（Wi‑Fi/サーバ確認）';
  }});
}}

function addTodo(prefix){{
  var msg = document.getElementById('todoMsg');
  var input = document.getElementById('todoInput');
  var raw = _trim(input && input.value ? input.value : '');
  if (!raw) {{
    if (msg) msg.textContent = '空です';
    return;
  }}
  if (msg) msg.textContent = '送信中...';

  var text = (prefix || '') + raw;
  var path = (todoMode === 'google') ? '/gtasks/add' : '/todo/add';
  var payload = (todoMode === 'google') ? {{ text: text }} : {{ text: text, top: true }};
  postJson(path, payload, function(status, out){{
    if (!_okStatus(status) || !out || !out.ok) {{
      if (msg) msg.textContent = (out && out.error) ? out.error : ('エラー ' + status);
      showToast('追加失敗', '', null, 1800);
      return;
    }}
    if (input) input.value = '';
    if (msg) msg.textContent = '';
    showToast('追加しました', '', null, 1200);
    refresh();
  }});
}}

function completeLocal(index){{
  var msg = document.getElementById('todoMsg');
  var text = '';
  try{{ text = (lastData && lastData.todo && lastData.todo[index]) ? lastData.todo[index] : ''; }}catch(e){{}}
  if (msg) msg.textContent = '更新中...';
  postJson('/todo/delete', {{ index: index }}, function(status, out){{
    if (!_okStatus(status) || !out || !out.ok) {{
      if (msg) msg.textContent = (out && out.error) ? out.error : ('エラー ' + status);
      return;
    }}
    if (msg) msg.textContent = '';
    showToast('完了', '戻す', function(){{ if (text) addLocalUndo(text); }}, 5000);
    refresh();
  }});
}}

function addLocalUndo(text){{
  postJson('/todo/add', {{ text: text, top: true }}, function(status, out){{
    if (!_okStatus(status) || !out || !out.ok) {{
      showToast('戻せませんでした', '', null, 2200);
      return;
    }}
    showToast('戻しました', '', null, 1200);
    refresh();
  }});
}}

function completeGTask(id){{
  var msg = document.getElementById('todoMsg');
  if (msg) msg.textContent = '更新中...';
  postJson('/gtasks/complete', {{ id: id }}, function(status, out){{
    if (!_okStatus(status) || !out || !out.ok) {{
      if (msg) msg.textContent = (out && out.error) ? out.error : ('エラー ' + status);
      return;
    }}
    if (msg) msg.textContent = '';
    showToast('完了', '戻す', function(){{ uncompleteGTask(id); }}, 5000);
    refresh();
  }});
}}

function uncompleteGTask(id){{
  postJson('/gtasks/uncomplete', {{ id: id }}, function(status, out){{
    if (!_okStatus(status) || !out || !out.ok) {{
      showToast((out && out.error) ? out.error : '戻せませんでした', '', null, 2200);
      return;
    }}
    showToast('戻しました', '', null, 1200);
    refresh();
  }});
}}

// memo (editable on PC/phone; kiosk is view-only)
var memoLastMtime = 0;
var memoLastText = '';
var memoEditing = false;
function memoSetMsg(t){{ var el = document.getElementById('memoMsg'); if (el) el.textContent = t || ''; }}
function memoSetHint(t){{ var el = document.getElementById('memoHint'); if (el) el.textContent = t || ''; }}
function memoSyncUi(){{
  var row = document.getElementById('memoEditRow');
  var ta = document.getElementById('memoInput');
  var view = document.getElementById('memoView');
  var bEdit = document.getElementById('memoEditBtn');
  var bSave = document.getElementById('memoSaveBtn');
  var bCancel = document.getElementById('memoCancelBtn');

  if (typeof kioskMode !== 'undefined' && kioskMode) {{
    memoEditing = false;
    if (row) row.style.display = 'none';
    if (ta) ta.style.display = 'none';
    if (view) view.style.display = 'block';
    return;
  }}

  if (row) row.style.display = '';
  if (memoEditing) {{
    if (view) view.style.display = 'none';
    if (ta) ta.style.display = 'block';
    if (bEdit) bEdit.style.display = 'none';
    if (bSave) bSave.style.display = 'inline-block';
    if (bCancel) bCancel.style.display = 'inline-block';
    memoSetHint('Ctrl/⌘ + Enter で保存');
  }} else {{
    if (view) view.style.display = 'block';
    if (ta) ta.style.display = 'none';
    if (bEdit) bEdit.style.display = 'inline-block';
    if (bSave) bSave.style.display = 'none';
    if (bCancel) bCancel.style.display = 'none';
    memoSetHint('');
  }}
}}
function memoStartEdit(){{
  if (typeof kioskMode !== 'undefined' && kioskMode) return;
  memoEditing = true;
  var ta = document.getElementById('memoInput');
  if (ta) ta.value = memoLastText || '';
  memoSyncUi();
  try{{ if (ta) {{ ta.focus(); ta.selectionStart = ta.value.length; ta.selectionEnd = ta.value.length; }} }}catch(e){{}}
}}
function memoCancel(){{
  memoEditing = false;
  memoSyncUi();
}}
function memoSave(){{
  if (typeof kioskMode !== 'undefined' && kioskMode) return;
  var ta = document.getElementById('memoInput');
  var text = ta ? (ta.value || '') : '';
  memoSetMsg('保存中...');
  postJson('/memo/set', {{ text: text }}, function(status, out){{
    if (!_okStatus(status) || !out || !out.ok) {{
      memoSetMsg((out && out.error) ? out.error : ('エラー ' + status));
      showToast('メモ保存失敗', '', null, 2000);
      return;
    }}
    if (out.mtime) memoLastMtime = out.mtime;
    memoLastText = text;
    memoEditing = false;
    memoSyncUi();
    loadMemo(true);
    showToast('メモ保存', '', null, 1200);
  }});
}}
function loadMemo(force){{
  getJson('/memo.json?_=' + nowMs(), function(status, out){{
    if (!_okStatus(status) || !out || !out.ok) return;
    if (!force && out.mtime && memoLastMtime && out.mtime == memoLastMtime) return;
    if (out.mtime) memoLastMtime = out.mtime;
    var view = document.getElementById('memoView');
    memoLastText = out.memo || '';
    if (view) view.textContent = memoLastText;
    var ta = document.getElementById('memoInput');
    if (ta && !memoEditing) ta.value = memoLastText;
    if (out.mtime) memoSetMsg('メモ: ' + new Date(out.mtime * 1000).toLocaleTimeString());
  }});
}}

	setTimeout(function(){{
	  var input = document.getElementById('todoInput');
	  if (input){{
	    input.addEventListener('keydown', function(ev){{
      ev = ev || window.event;
      var code = ev.keyCode || ev.which || 0;
      var key = ev.key || '';
      if (key === 'Enter' || code === 13){{
        if (ev.preventDefault) ev.preventDefault();
        addTodo('');
      }}
	    }}, false);
	  }}
	  var ta = document.getElementById('memoInput');
	  if (ta){{
	    ta.addEventListener('keydown', function(ev){{
	      ev = ev || window.event;
	      var code = ev.keyCode || ev.which || 0;
	      var key = ev.key || '';
	      var enter = (key === 'Enter' || code === 13);
	      if (enter && (ev.ctrlKey || ev.metaKey)){{
	        if (ev.preventDefault) ev.preventDefault();
	        memoSave();
	      }} else if (key === 'Escape' || code === 27){{
	        memoCancel();
	      }}
	    }}, false);
	  }}
	  memoSyncUi();
	  loadMemo(true);
	}}, 0);

	refresh();
	setInterval(function(){{ try{{ if (lastData) renderNextText(lastData); }}catch(e){{}} }}, 30 * 1000);
	setInterval(refresh, {REFRESH_SEC} * 1000);
	</script>
	</body>
	</html>
"""


TIMER_HTML = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#060a11">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<title>Timer</title>
<style>
  :root {
    --bg: #060a11;
    --bg2: #0b1220;
    --card: #0f1b2e;
    --card2: #0a1324;
    --bd: rgba(255,255,255,.10);
    --bd2: rgba(255,255,255,.16);
    --fg: #e7eefc;
    --muted: #a8b8d3;
    --accent: #5eead4;
    --accent2: #a78bfa;
    --warn: #fbbf24;
  }
  body {
    margin:0;
    font-family: -apple-system,BlinkMacSystemFont,"Helvetica Neue",Arial,"Hiragino Kaku Gothic ProN","Noto Sans JP",sans-serif;
    background:
      radial-gradient(900px 600px at -10% -10%, rgba(94,234,212,.14), transparent 60%),
      radial-gradient(900px 600px at 110% 15%, rgba(167,139,250,.11), transparent 55%),
      radial-gradient(800px 560px at 60% 120%, rgba(96,165,250,.08), transparent 55%),
      linear-gradient(180deg, var(--bg2), var(--bg));
    background-color:var(--bg);
    color:var(--fg);
    color-scheme: dark;
    -webkit-font-smoothing: antialiased;
    text-rendering: optimizeLegibility;
  }
  .wrap { padding:16px; }
  .topbar { display:flex; align-items:center; flex-wrap:wrap; }
  .topbar > * { margin-right:12px; }
  .topbar > *:last-child { margin-right:0; }
  .btn {
    padding:10px 12px;
    background:rgba(255,255,255,.06);
    background:linear-gradient(180deg, rgba(255,255,255,.08), rgba(255,255,255,.04));
    color:var(--fg);
    border:1px solid var(--bd2);
    border-radius:12px;
    text-decoration:none;
    display:inline-block;
    white-space:nowrap;
  }
  .btn:active { opacity:0.92; transform: translateY(1px); }
  .overlay { position:fixed; top:0; left:0; right:0; bottom:0; display:none; align-items:center; justify-content:center; background:rgba(0,0,0,.65); z-index:9999; }
	  .overlay .box { max-width:420px; margin:20px; padding:18px; border-radius:16px; background:var(--card); border:1px solid var(--bd2); text-align:center; }
	  .overlay .big2 { font-size:18px; font-weight:800; margin-bottom:8px; }
	  .overlay .small { color:var(--muted); font-size:12px; line-height:1.4; }
	  body.fs .wrap { padding-top:76px; }
	  body.fs.hidebar .wrap { padding-top:16px; }
	  body.fs .topbar { position:fixed; top:0; left:0; right:0; z-index:1000; background:rgba(6,10,17,.82); padding:12px 16px; backdrop-filter: blur(6px); -webkit-backdrop-filter: blur(6px); }
	  body.fs .topbar { transition: transform .2s ease, opacity .2s ease; }
	  body.fs.hidebar .topbar { transform: translateY(-100%); opacity:0; pointer-events:none; }
	  .big { font-size:58px; font-weight:900; letter-spacing:1px; margin-top:18px; font-variant-numeric: tabular-nums; }
  .sub { font-size:16px; color:var(--muted); margin-top:6px; display:flex; flex-wrap:wrap; gap:10px; align-items:baseline; }
  .sub .sep { opacity:.55; }
  .sub .wx { color:var(--fg); font-weight:800; }
  .mode { font-size:14px; color:var(--muted); margin-top:8px; }
  .row { display:flex; flex-wrap:wrap; margin-top:16px; }
  .row > * { margin-right:10px; margin-bottom:10px; }
  .pill { padding:12px 14px; background:linear-gradient(180deg, var(--card), var(--card2)); border:1px solid var(--bd); border-radius:16px; }
  .muted { color:var(--muted); font-size:12px; margin-top:8px; }
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <a class="btn" href="/">予定+ToDo</a>
    <a class="btn" href="/timer">時計+タイマー</a>
    <button class="btn" id="fsBtn" onclick="toggleFullscreen()">全画面</button>
    <button class="btn" onclick="resetAll()">リセット</button>
    <span class="muted" id="host"></span>
  </div>

  <div class="big" id="clock">--:--</div>
  <div class="sub">
    <span id="clockDate">----</span>
    <span class="sep">・</span>
    <span id="wxText" class="wx">天気 --</span>
  </div>
  <div class="mode" id="stateText">停止中</div>

  <div class="row">
    <button class="btn" onclick="startPomodoro(25, 5)">25/5</button>
    <button class="btn" onclick="startPomodoro(50, 10)">50/10</button>
    <button class="btn" onclick="startBreak(5)">休憩5</button>
    <button class="btn" onclick="startBreak(10)">休憩10</button>
    <button class="btn" onclick="togglePause()">一時停止/再開</button>
  </div>

  <div class="pill">
    <div style="font-weight:800;">カウントダウン</div>
    <div class="big" id="timer">--:--</div>
    <div class="muted">※ 終了時は画面点滅（音は端末依存で鳴らないことがあります）</div>
  </div>
</div>

<div id="fsOverlay" class="overlay" onclick="overlayTap()">
  <div class="box">
    <div class="big2">タップで全画面</div>
    <div class="small">アドレスバー非表示で「机上スクリーン」が安定します。</div>
    <div class="small" style="margin-top:10px;">もう一度タップで解除（端末の戻る/ESCでもOK）</div>
  </div>
</div>

<script>
function pad(n){ return (n<10?'0':'')+n; }
function isFullscreen(){
  return !!(document.fullscreenElement || document.webkitFullscreenElement || document.mozFullScreenElement || document.msFullscreenElement);
}
function requestFullscreen(){
  var el = document.documentElement;
  if (el.requestFullscreen) return el.requestFullscreen();
  if (el.webkitRequestFullscreen) return el.webkitRequestFullscreen();
  if (el.mozRequestFullScreen) return el.mozRequestFullScreen();
  if (el.msRequestFullscreen) return el.msRequestFullscreen();
}
function exitFullscreen(){
  if (document.exitFullscreen) return document.exitFullscreen();
  if (document.webkitExitFullscreen) return document.webkitExitFullscreen();
  if (document.mozCancelFullScreen) return document.mozCancelFullScreen();
  if (document.msExitFullscreen) return document.msExitFullscreen();
}
function isStandalone(){
  try{
    if (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches) return true;
    if (window.navigator && window.navigator.standalone) return true;
  }catch(e){}
  return false;
}
function isTouchDevice(){
  return ('ontouchstart' in window) || (navigator.maxTouchPoints && navigator.maxTouchPoints > 0);
}
function getFsPref(){
  var v = '';
  try{ v = localStorage.getItem('desk_fs_pref') || ''; }catch(e){}
  if (!v){
    v = isTouchDevice() ? '1' : '0';
    try{ localStorage.setItem('desk_fs_pref', v); }catch(e){}
  }
  return v === '1';
}
function setOverlay(show){
  var o = document.getElementById('fsOverlay');
  if (!o) return;
  o.style.display = show ? 'flex' : 'none';
}
function updateFsUi(){
  var fs = isFullscreen();
  if (fs) document.body.classList.add('fs');
  else document.body.classList.remove('fs');
  document.body.classList.remove('hidebar');
  var btn = document.getElementById('fsBtn');
  if (btn) btn.textContent = fs ? '全画面解除' : '全画面';
  if (!fs && getFsPref() && !isStandalone()) setOverlay(true);
  else setOverlay(false);
  kickBarTimer();
}
function toggleFullscreen(){
  if (isFullscreen()){ exitFullscreen(); return; }
  try{
    var p = requestFullscreen();
    if (p && p.catch) p.catch(function(){});
  }catch(e){}
}
function kickBarTimer(){
  if (!document.body.classList.contains('fs')) return;
  if (window._barTimer) clearTimeout(window._barTimer);
  window._barTimer = setTimeout(function(){
    document.body.classList.add('hidebar');
  }, 5000);
}
function showBar(){
  document.body.classList.remove('hidebar');
  kickBarTimer();
}
function hideAddressBar(){
  setTimeout(function(){
    try{ window.scrollTo(0, 1); }catch(e){}
  }, 50);
}
['click','touchstart','mousemove','keydown'].forEach(function(ev){
  document.addEventListener(ev, showBar, false);
});
document.addEventListener('fullscreenchange', updateFsUi, false);
document.addEventListener('webkitfullscreenchange', updateFsUi, false);
document.addEventListener('mozfullscreenchange', updateFsUi, false);
document.addEventListener('MSFullscreenChange', updateFsUi, false);
window.addEventListener('orientationchange', hideAddressBar, false);
window.addEventListener('load', hideAddressBar, false);
setTimeout(hideAddressBar, 500);

function overlayTap(){
  setOverlay(false);
  toggleFullscreen();
}
updateFsUi();
document.getElementById('host').textContent = location.host;

function tickClock(){
  var d = new Date();
  document.getElementById('clock').textContent = pad(d.getHours()) + ':' + pad(d.getMinutes());
  var dd = document.getElementById('clockDate');
  if (dd) dd.textContent = (d.getMonth()+1) + '/' + d.getDate() + ' (' + '日月火水木金土'[d.getDay()] + ')';
}
setInterval(tickClock, 1000 * 5);
tickClock();

function nowMs(){
  return (new Date()).getTime();
}

function shortErr(s){
  s = (s || '').toString();
  if (s.length > 90) return s.slice(0, 90) + '...';
  return s;
}

function renderWeather(data){
  var el = document.getElementById('wxText');
  if (!el) return;
  var w = (data && data.weather) ? data.weather : {};
  var err = (data && data.weather_error) ? ('' + data.weather_error) : '';
  if (w && w.enabled === false){
    el.textContent = '天気 OFF';
    el.style.opacity = '0.6';
    return;
  }
  el.style.opacity = '1';
  var name = (w && w.name) ? ('' + w.name) : '';
  var text = (w && w.text) ? ('' + w.text) : '';
  var temp = '';
  if (w && typeof w.temp_c_int !== 'undefined' && w.temp_c_int !== null){
    temp = '' + w.temp_c_int + '℃';
  } else if (w && typeof w.temp_c !== 'undefined' && w.temp_c !== null){
    temp = '' + w.temp_c + '℃';
  }
  var line = '';
  if (text) line = text;
  if (temp) line = line ? (line + ' ' + temp) : temp;
  if (name && name !== '天気') line = line ? (name + ' ' + line) : name;
  if (!line) line = err ? ('天気: ' + shortErr(err)) : '天気 --';
  el.textContent = line;
}

function getJson(url, cb){
  var xhr = null;
  try{ xhr = new XMLHttpRequest(); }catch(e){ cb(0, {}); return; }
  try{ xhr.open('GET', url, true); }catch(e){ cb(0, {}); return; }
  try{ xhr.timeout = 8000; }catch(e){}
  xhr.onreadystatechange = function(){
    if (xhr.readyState !== 4) return;
    var out = {};
    try{ out = JSON.parse(xhr.responseText || '{}'); }catch(e){ out = {}; }
    cb(xhr.status || 0, out);
  };
  xhr.onerror = function(){ cb(0, {}); };
  xhr.ontimeout = function(){ cb(0, {}); };
  try{ xhr.setRequestHeader('Accept', 'application/json'); }catch(e){}
  try{ xhr.send(null); }catch(e){ cb(0, {}); }
}

function refreshWeather(){
  getJson('/status.json?_=' + nowMs(), function(status, out){
    if (status >= 200 && status < 300){
      renderWeather(out);
      try{
        var rev = parseInt(out.ui_rev || '0', 10) || 0;
        if (!window._uiRev) window._uiRev = rev;
        else if (rev && window._uiRev && rev !== window._uiRev) location.reload();
      }catch(e){}
    }
  });
}
refreshWeather();
setInterval(refreshWeather, 10 * 60 * 1000);

var state = {
  running: false,
  paused: false,
  mode: 'stop', // 'work' or 'break'
  endAt: 0,
  remainSec: 0,
  cycle: 0, // work->break count
  _breakMin: 0,
};

function loadState(){
  try{
    var s = localStorage.getItem('desk_timer_state');
    if (s){
      var obj = JSON.parse(s);
      for (var k in obj){
        if (Object.prototype.hasOwnProperty.call(obj, k)){
          state[k] = obj[k];
        }
      }
    }
  }catch(e){}
}
function saveState(){
  try{ localStorage.setItem('desk_timer_state', JSON.stringify(state)); }catch(e){}
}
loadState();

function setText(){
  var st = document.getElementById('stateText');
  if (!state.running) st.textContent = '停止中';
  else if (state.paused) st.textContent = '一時停止中';
  else st.textContent = (state.mode === 'work' ? '作業中' : '休憩中') + '（サイクル ' + state.cycle + '）';
}

function renderTimer(sec){
  if (sec < 0) sec = 0;
  var m = Math.floor(sec / 60);
  var s = sec % 60;
  document.getElementById('timer').textContent = pad(m) + ':' + pad(s);
}

function flash(){
  var prevBg = '';
  var prevBgColor = '';
  try{
    prevBg = document.body.style.background || '';
    prevBgColor = document.body.style.backgroundColor || '';
  }catch(e){}
  var n = 0;
  var id = setInterval(function(){
    var c = (n % 2 === 0) ? '#2a0f14' : '#060a11';
    document.body.style.background = c;
    document.body.style.backgroundColor = c;
    n++;
    if (n > 12){
      clearInterval(id);
      document.body.style.background = prevBg;
      document.body.style.backgroundColor = prevBgColor;
    }
  }, 250);
}

function startWork(min){
  var now = nowMs();
  state.running = true;
  state.paused = false;
  state.mode = 'work';
  state.endAt = now + min * 60 * 1000;
  state.remainSec = min * 60;
  saveState();
  setText();
}
function startBreak(min){
  var now = nowMs();
  state.running = true;
  state.paused = false;
  state.mode = 'break';
  state.endAt = now + min * 60 * 1000;
  state.remainSec = min * 60;
  saveState();
  setText();
}

function startPomodoro(workMin, breakMin){
  state.cycle = state.cycle + 1;
  state._breakMin = breakMin;
  startWork(workMin);
  saveState();
}

function togglePause(){
  if (!state.running) return;
  if (!state.paused){
    state.paused = true;
    state.remainSec = Math.max(0, Math.floor((state.endAt - nowMs()) / 1000));
  }else{
    state.paused = false;
    state.endAt = nowMs() + state.remainSec * 1000;
  }
  saveState();
  setText();
}

function resetAll(){
  state = { running:false, paused:false, mode:'stop', endAt:0, remainSec:0, cycle:0, _breakMin:0 };
  saveState();
  setText();
  renderTimer(0);
}

function step(){
  setText();

  if (!state.running){
    renderTimer(0);
    return;
  }

  if (state.paused){
    renderTimer(state.remainSec);
    return;
  }

  var sec = Math.floor((state.endAt - nowMs()) / 1000);
  renderTimer(sec);

  if (sec <= 0){
    flash();
    if (state.mode === 'work' && state._breakMin){
      var bm = state._breakMin;
      startBreak(bm);
    }else{
      state.running = false;
      state.mode = 'stop';
      state.paused = false;
      saveState();
    }
  }
}

step();
setInterval(step, 250);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def _is_demo(self) -> bool:
        if _env_bool("DESK_SCREEN_DEMO", False):
            return True
        try:
            qs = urllib.parse.parse_qs(urlparse(self.path).query, keep_blank_values=True)
            v = (qs.get("demo") or [""])[0]
            return str(v).strip().lower() in ("1", "true", "yes", "on")
        except Exception:
            return False

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        # Keep stdout/stderr quiet (useful for launchd).
        return

    def _send(self, body: bytes, content_type: str = "text/html; charset=utf-8", code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj: dict, code: int = 200) -> None:
        self._send(
            json.dumps(obj, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            code,
        )

    def _read_body_bytes(self, max_bytes: int = 8192) -> Optional[bytes]:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except Exception:
            length = 0
        if length <= 0:
            return b""
        if length > max_bytes:
            self._send(b"Payload Too Large", "text/plain; charset=utf-8", 413)
            return None
        return self.rfile.read(length)

    def _read_body_params(self) -> Optional[dict]:
        raw = self._read_body_bytes()
        if raw is None:
            return None

        ctype = (self.headers.get("Content-Type", "") or "").split(";")[0].strip().lower()
        if ctype == "application/json":
            try:
                return json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                return {}

        # form fallback
        try:
            qs = urllib.parse.parse_qs(raw.decode("utf-8"), keep_blank_values=True)
            out = {}
            for k, v in qs.items():
                if not v:
                    continue
                out[k] = v[0]
            return out
        except Exception:
            return {}

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        demo = self._is_demo()

        if path in ("/", "/index.html"):
            self._send(INDEX_HTML.encode("utf-8"))
            return

        if path in ("/timer", "/timer/"):
            self._send(TIMER_HTML.encode("utf-8"))
            return

        if path == "/data.json":
            payload = build_demo_payload() if demo else build_payload()
            self._send(
                json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                "application/json; charset=utf-8",
            )
            return

        if path == "/status.json":
            if demo:
                p = build_demo_payload()
                weather = dict(p.get("weather") or {})
                weather_err = str(p.get("weather_error") or "")
            else:
                weather, weather_err = read_weather_cached()
            self._send_json(
                {"ok": True, "now": int(time.time()), "ui_rev": UI_REV, "weather": weather, "weather_error": weather_err}
            )
            return

        if path == "/memo.json":
            if demo:
                text = "【DEMO】\n・このメモはサンプル表示です\n・kiosk では閲覧のみ\n・PC/スマホから編集できます\n"
                mtime = int(time.time())
            else:
                text, mtime = memo_read()
            self._send_json({"ok": True, "memo": text, "mtime": mtime})
            return

        if path in ("/healthz", "/health"):
            self._send(b"ok", "text/plain; charset=utf-8")
            return

        self._send(b"Not Found", "text/plain; charset=utf-8", 404)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if self._is_demo():
            self._send_json({"ok": False, "error": "demo mode (read-only)"}, 403)
            return

        if path == "/todo/add":
            ok, msg = _todo_check_auth(self.headers.get("X-Desk-Token", ""))
            if not ok:
                self._send_json({"ok": False, "error": msg}, 401)
                return

            params = self._read_body_params()
            if params is None:
                return

            text = str(params.get("text") or "")
            top_raw = params.get("top")
            top = True if top_raw is None else str(top_raw).strip().lower() not in ("0", "false", "no", "off")

            try:
                lines = todo_add(text, top=top)
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 400)
                return

            self._send_json({"ok": True, "todo": lines[:MAX_TODO_LINES], "todo_total": len(lines)})
            return

        if path == "/todo/delete":
            ok, msg = _todo_check_auth(self.headers.get("X-Desk-Token", ""))
            if not ok:
                self._send_json({"ok": False, "error": msg}, 401)
                return

            params = self._read_body_params()
            if params is None:
                return

            try:
                index = int(params.get("index"))
            except Exception:
                self._send_json({"ok": False, "error": "invalid index"}, 400)
                return

            lines = todo_delete(index)
            self._send_json({"ok": True, "todo": lines[:MAX_TODO_LINES], "todo_total": len(lines)})
            return

        if path == "/memo/set":
            ok, msg = _todo_check_auth(self.headers.get("X-Desk-Token", ""))
            if not ok:
                self._send_json({"ok": False, "error": msg}, 401)
                return

            params = self._read_body_params()
            if params is None:
                return
            if not isinstance(params, dict):
                params = {}

            text = params.get("text")
            mtime = memo_write("" if text is None else str(text))
            self._send_json({"ok": True, "mtime": mtime})
            return

        if path == "/gtasks/add":
            ok, msg = _todo_check_auth(self.headers.get("X-Desk-Token", ""))
            if not ok:
                self._send_json({"ok": False, "error": msg}, 401)
                return

            params = self._read_body_params()
            if params is None:
                return
            if not isinstance(params, dict):
                params = {}

            title = str(params.get("title") or params.get("text") or "")
            notes = str(params.get("notes") or "")

            try:
                task = gtasks_add(title, notes=notes)
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 400)
                return

            self._send_json({"ok": True, "task": {"id": task.get("id", ""), "title": task.get("title", "")}})
            return

        if path == "/gtasks/complete":
            ok, msg = _todo_check_auth(self.headers.get("X-Desk-Token", ""))
            if not ok:
                self._send_json({"ok": False, "error": msg}, 401)
                return

            params = self._read_body_params()
            if params is None:
                return
            if not isinstance(params, dict):
                params = {}

            task_id = str(params.get("id") or "")
            try:
                gtasks_complete(task_id)
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 400)
                return

            self._send_json({"ok": True})
            return

        if path == "/gtasks/uncomplete":
            ok, msg = _todo_check_auth(self.headers.get("X-Desk-Token", ""))
            if not ok:
                self._send_json({"ok": False, "error": msg}, 401)
                return

            params = self._read_body_params()
            if params is None:
                return
            if not isinstance(params, dict):
                params = {}

            task_id = str(params.get("id") or "")
            try:
                gtasks_uncomplete(task_id)
            except Exception as e:
                self._send_json({"ok": False, "error": str(e)}, 400)
                return

            self._send_json({"ok": True})
            return

        self._send(b"Not Found", "text/plain; charset=utf-8", 404)


def _local_ips() -> list[str]:
    ips: list[str] = []
    try:
        import subprocess

        for iface in ("en0", "en1", "bridge0"):
            p = subprocess.run(["ipconfig", "getifaddr", iface], capture_output=True, text=True, check=False)
            s = (p.stdout or "").strip()
            if s and s not in ips:
                ips.append(s)
    except Exception:
        pass

    # Fallback: best-effort "default route" IP
    if not ips:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            if ip:
                ips.append(ip)
        except Exception:
            pass

    return ips


class _ThreadingHTTPServerV6(ThreadingHTTPServer):
    address_family = socket.AF_INET6

    def server_bind(self) -> None:  # noqa: D401
        # Bind IPv6-only so we can also keep an IPv4 listener on the same port.
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        except Exception:
            pass
        super().server_bind()


def main() -> None:
    _acquire_lock_or_exit()

    preferred = _parse_port(PORT_ENV) if PORT_ENV else None
    httpd, port = _create_server(HOST, preferred)

    # iOS/Safari sometimes prefers IPv6 (e.g. Bonjour `.local` may resolve to IPv6),
    # so also listen on IPv6 on the same port when we are bound to all IPv4 interfaces.
    httpd6: Optional[ThreadingHTTPServer] = None
    if HOST == "0.0.0.0":
        try:
            httpd6 = _ThreadingHTTPServerV6(("::", port), Handler)
        except OSError:
            httpd6 = None
        except Exception:
            httpd6 = None

    _write_port_file(port)

    urls = [f"http://{ip}:{port}/" for ip in _local_ips()] or [f"http://127.0.0.1:{port}/"]
    print(f"Desk Screen running (port {port})")
    for u in urls:
        print(f"  {u}")
        print(f"  {u}timer")

    if httpd6 is not None:
        t = threading.Thread(target=httpd6.serve_forever, daemon=True)
        t.start()
    httpd.serve_forever()


if __name__ == "__main__":
    main()
