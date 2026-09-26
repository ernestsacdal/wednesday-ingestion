#!/usr/bin/env bash
# Midday atomic promo-week roll (ADR-0001 in the wednesday repo).
#
# Why this exists: the app's "current week" is max(week_start), and on
# Wednesdays the week must arrive WHOLE — on 2026-07-15 the Woolies-only
# morning task rolled the new week alone and live App Store users saw
# "Coles: 0 items" for hours (the cloud cron, scheduled noon AEST, has been
# firing 2.5-4h late on GitHub's congested scheduler). By 12:30 AEST the
# hotprices Coles dump is fresh (refreshes ~11am), so this task rolls the
# week atomically from the residential IP: fresh Coles + LIVE Woolies +
# Coles catalogue corrections + the new week's dinners. The cloud cron
# remains the backstop for days this machine is off. Registered as a launchd
# agent (daily 12:30) by scripts/install-launchd.sh.
#
# Every step has its own guards (solo-roll guard, stale-dump gate, sticky-live
# guard, dinner revalidation), so re-runs and out-of-order timing are safe.
# A failed step does not stop the later ones; the exit code is the first
# non-zero step's.

set -u

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
py="$repo/.venv/bin/python"

log_dir="$HOME/Library/Logs/Wednesday"
mkdir -p "$log_dir"
log="$log_dir/midday-roll-$(date +%Y%m%d-%H%M%S).log"

# Desktop alert when a run fails or the live Woolworths API was unreachable
# (the run then served the weaker hotprices dump). verify_data also fails the
# cloud cron for the same condition; this is the immediate, local signal.
notify() {
    /usr/bin/osascript -e "display notification \"$1\" with title \"Wednesday ingestion\"" >/dev/null 2>&1 || true
}

stamp="$log_dir/.ok-midday-roll"
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
echo "[$(date -Iseconds)] starting midday atomic roll" >> "$log"

final=0
run_step() {
    local name="$1"; shift
    echo "[$(date -Iseconds)] step=$name starting" >> "$log"
    /usr/bin/caffeinate -i -s "$py" -X utf8 "$@" >> "$log" 2>&1
    local code=$?
    echo "[$(date -Iseconds)] step=$name exit=$code" >> "$log"
    if [ "$code" -ne 0 ] && [ "$final" -eq 0 ]; then final=$code; fi
}

run_step coles   -m src.refresh_coles_hotprices --verbose
run_step woolies -m src.refresh_woolies_specials --verbose
run_step audit   -m src.audit_accuracy --write-db --correct --verbose
run_step dinners -m src.generate_recipes --seed --write-db --revalidate --verbose
run_step predict -m src.prediction.hazard --write-db --verbose
run_step ledger  -m src.eval.predictions_eval --snapshot --write-db --verbose

echo "[$(date -Iseconds)] finished exit=$final" >> "$log"
[ "$final" -eq 0 ] && echo "$today" > "$stamp"
if [ "$final" -ne 0 ]; then
    notify "Midday roll failed (exit $final). Log: $log"
elif grep -q "live_api_unavailable" "$log"; then
    notify "Midday roll: live Woolworths API unreachable, served the dump fallback."
fi
exit $final
