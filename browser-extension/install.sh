#!/bin/bash
set -e

# ── easyeda2kicad Chrome extension installer ──────────────────────────────────
# Run once. The server will start automatically at every login after this.

PLIST_LABEL="com.easyeda2kicad.server"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
SERVER_SRC="$(cd "$(dirname "$0")" && pwd)/server.py"
SERVER_DEST="$HOME/.easyeda2kicad/server.py"
LOG_FILE="/tmp/easyeda2kicad-server.log"

echo "==> Installing easyeda2kicad browser-extension server"

# ── 1. find python3 ───────────────────────────────────────────────────────────
PYTHON=$(which python3 2>/dev/null || true)
if [ -z "$PYTHON" ]; then
  echo "ERROR: python3 not found in PATH. Install Python 3 and try again."
  exit 1
fi
echo "    python3: $PYTHON"

# ── 2. find easyeda2kicad ─────────────────────────────────────────────────────
EASYEDA=$(which easyeda2kicad 2>/dev/null || true)
if [ -z "$EASYEDA" ]; then
  echo "ERROR: easyeda2kicad not found in PATH. Install it and try again."
  exit 1
fi
echo "    easyeda2kicad: $EASYEDA"

# ── 3. copy server to a permanent home ───────────────────────────────────────
mkdir -p "$HOME/.easyeda2kicad"
cp "$SERVER_SRC" "$SERVER_DEST"
chmod +x "$SERVER_DEST"
echo "    server copied to: $SERVER_DEST"

# ── 4. create the output library directory ────────────────────────────────────
mkdir -p "$HOME/Documents/KiCad"
echo "    KiCad output dir ready: $HOME/Documents/KiCad"

# ── 5. write launchd plist ────────────────────────────────────────────────────
mkdir -p "$HOME/Library/LaunchAgents"
# Derive the PATH that launchd should inherit so it can find easyeda2kicad
EASYEDA_DIR="$(dirname "$EASYEDA")"
LAUNCH_PATH="${EASYEDA_DIR}:/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin"

cat > "$PLIST_PATH" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>${PLIST_LABEL}</string>

  <key>ProgramArguments</key>
  <array>
    <string>${PYTHON}</string>
    <string>${SERVER_DEST}</string>
  </array>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>${LAUNCH_PATH}</string>
  </dict>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <true/>

  <key>StandardOutPath</key>
  <string>${LOG_FILE}</string>

  <key>StandardErrorPath</key>
  <string>${LOG_FILE}</string>
</dict>
</plist>
PLIST

echo "    launchd plist written: $PLIST_PATH"

# ── 6. load (or reload) the service ──────────────────────────────────────────
# Unload first in case a previous version is running
launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load -w "$PLIST_PATH"
echo "    launchd service loaded"

# ── 7. verify the server came up ─────────────────────────────────────────────
echo -n "    waiting for server..."
for i in $(seq 1 10); do
  sleep 0.5
  if curl -sf "http://localhost:7777/import?lcsc_id=test" > /dev/null 2>&1; then
    break
  fi
  echo -n "."
done
echo ""

if curl -sf "http://localhost:7777/import?lcsc_id=test" > /dev/null 2>&1; then
  echo "    server is running on http://localhost:7777"
else
  echo "    server may still be starting — check $LOG_FILE if you have issues"
fi

echo ""
echo "==> Done! Server will start automatically at every login."
echo ""
echo "Next step — load the Chrome extension:"
echo "  1. Open Chrome and go to chrome://extensions"
echo "  2. Enable 'Developer mode' (top-right toggle)"
echo "  3. Click 'Load unpacked' and select:"
echo "     $(cd "$(dirname "$0")" && pwd)/chrome"
echo ""
echo "Then browse lcsc.com and click 'Import to KiCad' on any component."
