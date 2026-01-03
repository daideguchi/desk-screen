#!/usr/bin/env bash
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
label="com.dd.desk_screen"
dest="${HOME}/Library/LaunchAgents/${label}.plist"
python_bin="${DESK_SCREEN_PYTHON:-/usr/bin/python3}"

xml_escape() {
  # Minimal XML escape for plist strings.
  sed -e 's/&/&amp;/g' -e 's/</&lt;/g' -e 's/>/&gt;/g' -e "s/'/&apos;/g" -e 's/\"/&quot;/g'
}

mkdir -p "${HOME}/Library/LaunchAgents"
mkdir -p "${HOME}/Library/Logs"

here_esc="$(printf '%s' "${here}" | xml_escape)"
py_esc="$(printf '%s' "${python_bin}" | xml_escape)"
out_esc="$(printf '%s' "${HOME}/Library/Logs/desk_screen.out.log" | xml_escape)"
err_esc="$(printf '%s' "${HOME}/Library/Logs/desk_screen.err.log" | xml_escape)"

cat > "${dest}" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>${label}</string>

    <key>ProgramArguments</key>
    <array>
      <string>${py_esc}</string>
      <string>${here_esc}/server.py</string>
    </array>

    <key>WorkingDirectory</key>
    <string>${here_esc}</string>

    <key>EnvironmentVariables</key>
    <dict>
      <key>PYTHONUNBUFFERED</key>
      <string>1</string>
    </dict>

    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>${out_esc}</string>
    <key>StandardErrorPath</key>
    <string>${err_esc}</string>

    <key>ThrottleInterval</key>
    <integer>10</integer>
  </dict>
</plist>
EOF

uid="$(id -u)"

# Unload if already loaded (ignore errors)
launchctl bootout "gui/${uid}" "${dest}" >/dev/null 2>&1 || true

# Load + start
launchctl bootstrap "gui/${uid}" "${dest}"
launchctl enable "gui/${uid}/${label}" >/dev/null 2>&1 || true
launchctl kickstart -k "gui/${uid}/${label}" >/dev/null 2>&1 || true

echo "launchd enabled: ${label}"
echo "logs:"
echo "  ${HOME}/Library/Logs/desk_screen.out.log"
echo "  ${HOME}/Library/Logs/desk_screen.err.log"
if [[ -f "${here}/port.txt" ]]; then
  echo "port: $(tr -d ' \n\r\t' < "${here}/port.txt" || true)"
fi
