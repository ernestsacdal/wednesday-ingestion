"""Continuous Woolworths accuracy: what users were SERVED vs the live Half Price node.

The live Woolworths browse API is exhaustive ground truth (every item on the
Half Price node, with Woolworths' own IsHalfPrice flag), but it only answers
from a residential IP — so this runs inside the Mac's refresh, just BEFORE
the refresh replaces the served set. That measures exactly what the app was
showing since the last live write: ~100% after a live write, and the true
cost of the hotprices dump fallback whenever the Mac was off (measured
2026-09-24: precision 88.9%, recall 77.9%).

Results go to accuracy_audit (source 'woolies_api', migration 0025/0030).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date

import psycopg

from src.truth import TruthRow

_SAMPLE = 50


@dataclass
class WooliesScore:
    truth_half: int
    matched: int                    # truth-half items in our catalogue
    we_flag: int                    # ...that we served as half
    served_half: int
    false_pos: list[str] = field(default_factory=list)
    fp_unavailable: int = 0         # served half, node lists it unavailable
    missed: list[str] = field(default_factory=list)
    not_stocked: int = 0
    priced: int = 0
    price_exact: int = 0

    @property
    def recall_pct(self) -> float | None:
        # Denominator is EVERY real half-price item: a deal missing from our
        # catalogue is still a deal the app doesn't show. (The Coles probe's
        # stocked-only denominator overstates recall; see not_stocked.)
        return round(100 * self.we_flag / self.truth_half, 1) if self.truth_half else None

    @property
    def precision_pct(self) -> float | None:
        if not self.served_half:
            return None
        return round(100 * (1 - len(self.false_pos) / self.served_half), 1)

    @property
    def price_exact_pct(self) -> float | None:
        return round(100 * self.price_exact / self.priced, 1) if self.priced else None


def score(served: dict[str, tuple[bool, int | None]], truth: dict[str, TruthRow],
          stocked: set[str]) -> WooliesScore:
    """Pure scoring. ``served`` maps sku -> (is_half, sale_cents) for the week;
    ``truth`` is a COMPLETE node crawl (absent == not half-price); ``stocked``
    is every Woolworths sku in our products table."""
    truth_half = {sku for sku, t in truth.items() if t.is_half}
    served_half = {sku for sku, (half, _sale) in served.items() if half}
    matched = truth_half & stocked
    we_flag = matched & served_half
    false_pos = sorted(served_half - truth_half)
    priced = [sku for sku in we_flag
              if served[sku][1] is not None and truth[sku].sale_cents is not None]
    return WooliesScore(
        truth_half=len(truth_half),
        matched=len(matched),
        we_flag=len(we_flag),
        served_half=len(served_half),
        false_pos=false_pos,
        fp_unavailable=sum(1 for sku in false_pos
                           if sku in truth and truth[sku].available is False),
        missed=sorted(matched - served_half),
        not_stocked=len(truth_half - stocked),
        priced=len(priced),
        price_exact=sum(1 for sku in priced if served[sku][1] == truth[sku].sale_cents),
    )


def _load_served(cur: psycopg.Cursor, week: date):
    cur.execute("select retailer_sku from products where retailer = 'woolworths'")
    stocked = {r[0] for r in cur.fetchall()}
    cur.execute(
        """select p.retailer_sku, s.is_half_price, s.sale_price_cents, s.source
             from specials s join products p on p.id = s.product_id
            where p.retailer = 'woolworths' and s.week_start = %s""",
        (week,),
    )
    served: dict[str, tuple[bool, int | None]] = {}
    sources: dict[str, int] = {}
    for sku, half, sale, source in cur.fetchall():
        served[sku] = (half, sale)
        if half:
            sources[source] = sources.get(source, 0) + 1
    return served, stocked, sources


def audit_served(cur: psycopg.Cursor, *, week: date, truth: dict[str, TruthRow],
                 log: logging.Logger) -> WooliesScore | None:
    """Score the served week against ``truth`` and record it. Caller guarantees
    the crawl was complete and ``week`` is the promo week the crawl describes."""
    served, stocked, sources = _load_served(cur, week)
    if not served:
        log.info("audit_woolies.skip no served Woolworths rows for week=%s", week)
        return None
    sc = score(served, truth, stocked)
    note = (f"missed={len(sc.missed)} not_stocked={sc.not_stocked} "
            f"false_pos={len(sc.false_pos)} fp_unavailable={sc.fp_unavailable} "
            f"served_sources={','.join(f'{k}:{v}' for k, v in sorted(sources.items()))}")
    details = {
        "served_sources": sources,
        "false_pos_sample": sc.false_pos[:_SAMPLE],
        "missed_sample": sc.missed[:_SAMPLE],
        "served_half": sc.served_half,
    }
    cur.execute(
        """insert into accuracy_audit
               (retailer, week_start, source, ground_truth_half, matched, we_flag_half,
                recall_pct, precision_pct, price_exact_pct, sample_note, details)
           values ('woolworths', %s, 'woolies_api', %s, %s, %s, %s, %s, %s, %s, %s::jsonb)""",
        (week, sc.truth_half, sc.matched, sc.we_flag, sc.recall_pct, sc.precision_pct,
         sc.price_exact_pct, note, json.dumps(details)),
    )
    log.info("audit_woolies week=%s precision=%s%% recall=%s%% price_exact=%s%% %s",
             week, sc.precision_pct, sc.recall_pct, sc.price_exact_pct, note)
    return sc
