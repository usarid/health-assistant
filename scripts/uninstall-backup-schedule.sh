#!/usr/bin/env bash
# Remove the launchd agent installed by install-backup-schedule.sh.
# Backup script + env file are NOT removed — only the schedule.

set -euo pipefail

LABEL="com.binahealth.backup"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"

if [ ! -f "$PLIST" ]; then
    echo "Nothing to uninstall — $PLIST not present."
    exit 0
fi

launchctl unload "$PLIST" 2>/dev/null || true
rm "$PLIST"

echo "Uninstalled ${LABEL}. Backup script and env file are still in place; you can run them manually."
