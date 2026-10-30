"""Chemist Warehouse usual prices from its own product pages (Mac only; ADR-0004).

Chemist Warehouse (CW) prices catalogue deals off RRP, so the honest saving is
against what CW normally charges. Between catalogues a product page shows that
normal price (and the RRP). This checks CW products that aren't on a deal and
haven't been checked in CHECK_EVERY_DAYS days, politely, one page every
DELAY_S seconds, and records each price as a 'cw_page' observation.
catalogue_usual() in the database takes the median and ignores any check that
turns out to fall inside one of the product's own deals.

Plain HTTP with an honest User-Agent; robots.txt allows /buy/ (it disallows
/api/ and the promotions filters, which this never touches). Runs from the
Mac's residential IP in the midday roll; stops after BLOCK_STREAK refusals in
a row so a block never turns into a hammering.

    python -m src.cw_usual_prices --verbose               # dry run (no writes)
    python -m src.cw_usual_prices --write-db --limit 200 --verbose
"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time

from src.scrapers.cw_catalogue import build_session

CHECK_EVERY_DAYS = 13
DELAY_S = 1.5
BLOCK_STREAK = 3
FLUSH_EVERY = 25   # prices per database write; a killed run loses at most this many

# The page's data holds one prices entry for the product:
#   "prices":[{"sku":"…","price":{"value":{"amount":65.99,…},"type":"…","rrp":{"amount":77.99,…}}}]
_PRICE = re.compile(r'"prices":\[\{"sku":"[^"]*","price":\{"value":\{"amount":([0-9]+(?:\.[0-9]+)?)')
_RRP = re.compile(r'"rrp":\{"amount":([0-9]+(?:\.[0-9]+)?)')


def parse_page(html: str) -> tuple[int | None, int | None]:
    """A /buy/ page -> (price_cents, rrp_cents)."""
    m = _PRICE.search(html)
    if not m:
        return None, None
    price = round(float(m.group(1)) * 100)
    r = _RRP.search(html, m.end(), m.end() + 300)
    rrp = round(float(r.group(1)) * 100) if r else None
    return (price if price > 0 else None), rrp


_CANDIDATES = """
select p.id::text, p.source_product_url
  from products p
 where p.retailer = 'chemist_warehouse'
   and p.source_product_url is not null
   and not exists (
         select 1 from catalogue_deals d
          where d.product_id = p.id
            and sydney_date() between d.starts_on and d.ends_on)
   and not exists (
         select 1 from price_observations o
          where o.product_id = p.id and o.source = 'cw_page'
            and o.observed_at > sydney_date() - %(every)s)
 order by exists (select 1 from device_watchlists w
                   where p.id = any(w.watched_product_ids)) desc,
          (select max(d.ends_on) from catalogue_deals d where d.product_id = p.id) desc nulls last,
          p.id
 limit %(limit)s
"""


def _flush(db_url: str, rows: list[tuple[str, int]]) -> int:
    """Write (product id, price) pairs as today's 'cw_page' checks."""
    import psycopg

    with psycopg.connect(db_url, connect_timeout=30) as conn, conn.cursor() as cur:
        cur.executemany(
            """
            insert into price_observations
                (product_id, price_cents, is_special, observed_at, source)
            values (%s, %s, false, sydney_date(), 'cw_page')
            on conflict (product_id, observed_at, source) do update set
                price_cents = excluded.price_cents
            """,
            rows,
        )
        cur.executemany(
            "update products set regular_price_cents = %s, last_seen = now() where id = %s",
            [(price, pid) for pid, price in rows],
        )
        conn.commit()
    return len(rows)


def run(*, db_url: str, write: bool, limit: int, log: logging.Logger) -> int:
    import psycopg

    with psycopg.connect(db_url, connect_timeout=30) as conn, conn.cursor() as cur:
        cur.execute(_CANDIDATES, {"every": CHECK_EVERY_DAYS, "limit": limit})
        todo = cur.fetchall()
    log.info("cw_usual.candidates n=%d", len(todo))
    session = build_session()
    checked = written = missing = 0
    refused = 0
    pending: list[tuple[str, int]] = []
    for pid, url in todo:
        try:
            r = session.get(url, timeout=25)
        except Exception as e:  # noqa: BLE001
            log.warning("cw_usual.request_error url=%s err=%s", url, e)
            time.sleep(DELAY_S)
            continue
        checked += 1
        if r.status_code in (403, 429):
            refused += 1
            log.warning("cw_usual.refused status=%s url=%s streak=%d", r.status_code, url, refused)
            if refused >= BLOCK_STREAK:
                log.error("cw_usual.stopped — %d refusals in a row; CW may be blocking checks", refused)
                break
            time.sleep(DELAY_S * 4)
            continue
        refused = 0
        if r.status_code == 404:
            missing += 1
            time.sleep(DELAY_S)
            continue
        price, rrp = parse_page(r.text) if r.status_code == 200 else (None, None)
        if price is None:
            log.info("cw_usual.no_price status=%s url=%s", r.status_code, url)
        elif write:
            pending.append((pid, price))
            if len(pending) >= FLUSH_EVERY:
                written += _flush(db_url, pending)
                pending = []
        else:
            log.info("cw_usual.dry %s price=%s rrp=%s", url, price, rrp)
        time.sleep(DELAY_S)
    if pending:
        written += _flush(db_url, pending)
    log.info("cw_usual.done checked=%d written=%d missing=%d refused_streak=%d",
             checked, written, missing, refused)
    return 0


def main(argv: list[str] | None = None) -> int:
    from src.env import load_dotenv
    from src.scrapers.base import configure_logging

    p = argparse.ArgumentParser(prog="cw_usual_prices", description=__doc__.splitlines()[0])
    p.add_argument("--write-db", action="store_true")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)
    log = configure_logging(verbose=args.verbose)
    load_dotenv()
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        log.error("SUPABASE_DB_URL not set")
        return 2
    return run(db_url=db_url, write=args.write_db, limit=args.limit, log=log)


if __name__ == "__main__":
    sys.exit(main())
