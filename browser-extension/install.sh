#!/bin/bash
set -e

# ── easyeda2kicad Chrome extension installer ──────────────────────────────────
# Run once on each computer, and again after every `git pull`.
# The server starts automatically at every login after this.

PLIST_LABEL="com.easyeda2kicad.server"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
EXT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(dirname "$EXT_DIR")"
SERVER_SRC="$EXT_DIR/server.py"
SERVER_DEST="$HOME/.easyeda2kicad/server.py"
VENV_DIR="$HOME/.easyeda2kicad/venv"
LOG_FILE="/tmp/easyeda2kicad-server.log"

if [ "$(uname)" != "Darwin" ]; then
  echo "ERROR: this installer is for macOS only."
  exit 1
fi

echo "==> Installing easyeda2kicad browser-extension server"

# ── 1. find python3 ───────────────────────────────────────────────────────────
# Prefer Apple's python: its path never changes, so the Full Disk Access grant
# survives. A Homebrew python moves on upgrade, which breaks launchd (exit 78).
# Run each candidate, because /usr/bin/python3 is only a stub until the Command
# Line Tools are installed.
PYTHON=""
for cand in /usr/bin/python3 "$(which python3 2>/dev/null)" /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if [ -n "$cand" ] && [ -x "$cand" ] && "$cand" --version >/dev/null 2>&1; then PYTHON="$cand"; break; fi
done
if [ -z "$PYTHON" ]; then
  echo "ERROR: python3 not found. Run this, then try again:  xcode-select --install"
  exit 1
fi
echo "    python3: $PYTHON"

# openpyxl lets the BOM export write .xlsx (falls back to .csv without it).
"$PYTHON" -c "import openpyxl" 2>/dev/null || {
  echo "    installing openpyxl (for xlsx BOM export)…"
  "$PYTHON" -m pip install --quiet --user openpyxl 2>/dev/null || echo "    (openpyxl install skipped — BOM export will use .csv)"
}

# ── 2. install easyeda2kicad from this repo ──────────────────────────────────
# Always this repo, not PyPI: the PyPI package lacks our JLCPCB fields and API
# fixes. A real copy (not editable) also keeps the server independent of
# ~/Documents, which macOS can block at login.
echo "    installing easyeda2kicad from $REPO_DIR…"
rm -rf "$VENV_DIR"
"$PYTHON" -m venv "$VENV_DIR"
"$VENV_DIR/bin/python" -m pip install --quiet --upgrade pip
"$VENV_DIR/bin/python" -m pip install --quiet "$REPO_DIR"
rm -rf "$REPO_DIR/build"
EASYEDA="$VENV_DIR/bin/easyeda2kicad"
"$EASYEDA" --help > /dev/null
echo "    easyeda2kicad: $EASYEDA"

# ── 3. copy server to a permanent home ───────────────────────────────────────
mkdir -p "$HOME/.easyeda2kicad"
cp "$SERVER_SRC" "$SERVER_DEST"
chmod +x "$SERVER_DEST"
echo "    server copied to: $SERVER_DEST"

# ── 4. create the output library directory ────────────────────────────────────
# A first import creates the library files KiCad needs to be pointed at. It runs
# here, in Terminal, because the background server may not have access yet.
LIB_DIR="$HOME/Documents/Kicad/easyeda2kicad"
if [ ! -f "$LIB_DIR/easyeda2kicad.kicad_sym" ]; then
  echo "    creating library with a first part (C2040)…"
  "$EASYEDA" --full --lcsc_id=C2040 > /dev/null 2>&1 \
    || echo "    (first import failed — check your internet connection)"
fi
echo "    KiCad library: $LIB_DIR"

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
  if curl -sf "http://127.0.0.1:7777/status" > /dev/null 2>&1; then
    break
  fi
  echo -n "."
done
echo ""

STATUS_JSON=""
if curl -sf "http://127.0.0.1:7777/status" > /dev/null 2>&1; then
  echo "    server is running on http://127.0.0.1:7777"
  STATUS_JSON=$(curl -s "http://127.0.0.1:7777/status" 2>/dev/null)
else
  echo "    server may still be starting — check $LOG_FILE if you have issues"
fi

# ── 8. Full Disk Access check (macOS TCC) ────────────────────────────────────
# A launchd background agent cannot read ~/Documents until it is granted Full
# Disk Access. Detect that case and tell the user exactly what to do.
if echo "$STATUS_JSON" | grep -q '"permission_denied"'; then
  echo ""
  echo "  ⚠️  ONE MANUAL STEP — grant Full Disk Access:"
  echo "       1. In the window that opens, click +"
  echo "       2. Press Cmd-Shift-G, enter:  $PYTHON  and click Open"
  echo "       3. Turn on the toggle next to it"
  open "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles" 2>/dev/null || true
  if [ -t 0 ]; then
    read -r -p "     Press Enter once that's done… " _
    launchctl kickstart -k "gui/$(id -u)/${PLIST_LABEL}"
    sleep 2
    if curl -s "http://127.0.0.1:7777/status" | grep -q '"permission_denied"'; then
      echo "     Still blocked — re-check the toggle, then run this installer again."
    else
      echo "     Access confirmed."
    fi
  fi
fi

echo ""
echo "==> Done! Server starts automatically at every login."
echo ""
echo "Dashboard (monitor parts, generate JLCPCB BOMs, update the library):"
echo "     http://127.0.0.1:7777/"
echo ""
echo "Next step — load the Chrome extension:"
echo "  1. Open Chrome and go to chrome://extensions"
echo "  2. Enable 'Developer mode' (top-right toggle)"
echo "  3. Click 'Load unpacked' and select:"
echo "     $EXT_DIR/chrome"
echo ""
echo "Then finish the KiCad steps in README.md and click 'Import to KiCad' on lcsc.com."
