#!/bin/bash
# Builds beetsGUI.app (a real Mac app: own window, Dock icon, menu bar) and
# copies it to /Applications. It runs on your existing beets install, so
# your plugins and config.yaml are the ones it uses.
#
# Run once: ./scripts/build-app.sh
# Rebuild only if setup_app.py or the icon changed — `git pull` alone
# updates the app's code (see setup_app.py for why).
set -e

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$HOME/.local/pipx/venvs/beets"
PYTHON="$VENV/bin/python"
DEST="${BEETSGUI_APP_DEST:-/Applications}"

if [ "$(uname)" != "Darwin" ]; then
  echo "error: beetsGUI.app is macOS-only. Elsewhere, run: python app.py" >&2
  exit 1
fi
if [ ! -x "$PYTHON" ]; then
  echo "error: no beets pipx venv at $VENV. Install beets first: pipx install beets" >&2
  exit 1
fi

echo "Installing the app's dependencies into beets' venv (flask, pywebview, py2app)..."
pipx inject beets flask pywebview py2app >/dev/null

cd "$REPO_DIR"
rm -rf build dist
"$PYTHON" setup_app.py py2app -A >/dev/null

rm -rf "$DEST/beetsGUI.app"
cp -R dist/beetsGUI.app "$DEST/"
rm -rf build dist

echo "Built $DEST/beetsGUI.app — open it from Launchpad, Spotlight or the Dock."
if [ -f "$HOME/Library/LaunchAgents/com.beetsgui.server.plist" ]; then
  echo
  echo "Note: the LaunchAgent server is still installed. The app attaches to it"
  echo "when it's running, so this works, but you no longer need it:"
  echo "  launchctl unload ~/Library/LaunchAgents/com.beetsgui.server.plist"
  echo "  rm ~/Library/LaunchAgents/com.beetsgui.server.plist"
fi
