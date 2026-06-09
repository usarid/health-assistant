#!/usr/bin/env bash
# Install a launchd agent that runs scripts/backup.sh nightly at 2 AM local time.
#
# launchd (not cron) because:
#   - macOS prefers it; cron requires Full Disk Access nag in System Settings.
#   - launchd re-runs missed jobs at next boot (good for laptops; harmless for desktops).
#   - logs go where the OS expects.
#
# Removes any existing entry first → idempotent.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKUP_SH="$REPO_ROOT/scripts/backup.sh"
LABEL="com.binahealth.backup"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
LOG_DIR="$HOME/Library/Logs/BinaHealth"

mkdir -p "$LOG_DIR" "$(dirname "$PLIST")"

# Unload first if it's already there
launchctl unload "$PLIST" 2>/dev/null || true

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>${BACKUP_SH}</string>
    </array>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>2</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>${LOG_DIR}/backup.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/backup.err</string>
    <!-- Run a missed job on next wake; safer than skipping if the Mac was asleep at 2 AM. -->
    <key>RunAtLoad</key>
    <false/>
</dict>
</plist>
EOF

launchctl load "$PLIST"

echo "Installed ${LABEL}"
echo "  plist:  $PLIST"
echo "  log:    $LOG_DIR/backup.log"
echo "  errors: $LOG_DIR/backup.err"
echo "  schedule: daily at 02:00 local time"
echo ""
echo "Verify:  launchctl list | grep com.binahealth"
echo "Run now: launchctl start ${LABEL}    # or just: scripts/backup.sh"
echo "Remove:  scripts/uninstall-backup-schedule.sh"
