#!/bin/bash
# Install (or remove) the lecture-tool panel as a macOS login service, so http://localhost:8765
# is always there — no terminal, no Claude app. Logs: ~/Library/Logs/lecture-tool.log
#
#   bash scripts/install_service.sh            install / update and start
#   bash scripts/install_service.sh --remove   stop and remove
#   bash scripts/install_service.sh --status   is it running?
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.sprtk.lecture-tool"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/lecture-tool.log"
PORT="${PORT:-8765}"
# Python: an explicit PYTHON_BIN, else the project's .venv (made by install.sh), else whatever python3 is on PATH
if [ -n "${PYTHON_BIN:-}" ]; then PY="$PYTHON_BIN"
elif [ -x "$ROOT/.venv/bin/python" ]; then PY="$ROOT/.venv/bin/python"
else PY="$(command -v python3)"; fi
CLAUDE_DIR="$(dirname "$(command -v claude || echo "$HOME/.local/bin/claude")")"
FFMPEG_DIR="$(dirname "$(command -v ffmpeg || echo /opt/homebrew/bin/ffmpeg)")"
UID_NUM="$(id -u)"
xml() { printf '%s' "$1" | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g'; }   # paths may contain & or <

case "${1:-}" in
  --remove)
    launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo "removed $LABEL"
    exit 0 ;;
  --status)
    if ! launchctl print "gui/$UID_NUM/$LABEL" >/dev/null 2>&1; then
      echo "not installed"
    elif curl -sf "http://localhost:$PORT/api/jobs" >/dev/null; then
      echo "running — http://localhost:$PORT   (log: $LOG)"
    else
      echo "installed but not answering on port $PORT — see $LOG (another program may hold the port)"
    fi
    exit 0 ;;
esac

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs" 2>/dev/null || true
if [ ! -w "$HOME/Library/LaunchAgents" ] || { [ -e "$PLIST" ] && [ ! -w "$PLIST" ]; }; then
  echo "cannot write to $HOME/Library/LaunchAgents (owned by: $(stat -f '%Su' "$HOME/Library/LaunchAgents" 2>/dev/null || echo '?'))."
  echo "Fix it once with:   sudo chown -R \"$(id -un)\" \"$HOME/Library/LaunchAgents\"   (asks for your Mac password)"
  exit 1
fi
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$(xml "$PY")</string>
    <string>$(xml "$ROOT")/scripts/serve.py</string>
    <string>$PORT</string>
    <string>--no-browser</string>
    <string>--service</string>
  </array>
  <key>WorkingDirectory</key><string>$(xml "$ROOT")</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>$(xml "$(dirname "$PY"):$CLAUDE_DIR:$FFMPEG_DIR"):/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>HOME</key><string>$(xml "$HOME")</string>
    <key>LANG</key><string>en_US.UTF-8</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>5</integer>
  <key>StandardOutPath</key><string>$(xml "$LOG")</string>
  <key>StandardErrorPath</key><string>$(xml "$LOG")</string>
</dict>
</plist>
EOF

# (re)load: bootout ignores "not loaded"; launchd needs a moment before the same label can be
# bootstrapped again, so wait and retry a few times
launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
for i in 1 2 3 4 5 6; do
  sleep 1.5
  if err="$(launchctl bootstrap "gui/$UID_NUM" "$PLIST" 2>&1)"; then break; fi
  if [ "$i" = 6 ]; then echo "could not load the service (launchctl bootstrap failed): $err"; exit 1; fi
done
sleep 1.5
if curl -sf "http://localhost:$PORT/api/jobs" >/dev/null; then
  echo "lecture-tool is running as a login service → http://localhost:$PORT"
  echo "log: $LOG   ·   remove with: bash scripts/install_service.sh --remove"
else
  echo "service installed but the panel did not answer yet — check $LOG"
  exit 1
fi
