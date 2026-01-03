#!/usr/bin/env python3
import argparse
import json
import os
import secrets
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer


HERE = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv(path: str) -> None:
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


_load_dotenv(os.path.join(HERE, ".env"))

DEFAULT_OUT = os.environ.get("DESK_SCREEN_GOOGLE_USER_TOKEN", os.path.join(HERE, "credentials", "user_oauth_token.json"))
DEFAULT_FROM = (os.environ.get("DESK_SCREEN_GOOGLE_OAUTH_FROM") or "").strip()

BASE_SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/tasks",
]


def _read_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _extract_client(creds: dict) -> tuple[str, str, str]:
    # Supports:
    # - Token JSON style: {client_id, client_secret, token_uri, ...}
    # - Google OAuth client secrets: {installed:{client_id, client_secret, token_uri}} or {web:{...}}
    for key in ("client_id", "client_secret"):
        if key not in creds and ("installed" in creds or "web" in creds):
            break
    else:
        client_id = str(creds.get("client_id") or "").strip()
        client_secret = str(creds.get("client_secret") or "").strip()
        token_uri = str(creds.get("token_uri") or "https://oauth2.googleapis.com/token").strip()
        return client_id, client_secret, token_uri

    node = creds.get("installed") or creds.get("web") or {}
    client_id = str(node.get("client_id") or "").strip()
    client_secret = str(node.get("client_secret") or "").strip()
    token_uri = str(node.get("token_uri") or "https://oauth2.googleapis.com/token").strip()
    return client_id, client_secret, token_uri


class _CallbackState:
    def __init__(self, expected_state: str) -> None:
        self.expected_state = expected_state
        self.code = ""
        self.error = ""
        self.event = threading.Event()


def _run_loopback_server(state: _CallbackState) -> tuple[HTTPServer, str]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:  # noqa: A002
            return

        def do_GET(self) -> None:  # noqa: N802
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            code = (qs.get("code") or [""])[0]
            err = (qs.get("error") or [""])[0]
            st = (qs.get("state") or [""])[0]

            if st and st != state.expected_state:
                state.error = "invalid state (possible CSRF)"
            elif err:
                state.error = err
            elif code:
                state.code = code
            else:
                state.error = "missing code"

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(
                (
                    "<!doctype html><meta charset='utf-8'>"
                    "<title>Desk Screen OAuth</title>"
                    "<body style='font-family:system-ui; padding:24px;'>"
                    "<h2>OK</h2>"
                    "<p>このタブは閉じて大丈夫です。</p>"
                    "</body>"
                ).encode("utf-8")
            )

            state.event.set()

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    host, port = httpd.server_address[0], int(httpd.server_address[1])
    redirect_uri = f"http://{host}:{port}/"
    return httpd, redirect_uri


def _post_form(url: str, form: dict) -> dict:
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
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description="One-time OAuth setup for Google Calendar + Google ToDo (Tasks).")
    ap.add_argument(
        "--from",
        dest="from_path",
        default=DEFAULT_FROM if os.path.exists(DEFAULT_FROM) else "",
        help="Source JSON to reuse client_id/client_secret (e.g. drive_oauth_token.json).",
    )
    ap.add_argument("--out", dest="out_path", default=DEFAULT_OUT, help="Output token JSON path.")
    ap.add_argument("--open", action="store_true", help="Try to open the auth URL in a browser (macOS: open).")
    ap.add_argument("--timeout-sec", type=int, default=900, help="OAuth callback wait timeout (default: 900).")
    args = ap.parse_args()

    if not args.from_path:
        ap.error("--from is required (no default source found)")

    src = _read_json(args.from_path)
    client_id, client_secret, token_uri = _extract_client(src)
    if not client_id or not client_secret:
        raise SystemExit("source JSON missing client_id/client_secret")

    expected_state = secrets.token_urlsafe(16)
    cb_state = _CallbackState(expected_state)
    httpd, redirect_uri = _run_loopback_server(cb_state)

    scopes = list(BASE_SCOPES)
    scope_str = " ".join(scopes)
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": scope_str,
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": expected_state,
        }
    )

    print("1) Open this URL and allow access:")
    print(auth_url)
    print("")
    print("2) After approval, you'll be redirected to a local page that says OK.")

    if args.open:
        try:
            import subprocess

            subprocess.run(["open", auth_url], check=False)
        except Exception:
            pass

    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()

    if not cb_state.event.wait(timeout=max(30, int(args.timeout_sec))):
        httpd.shutdown()
        raise SystemExit("timeout waiting for OAuth callback (try again)")

    httpd.shutdown()
    t.join(timeout=2)

    if cb_state.error:
        raise SystemExit(f"oauth error: {cb_state.error}")
    if not cb_state.code:
        raise SystemExit("oauth error: missing code")

    token = _post_form(
        token_uri,
        {
            "code": cb_state.code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
    )

    refresh_token = str(token.get("refresh_token") or "").strip()
    if not refresh_token:
        raise SystemExit(
            "no refresh_token returned.\n"
            "- Make sure you used a Desktop/Installed OAuth client.\n"
            "- If you've authorized before, revoke the app at https://myaccount.google.com/permissions and rerun."
        )

    out = {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "token_uri": token_uri,
        "scopes": scopes,
        "created_at": int(time.time()),
        "source": os.path.abspath(args.from_path),
    }

    out_path = os.path.abspath(args.out_path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, out_path)
    try:
        os.chmod(out_path, 0o600)
    except Exception:
        pass

    print("")
    print("Saved token (DO NOT SHARE):")
    print(out_path)
    print("")
    print("Next:")
    print("  ~/desk restart")
    print("  Open / and choose the 'Google' ToDo tab")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
