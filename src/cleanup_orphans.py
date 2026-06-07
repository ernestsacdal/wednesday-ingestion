"""Remove orphan products — those with no special at all.

After the StockUp purge, ~2,700 products linger with zero specials (neither
current nor historical): leftovers from the retired StockUp era that aren't in
our authoritative hotprices/Woolies data. They never appear in any half-price
list, only in search, and most of the catalogue's remaining image gaps are these.
Removing them keeps search on-brand (only products that have actually been on
half-price) and clears the image-coverage tail in one move. Anything that goes on
special again is re-added automatically by the next refresh.

Safe via FK cascade: every table referencing products(id) is ON DELETE CASCADE
(predictions / price_observations / specials / aliases / favourites / alerts),
so deleting the product row removes its children too.

DESTRUCTIVE: requires --confirm. Default is a dry run that only reports counts.

    python -m src.cleanup_orphans            # dry run
    python -m src.cleanup_orphans --confirm  # delete orphan products

Requires SUPABASE_DB_URL.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import psycopg

from src.scrapers.base import configure_logging

_ORPHAN_PREDICATE = "not exists (select 1 from specials s where s.product_id = p.id)"


def cleanup(*, db_url: str, log: logging.Logger, confirm: bool) -> dict[str, int]:
    with psycopg.connect(db_url, connect_timeout=20) as conn:
        with conn.cursor() as cur:
            cur.execute(f"select retailer, count(*) from products p where {_ORPHAN_PREDICATE} group by retailer order by 1")
            by_retailer = cur.fetchall()
            total = sum(n for _r, n in by_retailer)

            if not confirm:
                log.warning("cleanup_orphans.dry_run would_delete=%d by_retailer=%s "
                            "(re-run with --confirm to delete)", total, by_retailer)
                return {"would_delete": total}

            cur.execute(f"delete from products p where {_ORPHAN_PREDICATE}")
            deleted = cur.rowcount
        conn.commit()

    log.info("cleanup_orphans.done deleted=%d by_retailer_before=%s", deleted, by_retailer)
    return {"deleted": deleted}


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
    parser = argparse.ArgumentParser(prog="cleanup_orphans")
    parser.add_argument("--confirm", action="store_true", help="Actually delete (omit for a dry run).")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    log = configure_logging(verbose=args.verbose)

    if not os.environ.get("SUPABASE_DB_URL"):
        _load_dotenv()
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        log.error("SUPABASE_DB_URL not set (env or .env file)")
        return 2

    cleanup(db_url=db_url, log=log, confirm=args.confirm)
    return 0


if __name__ == "__main__":
    sys.exit(main())
