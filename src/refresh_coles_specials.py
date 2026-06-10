"""Trickle-scrape the full Coles half-price list, then replace StockUp Coles.

DEPRECATED (2026-06-07): Coles now comes from the public hotprices.org dump
(src/refresh_coles_hotprices.py) — accurate, free, and no Cloudflare fight. This
fragile self-scrape is retired from the hot path and kept only as an emergency
fallback. Do not wire it back into the pipeline without a clear reason.

Coles rate-limits the website to ~3-5 requests before a long (>7 min) cooldown,
and its BFF is Imperva-walled — so unlike Woolworths there's no fast/clean pull.
This collects the full ~1,130-item half-price list the only way that works:
fetch pages until Coles challenges, then go TRULY QUIET for ~15 min (no requests
at all, so the rate-limit token bucket refills) and resume. Progress is
persisted after every page, so an interrupted run resumes where it left off.

Only once ~all items are collected does it write them (source=coles_catalogue,
real prices + deterministic CDN image) and delete the week's wrong StockUp Coles
specials. A short run that can't finish leaves StockUp in place (no coverage
regression) and keeps its progress for the next run.

Runtime: ~1-2 hours (mostly the quiet gaps). Run locally; the weekly cron can't
do this (datacenter IP + the rate limit), so re-run manually each week.

    python -m src.refresh_coles_specials --verbose

Requires SUPABASE_DB_URL.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg

from src.db.writer import write_to_db
from src.models import ScrapeOutput, ScrapeRun
from src.scrapers import coles_specials
from src.scrapers.base import configure_logging

_PROGRESS_PATH = Path(__file__).resolve().parent.parent / "data" / "coles_trickle_progress.json"
_QUIET_GAP_S = 900       # 15 min of true quiet after a challenge — lets the bucket refill
_PAGE_DELAY_S = 5        # polite gap between successful pages
_MAX_QUIET_WAITS = 10    # give up after this many consecutive failed waits (~2.5 hr cap)
_MAX_PAGES = 40
# "Complete" = collected within 10% OR within an absolute slack of the catalogue
# total. The absolute slack absorbs week-to-week catalogue churn (a few products
# ending) that would otherwise make a valid full pull read as "incomplete".
_COMPLETE_FRACTION = 0.90
_COMPLETE_ABS_SLACK = 40
_SLEEP_CHUNK_S = 30      # heartbeat granularity during a quiet gap


def _sleep_with_heartbeat(seconds: int, log: logging.Logger) -> None:
    """Sleep in short chunks with a heartbeat log, so a long quiet gap looks
    alive (not hung) and Ctrl-C lands promptly between chunks rather than being
    swallowed by one 15-minute blocking call."""
    waited = 0
    while waited < seconds:
        time.sleep(min(_SLEEP_CHUNK_S, seconds - waited))
        waited += _SLEEP_CHUNK_S
        if waited < seconds:
            log.info("coles_trickle.waiting %ds/%ds", min(waited, seconds), seconds)


def _load_progress() -> dict:
    if _PROGRESS_PATH.is_file():
        try:
            return json.loads(_PROGRESS_PATH.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _save_progress(products: dict, last_page: int, total: int | None) -> None:
    _PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _PROGRESS_PATH.write_text(
        json.dumps({"products": products, "last_page": last_page, "total": total}),
        encoding="utf-8",
    )


def trickle(log: logging.Logger) -> tuple[list[dict], int | None, bool]:
    """Collect the half-price products. Returns (products, total, complete)."""
    prog = _load_progress()
    products: dict[str, dict] = prog.get("products", {})
    last_page: int = prog.get("last_page", 0)
    total: int | None = prog.get("total")
    if products:
        log.info("coles_trickle.resume collected=%d last_page=%d total=%s",
                 len(products), last_page, total)

    session = coles_specials.build_coles_session()
    page = last_page + 1
    quiet_waits = 0

    while page <= _MAX_PAGES:
        sr = coles_specials.fetch_search_results_once(session, page)
        if sr is None:
            quiet_waits += 1
            _save_progress(products, last_page, total)
            log.warning(
                "coles_trickle.challenged page=%d quiet_wait=%d/%d sleeping=%ds collected=%d/%s",
                page, quiet_waits, _MAX_QUIET_WAITS, _QUIET_GAP_S, len(products), total,
            )
            if quiet_waits > _MAX_QUIET_WAITS:
                log.error("coles_trickle.giving_up after %d quiet waits", quiet_waits)
                break
            _sleep_with_heartbeat(_QUIET_GAP_S, log)
            continue

        quiet_waits = 0
        if total is None:
            total = sr.get("noOfResults")
        new = 0
        for p in sr.get("results") or []:
            if p.get("_type") != "PRODUCT":
                continue
            pid = str(p.get("id"))
            if pid in products:
                continue
            products[pid] = {
                "_type": "PRODUCT",
                "id": p.get("id"),
                "name": p.get("name"),
                "brand": p.get("brand"),
                "pricing": p.get("pricing"),
                "imageUris": p.get("imageUris"),
            }
            new += 1
        last_page = page
        _save_progress(products, last_page, total)
        log.info("coles_trickle.page page=%d new=%d collected=%d/%s", page, new, len(products), total)
        if new == 0:
            break  # past the end
        if total and len(products) >= total:
            break
        page += 1
        time.sleep(_PAGE_DELAY_S)

    complete = bool(total) and (
        len(products) >= total * _COMPLETE_FRACTION
        or len(products) >= total - _COMPLETE_ABS_SLACK
    )
    return list(products.values()), total, complete


def _load_dotenv() -> None:
    for env_path in (Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"):
        if not env_path.is_file():
            continue
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k = k.strip(); v = v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
        break


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="refresh_coles_specials")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument(
        "--force-write", action="store_true",
        help="Publish + replace StockUp even if the collection is incomplete "
             "(manual override for a run stuck behind Cloudflare; requires >=50%% collected).",
    )
    args = parser.parse_args(argv)
    log = configure_logging(verbose=args.verbose)

    if not os.environ.get("SUPABASE_DB_URL"):
        _load_dotenv()
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        log.error("SUPABASE_DB_URL not set (env or .env file)")
        return 2

    products, total, complete = trickle(log)
    if not products:
        log.error("coles_trickle.no_products")
        return 1

    week_start = coles_specials._most_recent_wednesday()
    week_end = week_start + timedelta(days=6)
    scraped_at = datetime.now(timezone.utc)
    specials = []
    for p in products:
        sp = coles_specials.to_special(p, week_start=week_start, week_end=week_end, scraped_at=scraped_at)
        if sp:
            specials.append(sp)
    log.info("coles_trickle.mapped products=%d specials=%d complete=%s total=%s",
             len(products), len(specials), complete, total)

    if not complete and not args.force_write:
        log.warning("coles_trickle.incomplete collected=%d/%s — NOT replacing StockUp; "
                    "re-run to resume, or pass --force-write to publish a partial set",
                    len(products), total)
        return 1
    if not complete:
        # Force-write override: refuse if we have so little that replacing
        # StockUp would gut Coles coverage; otherwise publish the partial set.
        if total and len(products) < total * 0.5:
            log.error("coles_trickle.force_refused collected=%d/%s is under 50%% — too little to "
                      "replace StockUp; resume the scrape first", len(products), total)
            return 1
        log.warning("coles_trickle.force_write publishing PARTIAL set collected=%d/%s — StockUp "
                    "Coles will be replaced; uncollected items show no current special",
                    len(products), total)

    # Full set collected: write authoritative Coles + delete the week's StockUp Coles.
    run = ScrapeRun(source="coles_catalogue", started_at=scraped_at,
                    source_url=coles_specials._HALF_PRICE_URL)
    run.finalise(status="success", items=len(specials))
    result = write_to_db(ScrapeOutput(run=run, specials=specials), db_url=db_url, log=log)

    with psycopg.connect(db_url, connect_timeout=15) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                delete from specials s using products p
                where s.product_id = p.id and p.retailer = 'coles'
                  and s.week_start = %(w)s and s.source in ('stockup_post', 'stockup_sheet')
                """,
                {"w": week_start},
            )
            deleted = cur.rowcount
        conn.commit()

    log.info("coles_trickle.replaced written=%d deleted_stockup=%d week_start=%s",
             result.specials_written, deleted, week_start)
    if _PROGRESS_PATH.is_file():
        _PROGRESS_PATH.unlink()
    return 0


if __name__ == "__main__":
    sys.exit(main())
