#!/usr/bin/env bash
# Register (or remove) the two residential-IP jobs as per-user launchd agents:
#
#   com.ernestmikhail.wednesday.woolies-refresh   daily 06:00, 09:00                 refresh-woolies.sh
#   com.ernestmikhail.wednesday.midday-roll       daily 12:30, 13:45, 17:00, 20:30   refresh-midday.sh
#                                                 + a check every 15 minutes
#
# The 15-minute check is the catch-up for a Mac that slept through its slot:
# a sleeping Mac on battery can't be kept awake, so refresh-midday.sh defers
# then, and the check runs the roll within minutes of the Mac waking. The
# script exits at once (no log) when today's roll is done or it's before 12:30.
#
# The 13:45 midday retry covers daylight saving: at 12:30 AEDT the hotprices
# Coles dump (refreshed ~01:00 UTC) may be only minutes old, so the stale-dump
# gate can block the roll; the retry rolls it. Re-runs are safe (ADR-0001).
# The later slots are recovery for a Mac that was asleep or offline: each
# script exits at once if it already succeeded that day.
#
# Times are the Mac's local time (Australia/Sydney). launchd runs a missed
# calendar job once the Mac wakes from sleep (the old Windows tasks'
# StartWhenAvailable); a job missed while powered off is skipped, and the
# cloud cron is the backstop for that (ADR-0001).
#
# The plists are generated here rather than committed because launchd needs
# absolute paths and does not expand ~. Re-running is safe (bootout first).
#
#   scripts/install-launchd.sh              install / reinstall both agents
#   scripts/install-launchd.sh --uninstall  remove both agents

set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
agents="$HOME/Library/LaunchAgents"
log_dir="$HOME/Library/Logs/Wednesday"
domain="gui/$(id -u)"

# label  script  HH:MM[,HH:MM...]  check-every-seconds (0 = slots only)
jobs=(
    "com.ernestmikhail.wednesday.woolies-refresh refresh-woolies.sh 06:00,09:00 0"
    "com.ernestmikhail.wednesday.midday-roll refresh-midday.sh 12:30,13:45,17:00,20:30 900"
)

calendar_entries() {
    local times="$1" t
    for t in ${times//,/ }; do
        printf '        <dict>\n            <key>Hour</key>\n            <integer>%d</integer>\n' "$((10#${t%%:*}))"
        printf '            <key>Minute</key>\n            <integer>%d</integer>\n        </dict>\n' "$((10#${t##*:}))"
    done
}

start_interval() {
    [ "$1" -gt 0 ] || return 0
    printf '    <key>StartInterval</key>\n    <integer>%d</integer>\n' "$1"
}

write_plist() {
    local label="$1" script="$2" times="$3" every="$4"
    cat > "$agents/$label.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$label</string>
    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$repo/scripts/$script</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$repo</string>
    <key>StartCalendarInterval</key>
    <array>
$(calendar_entries "$times")
    </array>
$(start_interval "$every")
    <key>StandardOutPath</key>
    <string>$log_dir/launchd-$label.log</string>
    <key>StandardErrorPath</key>
    <string>$log_dir/launchd-$label.log</string>
</dict>
</plist>
EOF
}

if [ "${1:-}" = "--uninstall" ]; then
    for job in "${jobs[@]}"; do
        read -r label _ <<< "$job"
        launchctl bootout "$domain/$label" 2>/dev/null || true
        rm -f "$agents/$label.plist"
        echo "removed $label"
    done
    exit 0
fi

if [ ! -x "$repo/.venv/bin/python" ]; then
    echo "missing $repo/.venv — create it first (see README 'Running locally')" >&2
    exit 1
fi

mkdir -p "$agents" "$log_dir"
for job in "${jobs[@]}"; do
    read -r label script times every <<< "$job"
    write_plist "$label" "$script" "$times" "$every"
    plutil -lint -s "$agents/$label.plist"
    launchctl bootout "$domain/$label" 2>/dev/null || true
    launchctl bootstrap "$domain" "$agents/$label.plist"
    extra=""
    [ "$every" -gt 0 ] && extra=", plus a check every $((every / 60)) min"
    printf 'installed %s (daily %s%s)\n' "$label" "${times//,/ + }" "$extra"
done
echo "logs: $log_dir"
