"""Continuous accuracy probe: measure our half-price data against an external
ground truth and record it (accuracy_audit), optionally correcting misses.

Coles (cloud-reachable): SaleFinder's weekly catalogue carries an explicit
"1/2 PRICE" flag and joins to our ids by SKU. We measure:
  * recall          — of catalogue half-price items we have, how many we flag half
  * precision sample — of catalogue NON-half items we have, how many we wrongly flag half
  * price-exact     — sale-price agreement on the matched half set
and (with --correct) flip/insert the handful of confirmed-half items our dump
derivation missed, so Coles' own catalogue is authoritative.

Writes one accuracy_audit row per source so verify_data can alarm on regression
or staleness. Runs in the cloud cron (SaleFinder isn't IP-blocked).

    python -m src.audit_accuracy --verbose                 # dry-run measure
    python -m src.audit_accuracy --write-db --correct -v   # record + fix misses

Requires SUPABASE_DB_URL.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone

import psycopg

from src.env import load_dotenv
from src.scrapers.base import configure_logging
from src.scrapers.salefinder import fetch_coles_catalogue


def _load_our_coles(db_url: str):
    """Return (have: set[id], half: set[id], special: {id:(is_half,sale_cents)}, pid:{id:uuid})."""
    with psycopg.connect(db_url, connect_timeout=20) as c, c.cursor() as cur:
        cur.execute("select max(week_start) from specials")
        week = cur.fetchone()[0]
        cur.execute(
            "select split_part(retailer_sku, ':', 2), id::text from products where retailer = 'coles'"
        )
        pid = {sku: pid for sku, pid in cur.fetchall()}
        have = set(pid)
        cur.execute(
            """select split_part(p.retailer_sku, ':', 2), s.is_half_price, s.sale_price_cents
               from specials s join products p on p.id = s.product_id
               where p.retailer = 'coles' and s.week_start = %s""",
            (week,),
        )
        special = {}
        half = set()
        for cid, is_half, sale in cur.fetchall():
            special[cid] = (is_half, sale)
            if is_half:
                half.add(cid)
    return have, half, special, pid, week


def _correct(db_url, week, fixes, pid, log):
    """Upsert confirmed-half catalogue items our data didn't flag (source='salefinder')."""
    if not fixes:
        return 0
    now = datetime.now(timezone.utc)
    applied = 0
    with psycopg.connect(db_url, connect_timeout=20) as conn:
        with conn.cursor() as cur:
            cur.execute("select pg_advisory_xact_lock(hashtext('wednesday-ingest'))")
            for it in fixes:
                product_id = pid.get(it.coles_id)
                if not product_id:
                    continue  # not in our catalogue — can't FK; skip (logged by caller)
                sale = it.sale_cents or 0
                reg = it.was_cents or (sale * 2 if sale else 0)
                if sale <= 0 or reg <= 0:
                    continue
                pct = round(100 * (reg - sale) / reg)
                cur.execute(
                    """
                    insert into specials
                        (product_id, week_start, week_end, regular_price_cents,
                         sale_price_cents, discount_pct, is_half_price, source)
                    values (%s, %s, %s, %s, %s, %s, true, 'salefinder')
                    on conflict (product_id, week_start) do update set
                        is_half_price = true,
                        sale_price_cents = excluded.sale_price_cents,
                        regular_price_cents = greatest(specials.regular_price_cents, excluded.regular_price_cents),
                        discount_pct = excluded.discount_pct,
                        source = 'salefinder'
                    """,
                    (product_id, week, week, reg, sale, pct),
                )
                applied += 1
        conn.commit()
    log.info("audit.corrected applied=%d (source=salefinder)", applied)
    return applied


def run(*, db_url, log, write_db, correct) -> int:
    cat = fetch_coles_catalogue(log)
    if cat.error or not cat.half:
        # Soft skip: SaleFinder unreachable / no catalogue this week must NOT
        # fail the cron. The verify_data freshness invariant catches persistent
        # failure (no audit row in >9 days) and alarms then.
        log.warning("audit.coles_skip err=%s (probe produced no data; cron stays green)", cat.error)
        return 0
    have, our_half, special, pid, week = _load_our_coles(db_url)

    gt_half = cat.half
    matched = [i for i in gt_half if i.coles_id in have]            # catalogue-half we stock
    we_flag = [i for i in matched if i.coles_id in our_half]         # ...and flag half
    missed = [i for i in matched if i.coles_id not in our_half]      # ...we missed
    not_stocked = [i for i in gt_half if i.coles_id not in have]     # catalogue-half not in our catalogue

    # precision sample: catalogue NON-half items we stock but wrongly flag half
    gt_nonhalf = [i for i in cat.items if not i.is_half and i.coles_id in have]
    false_pos = [i for i in gt_nonhalf if i.coles_id in our_half]

    # price agreement on the matched half set
    priced = [i for i in we_flag if i.sale_cents and special.get(i.coles_id, (None, None))[1]]
    price_exact = sum(1 for i in priced if special[i.coles_id][1] == i.sale_cents)

    recall = round(100 * len(we_flag) / len(matched), 1) if matched else None
    prec_sample = round(100 * (1 - len(false_pos) / len(gt_nonhalf)), 1) if gt_nonhalf else None
    price_pct = round(100 * price_exact / len(priced), 1) if priced else None

    log.info("audit.coles week=%s catalogue_half=%d matched=%d we_flag=%d missed=%d not_stocked=%d",
             week, len(gt_half), len(matched), len(we_flag), len(missed), len(not_stocked))
    log.info("audit.coles RECALL=%s%% (we flag %d/%d catalogue half we stock)",
             recall, len(we_flag), len(matched))
    log.info("audit.coles precision_sample=%s%% (%d false-positives of %d catalogue non-half we stock)",
             prec_sample, len(false_pos), len(gt_nonhalf))
    log.info("audit.coles price_exact=%s%% on %d priced", price_pct, len(priced))
    for i in missed[:10]:
        log.info("   MISSED %s %s (catalogue %s)", i.coles_id, i.name[:42], i.discount_desc)

    if write_db:
        now = datetime.now(timezone.utc)
        with psycopg.connect(db_url, connect_timeout=20) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """insert into accuracy_audit
                       (retailer, week_start, source, ground_truth_half, matched,
                        we_flag_half, recall_pct, precision_pct, price_exact_pct,
                        sample_note, measured_at)
                       values ('coles', %s, 'salefinder_coles', %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (week, len(gt_half), len(matched), len(we_flag), recall, prec_sample,
                     price_pct, f"missed={len(missed)} not_stocked={len(not_stocked)} "
                                f"false_pos={len(false_pos)} sales={','.join(cat.sale_ids)}", now),
                )
            conn.commit()
        log.info("audit.coles written to accuracy_audit")

    if correct:
        n = _correct(db_url, week, missed, pid, log)
        skipped = sum(1 for i in not_stocked)
        log.info("audit.coles corrections=%d (skipped %d catalogue-half not in our catalogue)", n, skipped)

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="audit_accuracy")
    parser.add_argument("--write-db", action="store_true", help="Record the measurement in accuracy_audit.")
    parser.add_argument("--correct", action="store_true",
                        help="Flip/insert catalogue-confirmed half-price items our derivation missed.")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    log = configure_logging(verbose=args.verbose)

    if not os.environ.get("SUPABASE_DB_URL"):
        load_dotenv()
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        log.error("SUPABASE_DB_URL not set (env or .env file)")
        return 2
    return run(db_url=db_url, log=log, write_db=args.write_db, correct=args.correct)


if __name__ == "__main__":
    sys.exit(main())
