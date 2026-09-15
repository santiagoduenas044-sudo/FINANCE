#!/bin/bash
# Remove the FINANCE launchd service (leaves your repo, .venv and .env intact).
#   bash deploy/macos/uninstall.sh
set -euo pipefail
LABEL="com.finance.signal"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
UID_NUM="$(id -u)"

launchctl bootout "gui/$UID_NUM/$LABEL" 2>/dev/null || true
rm -f "$PLIST"
echo "Removed LaunchAgent $LABEL."
echo "Your repo, .venv and .env are untouched. Logs remain in ~/Library/Logs/finance-signal."
