#!/usr/bin/env bash
# Midday atomic promo-week roll (ADR-0001 in the wednesday repo).
#
# Why this exists: the app's "current week" is max(week_start), and on
# Wednesdays the week must arrive WHOLE — on 2026-07-15 the Woolies-only
# morning task rolled the new week alone and live App Store users saw
# "Coles: 0 items" for hours. By 12:30 AEST the
# hotprices Coles dump is fresh (refreshes ~11am), so this task rolls the
# week atomically from the residential IP: fresh Coles + LIVE Woolies +
# Coles catalogue corrections + the new week's dinners, then the weekly list
# alert (so it arrives around lunchtime). The cloud runs the same roll at
# 12:45 (a Supabase pg_cron dispatch, migration 0041; GitHub's own schedule
# started 6.5-7.7h late in Nov 2026) with the backup Woolies list, so this
# task's real job is the LIVE Woolies list the cloud can't fetch. Registered
# as a launchd agent (12:30, plus a check every 15 minutes) by
# scripts/install-launchd.sh.
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
# A failed run before the last slot only logs, and a later check retries
# (2026-10-05/06: the 06:00 run failed asleep, 09:00 succeeded, yet each
# failure raised a notification). From the last slot on, notify once a day.
last_slot=2030   # HHMM of the last launchd slot (scripts/install-launchd.sh)
retry_left() { [ "$(date +%H%M)" -lt "$last_slot" ]; }
notified="$log_dir/.notified-midday-roll"
notify_once() {
    [ "$(cat "$notified" 2>/dev/null)" = "$today" ] && return
    echo "$today" > "$notified"
    notify "$1"
}
[ "${1:-}" = "--force" ] && FORCE=1
# The Mac may run this during a brief "dark wake" from sleep, or before Wi-Fi
# reconnects (2026-09-26: the 06:03 run timed out, then couldn't resolve the
# DB host). Wait up to 3 minutes for DNS; with still no network, defer to the
# next check quietly — that isn't a failure worth a notification
# (verify-live emails if no live run lands for 36h).
# Hard cap per Python step: a Mac that sleeps mid-run can leave a socket
# hanging with no timeout (26-27 Sep: the Coles step hung for hours). perl's
# alarm is a portable timeout (macOS has no GNU timeout); it execs Python in
# place, so the alarm kills Python itself, not caffeinate above it.
STEP_TIMEOUT=900

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

# Nothing to do before the first slot: the dump refreshes ~11am.
too_early() { [ "${FORCE:-}" != "1" ] && [ "$(date +%H%M)" -lt 1230 ]; }

# A failure while the Mac was awake is a real one (a source down, say), so it
# retries at most hourly rather than at every 15-minute check.
cooldown="$log_dir/.failed-midday-roll"
cooling_down() {
    [ "${FORCE:-}" != "1" ] || return 1
    local t
    t=$(cat "$cooldown" 2>/dev/null) && [ -n "$t" ] && [ $(( $(date +%s) - t )) -lt 3600 ]
}

# A sleeping Mac on battery can't be kept awake: caffeinate's PreventSystemSleep
# only holds on AC power, and a sleeping Mac gets ~2-second "dark wakes" every
# ~15 minutes, which is all a run then gets; each step's database or Woolworths
# connection dies in between (2026-11-11: of 9 runs since 5 Nov only the one on
# the charger finished, and one hung asleep for 38 hours, blocking a day of
# slots). So the roll starts, and each step begins, only when the Mac can finish
# it: on AC power, or on battery while it's properly awake (screen on, lid open).
on_ac()      { /usr/bin/pmset -g batt | head -1 | grep -q "AC Power"; }
full_wake()  { /usr/bin/pmset -g systemstate | grep -q "Graphics"; }
lid_closed() { /usr/sbin/ioreg -r -k AppleClamshellState -d 4 | grep -q '"AppleClamshellState" = Yes'; }
mac_awake()  { on_ac || { full_wake && ! lid_closed; }; }
deferred_stamp="$log_dir/.deferred-midday-roll"

cd "$repo" || exit 1
# The 15-minute check makes these routine, so they exit without a log file.
if already_ok || too_early || cooling_down; then
    exit 0
fi
if ! mac_awake; then
    if [ "$(cat "$deferred_stamp" 2>/dev/null)" != "$today" ]; then
        echo "$today" > "$deferred_stamp"
        echo "[$(date -Iseconds)] Mac asleep on battery — deferring until it's awake (checked every 15 minutes)" >> "$log"
    fi
    exit 0
fi
if ! wait_for_network; then
    echo "[$(date -Iseconds)] no network after 3 min (asleep / offline) — deferring to the next check" >> "$log"
    exit 0
fi
echo "[$(date -Iseconds)] starting midday atomic roll" >> "$log"

final=0
deferred=0
started=$(date +%s)
RUN_DEADLINE=3600   # wall clock; perl's alarm doesn't count time asleep
# Once the Mac is asleep on battery (or the run has overrun an hour), the
# remaining steps are skipped and a later check reruns the roll (every step is
# safe to re-run). Exit 75 (temporary failure) marks the deferral.
can_continue() {
    [ "$deferred" -eq 0 ] || return 1
    if ! mac_awake || [ $(( $(date +%s) - started )) -gt "$RUN_DEADLINE" ]; then
        deferred=1
        [ "$final" -eq 0 ] && final=75
        echo "[$(date -Iseconds)] Mac asleep on battery or run overran — skipping the remaining steps" >> "$log"
        return 1
    fi
}
run_step() {
    local name="$1"; shift
    can_continue || { echo "[$(date -Iseconds)] step=$name skipped" >> "$log"; return; }
    echo "[$(date -Iseconds)] step=$name starting" >> "$log"
    /usr/bin/caffeinate -i -s /usr/bin/perl -e 'alarm shift; exec @ARGV' "$STEP_TIMEOUT" "$py" -X utf8 "$@" >> "$log" 2>&1
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

# The weekly list alert, right after a complete roll, so it lands around
# lunchtime instead of whenever the late cloud cron runs. send_alerts' week
# gate makes it a no-op until the new Wednesday week exists, and the
# device_alerts_log unique (one digest per device per week) means the cloud
# run, the backstop, never sends twice. Only after a clean roll: never alert
# on a half-written week.
if [ "$final" -eq 0 ]; then
    run_step alerts -m src.send_alerts --verbose
fi

# After a successful Wednesday roll, mark the website's cached product and
# category pages stale so they show the new week on their next visit.
revalidate_site() {
    local secret
    secret=$(grep -E '^SEO_REVALIDATE_SECRET=' "$repo/.env" 2>/dev/null | cut -d= -f2-)
    [ -n "$secret" ] || return 0
    if curl -fsS -m 20 -X POST -H "x-revalidate-secret: $secret" \
        https://wednesday.ernestmikhail.com/api/revalidate >> "$log" 2>&1; then
        echo "" >> "$log"; echo "[$(date -Iseconds)] website pages marked for refresh" >> "$log"
    else
        echo "[$(date -Iseconds)] website revalidate failed (pages refresh within a week anyway)" >> "$log"
    fi
}
if [ "$final" -eq 0 ] && [ "$(TZ=Australia/Sydney date +%u)" = "3" ]; then
    revalidate_site
fi

# Chemist Warehouse (ADR-0004, wednesday repo): its catalogue, then up to 200
# usual-price checks on its product pages (residential IP, ~10 min). Soft
# steps, after the alert: they never change the exit code, so a CW hiccup
# can't hold back the Coles/Woolworths roll, the alert or a retry slot.
soft_step() {
    local name="$1"; shift
    can_continue || { echo "[$(date -Iseconds)] step=$name skipped (soft)" >> "$log"; return; }
    echo "[$(date -Iseconds)] step=$name starting (soft)" >> "$log"
    /usr/bin/caffeinate -i -s /usr/bin/perl -e 'alarm shift; exec @ARGV' "$STEP_TIMEOUT" "$py" -X utf8 "$@" >> "$log" 2>&1
    local code=$?
    echo "[$(date -Iseconds)] step=$name exit=$code (soft, ignored)" >> "$log"
}
soft_step cw       -m src.refresh_cw --write-db --verbose
soft_step cw_usual -m src.cw_usual_prices --write-db --limit 200 --verbose

echo "[$(date -Iseconds)] finished exit=$final" >> "$log"
if [ "$final" -eq 0 ]; then
    echo "$today" > "$stamp"
    rm -f "$cooldown"
fi
if [ "$deferred" -eq 1 ]; then
    echo "[$(date -Iseconds)] deferred; a check reruns it once the Mac is awake (no notification)" >> "$log"
elif [ "$final" -ne 0 ]; then
    date +%s > "$cooldown"
    if retry_left; then
        echo "[$(date -Iseconds)] failed while awake; retrying within the hour (no notification)" >> "$log"
    else
        notify_once "Midday roll failed while the Mac was awake (exit $final). The cloud job covers it. Log: $log"
    fi
elif grep -q "live_api_unavailable" "$log"; then
    notify_once "Midday roll: live Woolworths API unreachable, served the backup list."
fi
exit $final
