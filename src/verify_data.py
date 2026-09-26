"""Post-run data invariants — the loud safety net.

Three incidents in one week (silent Woolies loss, a stale-code cron
re-creating purged synthetic rows, the fallback prune wiping Coles' current
week) all ran GREEN in GitHub Actions because nothing inspected the data
OUTCOME — only whether the scripts crashed. This module asserts invariants
against the live DB and exits non-zero when any fail, which fails the
workflow step, which triggers GitHub's built-in failure email. Bad data
becomes an inbox ping instead of a surprise in the app.

Runs as the final step of both workflows (daily-ingestion, weekly-catalogue)
and standalone:

    python -m src.verify_data --verbose

Thresholds are deliberately loose floors (roughly a third of typical) so
normal weekly variance never cries wolf; only real breakage trips them.
A check with severity "warn" logs loudly but never fails the run. Set
WOOLIES_LIVE_REQUIRED=0 (a repo variable in CI) to downgrade the live-Woolies
checks to warnings while the residential Mac is deliberately offline.
Requires SUPABASE_DB_URL.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass
from typing import Any, Callable

import psycopg

from src.env import load_dotenv
from src.scrapers.base import configure_logging
from src.weeks import live_deadline_passed


@dataclass
class Check:
    name: str
    sql: str
    ok: Callable[[Any], bool]
    expect: str                    # human description of the passing condition
    severity: str = "fail"         # "fail" reddens the run; "warn" only logs
    active: Callable[[], bool] | None = None   # None = always evaluated


# Checks that depend on the residential Mac pulling the live Woolworths API.
# WOOLIES_LIVE_REQUIRED=0 downgrades them to warnings (e.g. travelling).
LIVE_WOOLIES_CHECKS = {"woolies_live_heartbeat", "woolies_current_week_live"}


CHECKS: list[Check] = [
    # The class of incidents 1 + 3: a retailer's current week silently empty.
    Check(
        "coles_current_half",
        """select count(*) from specials s join products p on p.id = s.product_id
           where s.week_start = (select max(week_start) from specials)
             and s.is_half_price and p.retailer = 'coles'""",
        lambda v: v >= 400, ">= 400 (typical ~1,250)",
    ),
    Check(
        "woolies_current_half",
        """select count(*) from specials s join products p on p.id = s.product_id
           where s.week_start = (select max(week_start) from specials)
             and s.is_half_price and p.retailer = 'woolworths'""",
        lambda v: v >= 400, ">= 400 (typical ~1,100)",
    ),
    # Woolies half-price must not silently collapse RELATIVE to Coles. The
    # absolute floor above catches a total loss; this catches a partial
    # regression (a precision/recall change that halves the set) on the
    # dump-fallback path, which — unlike Coles (SaleFinder) — has no
    # ground-truth probe. Tolerant: normal weeks sit near parity (~0.95).
    Check(
        "woolies_coles_half_ratio",
        """select case when c.n = 0 then 1
                       else round(w.n::numeric / c.n, 2) end
           from (select count(*) n from specials s join products p on p.id = s.product_id
                  where s.week_start = (select max(week_start) from specials)
                    and s.is_half_price and p.retailer = 'woolworths') w,
                (select count(*) n from specials s join products p on p.id = s.product_id
                  where s.week_start = (select max(week_start) from specials)
                    and s.is_half_price and p.retailer = 'coles') c""",
        lambda v: v is None or float(v) >= 0.4, ">= 0.4 (Woolies/Coles half-price; ~0.95 typical)",
    ),
    # Stale week being served as current.
    Check(
        "week_is_current",
        """select (select max(week_start) from specials)
                = (d - ((extract(dow from d)::int - 3 + 7) % 7))::date
           from (select (now() at time zone 'Australia/Sydney')::date as d) t""",
        lambda v: v is True, "max(week_start) == most recent Wednesday",
    ),
    # The class of incident 2: resurrected synthetic-keyed rows.
    Check(
        "no_synthetic_products",
        "select count(*) from products where retailer_sku like 'stockup:%'",
        lambda v: v == 0, "== 0",
    ),
    # Marketplace junk stays purged (parse-time skip + this backstop).
    Check(
        "no_marketplace_products",
        """select count(*) from products
           where retailer = 'woolworths'
             and length(split_part(retailer_sku, ':', 2)) >= 8""",
        lambda v: v == 0, "== 0",
    ),
    # Push tokens live in device_watchlists/device_alerts_log behind RLS with
    # ZERO anon grants (the Expo push API is unauthenticated — a leaked token is
    # a spam vector). Any future migration that re-grants anon/authenticated on
    # these tables trips this and fails the run.
    Check(
        "push_tables_anon_locked",
        """select count(*) from information_schema.role_table_grants
           where table_schema = 'public'
             and table_name in ('device_watchlists', 'device_alerts_log')
             and grantee in ('anon', 'authenticated')""",
        lambda v: v == 0, "== 0 (push-token tables anon-locked)",
    ),
    # Catalogue wipes.
    Check(
        "coles_catalogue_floor",
        "select count(*) from products where retailer = 'coles'",
        lambda v: v >= 15_000, ">= 15,000 (typical ~21,300)",
    ),
    # The daily catalogue refresh actually ran broad. With the upsert now
    # overwriting price + name (the Sunsilk stale-price fix, 2026-07-28), a
    # fresh last_seen across the catalogue implies fresh prices; if this
    # collapses, the catalogue ingest silently stopped and prices are aging.
    Check(
        "catalogue_recently_refreshed",
        """select count(*) from products
           where last_seen >= now() - interval '2 days'""",
        lambda v: v >= 40_000, ">= 40,000 seen in 2 days (typical ~45,000)",
    ),
    Check(
        "woolies_catalogue_floor",
        "select count(*) from products where retailer = 'woolworths'",
        lambda v: v >= 12_000, ">= 12,000 (typical ~20,300)",
    ),
    # Both retailers actually wrote recently (silent-skip detector). 26h
    # tolerates cron delay; the weekly workflow may run before a delayed daily.
    Check(
        "coles_scraped_recently",
        """select count(*) from scrape_runs
           where status in ('success', 'partial') and source = 'hotprices'
             and run_at > now() - interval '26 hours'""",
        lambda v: v >= 1, ">= 1 run in 26h",
    ),
    # The live Woolworths API only works from the residential Mac. When it
    # stops, the cloud cron silently serves the hotprices dump fallback
    # (~89% precision / ~78% recall measured 2026-09-24) — which is exactly
    # how a whole week went out on the fallback unnoticed. Both of these fail
    # loudly so a sleeping/offline Mac is an inbox ping, not a quiet
    # accuracy drop.
    Check(
        "woolies_live_heartbeat",
        """select count(*) from scrape_runs
           where status in ('success', 'partial') and source = 'woolies_catalogue'
             and run_at > now() - interval '36 hours'""",
        lambda v: v >= 1, ">= 1 live Woolies API run in 36h (residential Mac)",
    ),
    Check(
        "woolies_current_week_live",
        """select count(*) from specials s join products p on p.id = s.product_id
           where s.week_start = (select max(week_start) from specials)
             and s.is_half_price and p.retailer = 'woolworths'
             and s.source = 'woolies_catalogue'""",
        lambda v: v >= 400, ">= 400 live-sourced Woolies half-price rows this week",
        active=live_deadline_passed,   # from Wed 15:00 Sydney onwards
    ),
    Check(
        "woolies_scraped_recently",
        """select count(*) from scrape_runs
           where status in ('success', 'partial')
             and source in ('woolies_catalogue', 'hotprices')
             and run_at > now() - interval '26 hours'""",
        lambda v: v >= 1, ">= 1 run in 26h",
    ),
    # Derived data staleness (predictor / backtest / matcher are scheduled
    # weekly — 9 days flags a skipped week).
    # Predictions (hazard_v2) are recomputed daily after the week roll.
    Check(
        "predictions_fresh",
        "select coalesce(extract(epoch from now() - max(computed_at)) / 86400, 999) from predictions",
        lambda v: v <= 2, "newest <= 2 days old (daily recompute)",
    ),
    # Every shown window looks forward: a window already over means the daily
    # recompute has stopped and the app is showing "Window passed".
    Check(
        "no_expired_predictions",
        """select count(*) from predictions
           where predicted_window_end < (now() at time zone 'Australia/Sydney')::date""",
        lambda v: v == 0, "== 0 prediction windows already over",
    ),
    # The predictions writer keeps only the latest run (superseded runs are
    # pruned after the served week is snapshotted into prediction_shown), so
    # the app never shows a stale window for a product the model dropped.
    Check(
        "predictions_single_run",
        "select count(distinct computed_at) from predictions",
        lambda v: v <= 2, "<= 2 runs kept (latest only; 2 tolerates a mid-write read)",
    ),
    # The as-shown evaluation ledger recorded this promo week's predictions.
    # Warn only: it's evaluation history, not something users see.
    Check(
        "ledger_this_week",
        """select count(*) from prediction_shown
           where week_start = (select max(week_start) from specials)""",
        lambda v: v >= 1_000, ">= 1,000 shown predictions recorded this week",
        severity="warn",
        active=live_deadline_passed,
    ),
    Check(
        "predictions_floor",
        "select count(*) from predictions",
        lambda v: v >= 3_000, ">= 3,000",
    ),
    Check(
        "accuracy_fresh",
        "select coalesce(extract(epoch from now() - max(computed_at)) / 86400, 999) from accuracy_stats",
        lambda v: v <= 9, "newest <= 9 days old",
    ),
    Check(
        "counterpart_links_floor",
        "select count(*) from product_aliases where alias_type = 'counterpart'",
        lambda v: v >= 3_000, ">= 3,000 (typical ~5,300)",
    ),
    # Price sanity on the current week.
    Check(
        "no_inverted_prices",
        """select count(*) from specials
           where week_start = (select max(week_start) from specials)
             and sale_price_cents > regular_price_cents""",
        lambda v: v == 0, "== 0",
    ),
    # A not-half row at a half-looking discount shows a "1/2 price" badge in the
    # app. The bulk writer drops these (enforce_badge_invariant); this catches
    # any other path that writes them.
    Check(
        "no_ambiguous_half_badge",
        """select count(*) from specials
           where week_start = (select max(week_start) from specials)
             and not is_half_price and discount_pct >= 48""",
        lambda v: v == 0, "== 0 not-half rows at >= 48% off this week",
    ),
    # Category coverage doesn't regress (the source caps us around ~30% uncoded).
    Check(
        "uncategorised_share",
        """select round(100.0 * count(*) filter (where category = 'Uncategorised')
                  / greatest(count(*), 1)) from products""",
        lambda v: v <= 40, "<= 40% (typical ~28%)",
    ),
    # Coles accuracy vs the SaleFinder catalogue ground truth (src/audit_accuracy).
    # Recall floor — tolerant: passes (sentinel 100) until the probe first runs.
    Check(
        "coles_recall_floor",
        """select coalesce(
                 (select recall_pct from accuracy_audit
                  where retailer = 'coles' and source = 'salefinder_coles'
                  order by measured_at desc limit 1), 100)""",
        lambda v: v is None or v >= 70, ">= 70% (catalogue half-price we flag)",
    ),
    # Probe freshness — alarms only once it has ever run (dormant returns 0).
    Check(
        "coles_audit_fresh",
        """select case when (select count(*) from accuracy_audit where retailer = 'coles') = 0
                       then 0
                       else extract(epoch from now()
                            - (select max(measured_at) from accuracy_audit where retailer = 'coles')) / 86400
                  end""",
        lambda v: v <= 9, "newest <= 9 days (probe still running)",
    ),
    # Half-Price Dinners freshness — TOLERANT: only fails once the feature is
    # live (any recipe written in the last 8 days). Before the Groq key is
    # added the table is dormant and this passes vacuously, so it never
    # spuriously reddens. The generator self-validates its own >=2 floor.
    Check(
        "dinners_fresh_when_live",
        """select case
                 when (select count(*) from recipes
                       where generated_at > now() - interval '8 days') = 0 then 99
                 else (select count(*) from recipes
                       where week_start = (select max(week_start) from specials))
               end""",
        lambda v: v >= 2, ">= 2 this-week dinners when the feature is live (99 = dormant)",
    ),
    # Every priced dinner hero must still be half-price THIS week. The daily
    # --revalidate step repairs weeks whose heroes got pruned (residential
    # Woolies refresh / SaleFinder corrections remove dump false-positives);
    # this reddens the run if that repair ever breaks. Vacuous (0) when the
    # week has no recipes — existence is dinners_fresh_when_live's job.
    Check(
        "dinner_heroes_half_price",
        """select count(*)
             from recipes r
             cross join lateral jsonb_array_elements(r.ingredients) ing
            where r.week_start = (select max(week_start) from specials)
              and not exists (
                    select 1 from specials s
                     where s.week_start = r.week_start
                       and s.is_half_price
                       and s.product_id::text = ing->>'product_id')""",
        lambda v: v == 0, "0 dinner heroes without a current half-price special",
    ),
]


def effective_severity(check: Check, env: dict[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    if check.name in LIVE_WOOLIES_CHECKS and env.get("WOOLIES_LIVE_REQUIRED") == "0":
        return "warn"
    return check.severity


def evaluate(results: list[tuple[Check, Any]], env: dict[str, str] | None = None
             ) -> tuple[list[str], list[str]]:
    """Pure pass/fail aggregation: (failed names, warned names).

    ``results`` pairs each evaluated check with its SQL value; skipped
    (inactive) checks are simply absent.
    """
    failed: list[str] = []
    warned: list[str] = []
    for check, value in results:
        if check.ok(value):
            continue
        (warned if effective_severity(check, env) == "warn" else failed).append(check.name)
    return failed, warned


def verify(*, db_url: str, log: logging.Logger) -> int:
    """Run all checks; return the number of failures (warnings don't count)."""
    results: list[tuple[Check, Any]] = []
    with psycopg.connect(db_url, connect_timeout=30) as conn, conn.cursor() as cur:
        for check in CHECKS:
            if check.active is not None and not check.active():
                log.info("verify.skip %-26s (not active yet)", check.name)
                continue
            cur.execute(check.sql)
            results.append((check, cur.fetchone()[0]))
    failed, warned = evaluate(results)
    for check, value in results:
        if check.name in failed:
            log.error("verify.FAIL %-26s value=%s expected %s", check.name, value, check.expect)
        elif check.name in warned:
            log.warning("verify.WARN %-26s value=%s expected %s", check.name, value, check.expect)
        else:
            log.info("verify.pass %-26s value=%s", check.name, value)
    if failed:
        log.error("verify.result FAILED checks=%d/%d (warnings=%d) — data needs attention",
                  len(failed), len(results), len(warned))
    else:
        log.info("verify.result all %d checks passed (warnings=%d)", len(results), len(warned))
    return len(failed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="verify_data")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    log = configure_logging(verbose=args.verbose)

    if not os.environ.get("SUPABASE_DB_URL"):
        load_dotenv()
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        log.error("SUPABASE_DB_URL not set (env or .env file)")
        return 2

    return 1 if verify(db_url=db_url, log=log) else 0


if __name__ == "__main__":
    sys.exit(main())
