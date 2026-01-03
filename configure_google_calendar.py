#!/usr/bin/env python3
import json
import os
import sys
import time
from typing import List


def main() -> int:
    base_dir = os.path.dirname(os.path.abspath(__file__))
    conf_path = os.path.join(base_dir, "google_calendar.json")
    arg_ids = [s.strip() for s in sys.argv[1:] if s.strip()]

    try:
        import server  # noqa: PLC0415
    except Exception as e:
        print(f"failed to import server.py: {e}", file=sys.stderr)
        return 1

    cfg = server._read_google_calendar_config()
    sa_path = str(cfg.get("service_account_json") or os.path.join(base_dir, "credentials", "service_account.json"))
    impersonate = str(cfg.get("impersonate") or "")

    if not os.path.exists(sa_path):
        print(f"service account json not found: {sa_path}", file=sys.stderr)
        return 1

    try:
        token, _exp = server._google_get_access_token(sa_path, impersonate)
    except Exception as e:
        print(f"google token failed: {e}", file=sys.stderr)
        return 2

    calendar_ids: List[str] = []

    if arg_ids:
        # Use explicit IDs (recommended; service accounts may not list shared calendars).
        for cid in arg_ids:
            if cid not in calendar_ids:
                calendar_ids.append(cid)
    else:
        # Try discovery via calendarList.list (may be empty for service accounts).
        try:
            data = server._http_get_json(
                "https://www.googleapis.com/calendar/v3/users/me/calendarList?maxResults=250",
                headers={"Authorization": f"Bearer {token}"},
            )
        except Exception as e:
            print(f"google calendarList.list failed: {e}", file=sys.stderr)
            return 2

        items = data.get("items", []) or []
        for it in items:
            cid = str(it.get("id") or "").strip()
            if cid and cid not in calendar_ids:
                calendar_ids.append(cid)

    if not calendar_ids:
        print("no calendars found.", file=sys.stderr)
        print("Share your Google Calendar with this email:", file=sys.stderr)
        print("  youtube@srtfile-468804.iam.gserviceaccount.com", file=sys.stderr)
        print("", file=sys.stderr)
        print("Then pass your calendarId explicitly, e.g.:", file=sys.stderr)
        print("  ./configure_google_calendar.py your_calendar_id", file=sys.stderr)
        return 3

    # Validate access (best-effort). If a calendarId is wrong or not shared, Google returns 404.
    time_min = "1970-01-01T00:00:00Z"
    time_max = "2100-01-01T00:00:00Z"
    ok_ids: List[str] = []
    for cid in calendar_ids:
        try:
            server._google_list_events(token, cid, time_min, time_max, max_results=1)
            ok_ids.append(cid)
        except Exception as e:
            print(f"not accessible: {cid} ({e})", file=sys.stderr)

    if not ok_ids:
        print("no accessible calendars. Confirm share + calendarId.", file=sys.stderr)
        return 4

    calendar_ids = ok_ids

    if os.path.exists(conf_path):
        ts = time.strftime("%Y%m%d_%H%M%S")
        bak = conf_path + f".bak_{ts}"
        os.replace(conf_path, bak)

    conf = {
        "enabled": True,
        "service_account_json": "credentials/service_account.json",
        "calendar_ids": calendar_ids,
    }
    with open(conf_path, "w", encoding="utf-8") as f:
        json.dump(conf, f, ensure_ascii=False, indent=2)

    try:
        os.chmod(conf_path, 0o600)
    except Exception:
        pass

    print(f"wrote {conf_path} (calendar_ids={len(calendar_ids)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
