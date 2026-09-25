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

cd "$repo" || exit 1
echo "[$(date -Iseconds)] starting woolies live refresh" >> "$log"
"$py" -X utf8 -m src.refresh_woolies_specials --verbose >> "$log" 2>&1
code=$?
echo "[$(date -Iseconds)] finished exit=$code" >> "$log"
if [ "$code" -ne 0 ]; then
    notify "Woolies live refresh failed (exit $code). Log: $log"
elif grep -q "live_api_unavailable" "$log"; then
    notify "Woolies live refresh: live Woolworths API unreachable, served the dump fallback."
fi
exit $code
