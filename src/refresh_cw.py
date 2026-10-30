"""Chemist Warehouse catalogue -> products + catalogue_deals (ADR-0004).

Runs daily (cloud cron and the Mac midday roll). For every catalogue the
publisher lists for Chemist Warehouse (CW), plus any --catalogue backfill, it
fetches each exact-product deal and writes:

  products            retailer 'chemist_warehouse', sku 'chemist_warehouse:<id>'
  catalogue_deals     one row per product per catalogue, pruned to what the
                      catalogue holds only after a complete fetch
  price_observations  'cw_catalogue' (the deal price, dated the catalogue's
                      start) and 'cw_edlp' (CW's own everyday price, from
                      "$X OFF EDLP" lines)
  catalogue_runs      what each fetch saw

It never writes specials, predictions, product_aliases or scrape_runs, so it
cannot move the app's week (ADR-0001) or reach the predictor or old apps.
A catalogue whose product data isn't published yet is logged and skipped;
that is the normal state for a few days after a catalogue starts.

    python -m src.refresh_cw --verbose                 # dry run
    python -m src.refresh_cw --write-db --verbose
    python -m src.refresh_cw --write-db --catalogue 38854918 \\
        --starts 2026-10-15 --ends 2026-10-28 --title "The House Of Wellness October 2026"
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date

from src.scrapers.cw_catalogue import (
    Catalogue,
    CatalogueFetch,
    build_session,
    fetch_catalogue,
    image_url,
    list_catalogues,
    product_url,
)

RETAILER = "chemist_warehouse"


def _sku(cw_id: str) -> str:
    return f"{RETAILER}:{cw_id}"


def write_fetch(fetch: CatalogueFetch, *, db_url: str, log: logging.Logger) -> int:
    """Write one fetched catalogue in a single transaction; return deals written."""
    import psycopg

    cat = fetch.catalogue
    with psycopg.connect(db_url, connect_timeout=30) as conn:
        with conn.cursor() as cur:
            cur.execute("select pg_advisory_xact_lock(hashtext('wednesday-ingest'))")
            ids: dict[str, str] = {}
            for d in fetch.deals:
                cur.execute(
                    """
                    insert into products
                        (retailer, retailer_sku, name, brand, category, image_url,
                         image_fetched_at, source_product_url, regular_price_cents, last_seen)
                    values (%s, %s, %s, %s, %s, %s, now(), %s, %s, now())
                    on conflict (retailer, retailer_sku) do update set
                        name = excluded.name,
                        brand = coalesce(excluded.brand, products.brand),
                        category = case when excluded.category <> 'Uncategorised'
                                        then excluded.category else products.category end,
                        image_url = coalesce(products.image_url, excluded.image_url),
                        source_product_url = excluded.source_product_url,
                        regular_price_cents = coalesce(products.regular_price_cents,
                                                       excluded.regular_price_cents),
                        last_seen = now()
                    returning id::text
                    """,
                    (RETAILER, _sku(d.cw_id), d.name, d.brand, d.category, image_url(d.cw_id),
                     product_url(d.cw_id, d.slug), d.edlp_cents),
                )
                pid = cur.fetchone()[0]
                ids[d.cw_id] = pid
                cur.execute(
                    """
                    insert into catalogue_deals
                        (product_id, retailer, catalogue_id, catalogue_title, starts_on, ends_on,
                         price_cents, rrp_cents, claim, half_rrp, off_edlp, page)
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    on conflict (product_id, catalogue_id) do update set
                        catalogue_title = excluded.catalogue_title,
                        starts_on = excluded.starts_on,
                        ends_on = excluded.ends_on,
                        price_cents = excluded.price_cents,
                        rrp_cents = excluded.rrp_cents,
                        claim = excluded.claim,
                        half_rrp = excluded.half_rrp,
                        off_edlp = excluded.off_edlp,
                        page = excluded.page,
                        last_seen = now()
                    """,
                    (pid, RETAILER, cat.nid, cat.title, cat.starts_on, cat.ends_on, d.price_cents,
                     d.rrp_cents, d.claim, d.half_rrp, d.off_edlp, d.page),
                )
                off_rrp = (round(100 * (d.rrp_cents - d.price_cents) / d.rrp_cents)
                           if d.rrp_cents else None)
                cur.execute(
                    """
                    insert into price_observations
                        (product_id, price_cents, is_special, discount_pct, observed_at, source)
                    values (%s, %s, true, %s, %s, 'cw_catalogue')
                    on conflict (product_id, observed_at, source) do update set
                        price_cents = excluded.price_cents, discount_pct = excluded.discount_pct
                    """,
                    (pid, d.price_cents, off_rrp, cat.starts_on),
                )
                if d.edlp_cents:
                    cur.execute(
                        """
                        insert into price_observations
                            (product_id, price_cents, is_special, observed_at, source)
                        values (%s, %s, false, %s, 'cw_edlp')
                        on conflict (product_id, observed_at, source) do update set
                            price_cents = excluded.price_cents
                        """,
                        (pid, d.edlp_cents, cat.starts_on),
                    )
            pruned = 0
            if fetch.complete:
                cur.execute(
                    """
                    delete from catalogue_deals
                     where retailer = %s and catalogue_id = %s
                       and not (product_id = any(%s::uuid[]))
                    """,
                    (RETAILER, cat.nid, list(ids.values())),
                )
                pruned = cur.rowcount
            _log_run(cur, fetch)
        conn.commit()
    log.info("cw.written catalogue=%s deals=%d pruned=%d", cat.nid, len(fetch.deals), pruned)
    return len(fetch.deals)


def _log_run(cur, fetch: CatalogueFetch) -> None:
    cat = fetch.catalogue
    cur.execute(
        """
        insert into catalogue_runs
            (retailer, catalogue_id, catalogue_title, starts_on, ends_on,
             regions, products, exact, half_rrp, complete, error)
        values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (RETAILER, cat.nid, cat.title, cat.starts_on, cat.ends_on, fetch.regions,
         fetch.products, len(fetch.deals), fetch.half_rrp, fetch.complete, fetch.error),
    )


def log_pending(fetch: CatalogueFetch, *, db_url: str) -> None:
    import psycopg

    with psycopg.connect(db_url, connect_timeout=30) as conn, conn.cursor() as cur:
        _log_run(cur, fetch)
        conn.commit()


def run(*, db_url: str | None, log: logging.Logger, backfill: list[Catalogue]) -> int:
    session = build_session()
    catalogues = list_catalogues(session, log)
    if not catalogues and not backfill:
        log.warning("cw.no_catalogues — feed down or nothing listed; nothing written")
        return 0
    seen = {c.nid for c in catalogues}
    catalogues += [c for c in backfill if c.nid not in seen]
    written = 0
    for cat in catalogues:
        fetch = fetch_catalogue(cat, session, log)
        if fetch.error == "regions_pending":
            log.info("cw.regions_pending catalogue=%s title=%r starts=%s — product data not published yet",
                     cat.nid, cat.title, cat.starts_on)
        if not db_url:
            for d in sorted(fetch.deals, key=lambda d: d.name)[:5]:
                log.info("cw.sample %s | $%.2f | rrp %s | %s | %s", d.name[:50], d.price_cents / 100,
                         f"${d.rrp_cents / 100:.2f}" if d.rrp_cents else "-", d.category, d.claim[:40])
            continue
        if fetch.deals:
            written += write_fetch(fetch, db_url=db_url, log=log)
        else:
            log_pending(fetch, db_url=db_url)
    log.info("cw.done catalogues=%d deals_written=%d", len(catalogues), written)
    return 0


def main(argv: list[str] | None = None) -> int:
    from src.env import load_dotenv
    from src.scrapers.base import configure_logging

    p = argparse.ArgumentParser(prog="refresh_cw", description=__doc__.splitlines()[0])
    p.add_argument("--write-db", action="store_true")
    p.add_argument("--catalogue", help="backfill one catalogue id (with --starts/--ends/--title)")
    p.add_argument("--starts", type=date.fromisoformat)
    p.add_argument("--ends", type=date.fromisoformat)
    p.add_argument("--title", default="")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)
    log = configure_logging(verbose=args.verbose)
    load_dotenv()
    backfill: list[Catalogue] = []
    if args.catalogue:
        if not (args.starts and args.ends):
            p.error("--catalogue needs --starts and --ends")
        backfill.append(Catalogue(args.catalogue, args.title, args.starts, args.ends))
    db_url = None
    if args.write_db:
        db_url = os.environ.get("SUPABASE_DB_URL")
        if not db_url:
            log.error("SUPABASE_DB_URL not set")
            return 2
    return run(db_url=db_url, log=log, backfill=backfill)


if __name__ == "__main__":
    sys.exit(main())
