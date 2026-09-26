#!/usr/bin/env bash
# Residential pull of Woolworths LIVE half-price data into Supabase.
#
# Why this exists: the live Woolworths browse API is blocked from datacenter
# IPs, so the cloud cron (GitHub Actions) can only fall back to the less-
# complete hotprices dump (~82% precision / ~57% recall). Run from a home /
# residential IP, the live API works and gives the full ~100% set. Registered
# as a launchd agent (daily 06:00) by scripts/install-launchd.sh.
#
# It writes through the same refresh_woolies_specials path the cron uses, so
# the result is identical to a manual `python -m src.refresh_woolies_specials`.

set -u

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
py="$repo/.venv/bin/python"

# Log outside the repo so it never gets committed.
log_dir="$HOME/Library/Logs/Wednesday"
mkdir -p "$log_dir"
log="$log_dir/woolies-refresh-$(date +%Y%m%d-%H%M%S).log"

# Desktop alert when a run fails or the live Woolworths API was unreachable
# (the run then served the weaker hotprices dump). verify_data also fails the
# cloud cron for the same condition; this is the immediate, local signal.
notify() {
    /usr/bin/osascript -e "display notification \"$1\" with title \"Wednesday ingestion\"" >/dev/null 2>&1 || true
}

stamp="$log_dir/.ok-woolies-refresh"
[ "${1:-}" = "--force" ] && FORCE=1
# The Mac may run this during a brief "dark wake" from sleep, or before Wi-Fi
# reconnects (2026-09-26: the 06:03 run timed out, then couldn't resolve the
# DB host). Wait up to 3 minutes for DNS; with still no network, defer to the
# next scheduled slot quietly — that isn't a failure worth a notification
# (verify-live emails if no live run lands for 36h).
wait_for_network() {
    local i
    for i in $(seq 1 18); do
        if "$py" -c 'import socket
for h in ("www.woolworths.com.au", "hotprices.org"):
    socket.getaddrinfo(h, 443)' 2>/dev/null; then
            return 0
        fi
        sleep 10
    done
    return 1
}

# One success per Sydney day is enough; later retry slots only act if the
# earlier runs failed (or pass --force).
today=$(TZ=Australia/Sydney date +%F)
already_ok() { [ "${FORCE:-}" != "1" ] && [ -f "$stamp" ] && [ "$(cat "$stamp")" = "$today" ]; }

cd "$repo" || exit 1
if already_ok; then
    echo "[$(date -Iseconds)] already succeeded today ($today) — skipping this slot" >> "$log"
    exit 0
fi
if ! wait_for_network; then
    echo "[$(date -Iseconds)] no network after 3 min (asleep / offline) — deferring to the next slot" >> "$log"
    exit 0
fi
echo "[$(date -Iseconds)] starting woolies live refresh" >> "$log"
/usr/bin/caffeinate -i -s "$py" -X utf8 -m src.refresh_woolies_specials --verbose >> "$log" 2>&1
code=$?
[ "$code" -eq 0 ] && echo "$today" > "$stamp"
echo "[$(date -Iseconds)] finished exit=$code" >> "$log"
if [ "$code" -ne 0 ]; then
    notify "Woolies live refresh failed (exit $code). Log: $log"
elif grep -q "live_api_unavailable" "$log"; then
    notify "Woolies live refresh: live Woolworths API unreachable, served the dump fallback."
fi
exit $code
