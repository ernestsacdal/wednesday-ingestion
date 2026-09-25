"""Ground-truth capture (truth_snapshots, migration 0030).

Every authoritative reading we get — the live Woolworths Half Price node from
the residential Mac, the SaleFinder catalogues — is stored per promo week and
SKU, independent of what the pipeline decided to serve. That history is what
the derivation harness, the Woolworths audit and prediction labels are judged
against. Rows are upserted: a repeat sighting bumps last_seen/seen_count, so
"what did the source say on day D" is first_seen <= D <= last_seen.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import psycopg

SOURCES = ("woolies_api", "salefinder_coles", "salefinder_woolies")
_BATCH = 500


@dataclass(frozen=True)
class TruthRow:
    retailer_sku: str               # 'woolworths:<stockcode>' / 'coles:<id>'
    is_half: bool
    sale_cents: int | None = None
    was_cents: int | None = None
    available: bool | None = None
    instore_special: bool | None = None
    promo_desc: str | None = None


def upsert_truth(cur: psycopg.Cursor, *, source: str, retailer: str,
                 week_start: date, rows: list[TruthRow]) -> int:
    """Insert or refresh ``rows`` for one (source, retailer, week). Returns count."""
    if source not in SOURCES:
        raise ValueError(f"unknown truth source {source!r}")
    by_sku = {r.retailer_sku: r for r in rows}   # last reading wins within a batch
    unique = list(by_sku.values())
    for i in range(0, len(unique), _BATCH):
        batch = unique[i:i + _BATCH]
        ph = ",".join(["(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"] * len(batch))
        flat = [v for r in batch for v in (
            source, retailer, week_start, r.retailer_sku, r.is_half, r.sale_cents,
            r.was_cents, r.available, r.instore_special,
            (r.promo_desc or None) and r.promo_desc[:60],
        )]
        cur.execute(
            f"""
            insert into truth_snapshots
                (source, retailer, week_start, retailer_sku, is_half, sale_cents,
                 was_cents, available, instore_special, promo_desc)
            values {ph}
            on conflict (source, retailer, week_start, retailer_sku) do update set
                is_half = excluded.is_half,
                sale_cents = excluded.sale_cents,
                was_cents = excluded.was_cents,
                available = excluded.available,
                instore_special = excluded.instore_special,
                promo_desc = excluded.promo_desc,
                last_seen = now(),
                seen_count = least(truth_snapshots.seen_count + 1, 32767)
            """,
            flat,
        )
    return len(unique)


def load_truth(cur: psycopg.Cursor, *, source: str, retailer: str,
               week_start: date) -> dict[str, TruthRow]:
    cur.execute(
        """select retailer_sku, is_half, sale_cents, was_cents, available,
                  instore_special, promo_desc
             from truth_snapshots
            where source = %s and retailer = %s and week_start = %s""",
        (source, retailer, week_start),
    )
    return {r[0]: TruthRow(*r) for r in cur.fetchall()}
