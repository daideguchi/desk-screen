#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ ! -f port.txt ]]; then
  echo "port.txt がありません。先に server.py を起動してください。" >&2
  exit 1
fi

port="$(tr -d ' \n\r\t' < port.txt || true)"
if [[ -z "${port}" ]]; then
  echo "port.txt の内容が不正です。" >&2
  exit 1
fi

ips=()
for iface in en0 en1 bridge0; do
  ip="$(ipconfig getifaddr "$iface" 2>/dev/null || true)"
  if [[ -n "${ip}" ]]; then
    ips+=("$ip")
  fi
done

if [[ ${#ips[@]} -eq 0 ]]; then
  ips=("127.0.0.1")
fi

echo "Desk Screen URLs (port ${port})"
lh="$(scutil --get LocalHostName 2>/dev/null || true)"
for ip in "${ips[@]}"; do
  echo "  http://${ip}:${port}/"
  echo "  http://${ip}:${port}/?kiosk=1"
  echo "  http://${ip}:${port}/timer"
done

if [[ -n "${lh}" ]]; then
  echo "  http://${lh}.local:${port}/"
  echo "  http://${lh}.local:${port}/?kiosk=1"
  echo "  http://${lh}.local:${port}/timer"
fi
