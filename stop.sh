#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -f port.txt ]]; then
  echo "port.txt がありません（起動していない可能性）" >&2
  exit 1
fi

port="$(tr -d ' \n\r\t' < port.txt || true)"
if [[ -z "${port}" ]]; then
  echo "port.txt の内容が不正です。" >&2
  exit 1
fi

target_pids="$(/usr/sbin/lsof -iTCP:"${port}" -sTCP:LISTEN -n -P -t 2>/dev/null || true)"

if [[ -z "${target_pids}" ]]; then
  echo "no listener on port ${port}"
  exit 0
fi

echo "stopping pids on port ${port}: ${target_pids}"
for pid in ${target_pids}; do
  /bin/kill -TERM "${pid}" >/dev/null 2>&1 || true
done

sleep 0.3
if /usr/sbin/lsof -iTCP -sTCP:LISTEN -n -P | /usr/bin/grep -q ":${port} "; then
  echo "still listening on port ${port} (may take a moment)"; exit 0
fi

echo "stopped"
