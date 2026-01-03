#!/usr/bin/env python3
import os
import sys


def main() -> int:
    try:
        # Reuse the no-deps JWT + HTTP helpers from server.py
        from server import _google_get_access_token, _http_get_json, _read_google_calendar_config  # noqa: PLC0415
    except Exception as e:
        print(f"failed to import server helpers: {e}", file=sys.stderr)
        return 1

    cfg = _read_google_calendar_config()
    sa_path = str(cfg.get("service_account_json") or "")
    if not sa_path:
        print("missing service_account_json (expected credentials/service_account.json)", file=sys.stderr)
        return 1
    if not os.path.exists(sa_path):
        print(f"service_account_json not found: {sa_path}", file=sys.stderr)
        return 1

    impersonate = str(cfg.get("impersonate") or "")
    try:
        token, _exp = _google_get_access_token(sa_path, impersonate)
        data = _http_get_json(
            "https://www.googleapis.com/calendar/v3/users/me/calendarList?maxResults=250",
            headers={"Authorization": f"Bearer {token}"},
        )
    except Exception as e:
        print(f"google calendarList.list failed: {e}", file=sys.stderr)
        return 1

    items = data.get("items", []) or []
    if not items:
        print("no calendars found (share a calendar with the service account email first)", file=sys.stderr)
        return 2

    print("id\taccessRole\tsummary")
    for it in items:
        cid = str(it.get("id") or "").strip()
        role = str(it.get("accessRole") or "").strip()
        summary = str(it.get("summary") or "").strip()
        if not cid:
            continue
        print(f"{cid}\t{role}\t{summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

