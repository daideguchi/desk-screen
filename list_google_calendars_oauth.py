#!/usr/bin/env python3
import sys


def main() -> int:
    try:
        import server  # noqa: PLC0415
    except Exception as e:
        print(f"failed to import server.py: {e}", file=sys.stderr)
        return 1

    tok = server._read_google_user_oauth_token()
    if not tok:
        print("missing user_oauth_token.json (run: ~/desk oauth)", file=sys.stderr)
        return 2

    missing = server._google_user_missing_scopes(tok, server.GOOGLE_CALENDAR_SCOPES)
    if missing:
        print("OAuth token missing calendar scope (rerun: ~/desk oauth)", file=sys.stderr)
        return 3

    try:
        access_token, _exp = server._google_user_get_access_token()
        items = server._google_calendar_list_calendars(access_token, max_results=250)
    except Exception as e:
        print(f"failed to list calendars: {e}", file=sys.stderr)
        return 4

    if not items:
        print("no calendars found", file=sys.stderr)
        return 5

    print("selected\tprimary\trole\tsummary\tid")
    for it in items:
        if not isinstance(it, dict):
            continue
        cid = str(it.get("id") or "").strip()
        if not cid:
            continue
        selected = "1" if bool(it.get("selected")) else "0"
        primary = "1" if bool(it.get("primary")) else "0"
        role = str(it.get("accessRole") or "").strip()
        summary = str(it.get("summary") or "").strip().replace("\t", " ")
        print(f"{selected}\t{primary}\t{role}\t{summary}\t{cid}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

