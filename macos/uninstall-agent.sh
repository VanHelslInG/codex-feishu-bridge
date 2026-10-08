#!/usr/bin/env bash
#
# Remove the bridge's LaunchAgent. Does not stop a bridge started by hand.
#
# NOT YET VERIFIED ON A REAL MAC.
set -euo pipefail

LABEL="com.chen.codex-feishu-bridge"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$UID/$LABEL" 2>/dev/null || true

if [ -f "$PLIST" ]; then
    rm -f "$PLIST"
    echo "Removed $PLIST."
else
    echo "$PLIST is not installed."
fi

echo "A bridge started by hand is untouched; stop it with macos/stop.sh."
