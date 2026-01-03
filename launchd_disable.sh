#!/usr/bin/env bash
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
label="com.dd.desk_screen"
dest="${HOME}/Library/LaunchAgents/${label}.plist"
uid="$(id -u)"

if [[ -f "${dest}" ]]; then
  launchctl bootout "gui/${uid}" "${dest}" >/dev/null 2>&1 || true
  rm -f "${dest}"
  echo "launchd disabled: ${label}"
else
  echo "not installed: ${dest}"
fi
