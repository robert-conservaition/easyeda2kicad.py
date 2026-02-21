#!/bin/bash
PLIST_LABEL="com.easyeda2kicad.server"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"

echo "==> Uninstalling easyeda2kicad server"

launchctl unload "$PLIST_PATH" 2>/dev/null && echo "    service stopped" || true
rm -f "$PLIST_PATH" && echo "    plist removed"
rm -rf "$HOME/.easyeda2kicad" && echo "    server files removed"

echo "Done. You can also remove the Chrome extension from chrome://extensions"
