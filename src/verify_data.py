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
Requires SUPABASE_DB_URL.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from dataclasses import dataclass

import psycopg

from src.env import load_dotenv
from src.scrapers.base import configure_logging


@dataclass
class Check:
    name: str
    sql: str
    ok: "callable"  # scalar -> bool
    expect: str     # human description of the passing condition


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
    # Stale week being served as current.
    Check(
        "week_is_current",
        """select (select max(week_start) from specials)
                = (current_date - ((extract(dow from current_date)::int - 3 + 7) % 7))::date""",
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
    Check(
        "predictions_fresh",
        "select coalesce(extract(epoch from now() - max(computed_at)) / 86400, 999) from predictions",
        lambda v: v <= 9, "newest <= 9 days old",
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
    # spuriously reddens. The generator self-validates its own >=4 floor.
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
]


def verify(*, db_url: str, log: logging.Logger) -> int:
    """Run all checks; return the number of failures."""
    failures = 0
    with psycopg.connect(db_url, connect_timeout=30) as conn, conn.cursor() as cur:
        for check in CHECKS:
            cur.execute(check.sql)
            value = cur.fetchone()[0]
            if check.ok(value):
                log.info("verify.pass %-26s value=%s", check.name, value)
            else:
                failures += 1
                log.error("verify.FAIL %-26s value=%s expected %s",
                          check.name, value, check.expect)
    if failures:
        log.error("verify.result FAILED checks=%d/%d — data needs attention",
                  failures, len(CHECKS))
    else:
        log.info("verify.result all %d checks passed", len(CHECKS))
    return failures


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
