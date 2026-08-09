#!/bin/bash
# Installs a LaunchAgent that starts the beetsGUI server at login and keeps
# it running (restarts it if it crashes) — so it's already up by the time
# you click the Safari Web App icon, no separate "Launcher.app" click needed.
#
# Run once: ./scripts/install-launchagent.sh
# Undo:     launchctl unload ~/Library/LaunchAgents/com.beetsgui.server.plist
#           rm ~/Library/LaunchAgents/com.beetsgui.server.plist
set -e

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.beetsgui.server"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG_DIR="$HOME/Library/Logs"

# Prefer the pipx beets venv (has Flask injected per the README), fall back
# to plain python3 on PATH.
if [ -x "$HOME/.local/pipx/venvs/beets/bin/python" ]; then
  PYTHON="$HOME/.local/pipx/venvs/beets/bin/python"
else
  PYTHON="$(command -v python3)"
fi
"$PYTHON" -c "import flask" 2>/dev/null || {
  echo "error: $PYTHON has no Flask. Run: pipx inject beets flask" >&2
  exit 1
}

mkdir -p "$LOG_DIR"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$REPO_DIR/server.py</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$REPO_DIR</string>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <dict>
    <key>SuccessfulExit</key>
    <false/>
  </dict>
  <key>StandardOutPath</key>
  <string>$LOG_DIR/beetsgui-server.log</string>
  <key>StandardErrorPath</key>
  <string>$LOG_DIR/beetsgui-server.log</string>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load -w "$PLIST"

echo "Installed. Server will start at login and stay running — logs at $LOG_DIR/beetsgui-server.log"
echo "Starting it now for this session too..."
sleep 1
curl -s "http://localhost:${BEETSGUI_PORT:-1612}/" >/dev/null && echo "Server is up." || echo "Not responding yet — check the log."
