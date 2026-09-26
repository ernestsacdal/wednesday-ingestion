"""Score predictions the way users SAW them, and publish honest hit rates.

The old published numbers came from src/backtest.py's replay (first window
after each historical sale): 63.5% overall / 73.5% high. Scored against what
the app actually showed (first measured 2026-09-26 over the June-July weeks),
the statistical model landed ~42% overall — BELOW the naive guess "half-price
sometime in the next k promo weeks" at the same width (~47%): its symmetric
mean +/- sd windows sit too late for a gap distribution that peaks at 2 weeks.

The fair baseline is that guess said on the shown Wednesday: the k weeks
AFTER the current one (users can already see this week isn't a sale week).

Pipeline:
  ledger   prediction_shown (migration 0031): per (product, promo week), the
           prediction in effect once that week's specials were live — only
           ACTIVE claims (window not over) for products not already half-price
           that week. backfill_ledger() rebuilds it from the append-only
           predictions history; snapshot_week() adds the current week daily.
  claims   each distinct prediction (product, computed_at) is scored ONCE,
           from the first promo week it was shown, so a prediction that sat
           on screen for weeks isn't counted weeks times.
  score    hit = the product went half-price in a promo week overlapping the
           part of the window still ahead; width = those promo weeks. Only
           windows that have fully elapsed are scored.
  publish  prediction_eval (overall / per retailer / per bucket, with naive
           same-width baseline, reliability table, ECE, Brier) and
           accuracy_stats — which the app's product page quotes — bucketed by
           the APP's thresholds (>=0.7 high, 0.5-0.7 medium, 0.3-0.5 low).

    python -m src.eval.predictions_eval --backfill --score --write-db
    python -m src.eval.predictions_eval --snapshot --score --write-db   # daily
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

import psycopg

from src.env import load_dotenv
from src.scrapers.base import configure_logging
from src.weeks import SYDNEY, current_promo_week, promo_week_start

# The in-effect cutoff for a promo week: a prediction computed after this
# (Sydney time, on the Wednesday) wasn't what users saw at the week's start.
_SHOWN_AT = time(13, 30)
_RETENTION_WEEKS = 26
# Only claims first shown at least this long ago are scored, so every window
# from those weeks has had time to finish. Scoring newer weeks would keep only
# their SHORT windows (the long ones haven't resolved) and bias hit rates low.
_MIN_AGE_WEEKS = 8
_BATCH = 1000

# The app's verdict thresholds (mobile/src/lib/predictions.ts verdictTier),
# mapped to the accuracy_stats tiers its product page quotes.
APP_BUCKETS = (("high", 0.70), ("medium", 0.50), ("low", 0.30))


@dataclass(frozen=True)
class Pred:
    product_id: str
    window_start: date
    window_end: date
    confidence: float
    method: str
    computed_at: datetime


@dataclass(frozen=True)
class Scored:
    product_id: str
    retailer: str
    week: date
    confidence: float
    method: str
    hit: bool
    width: int
    naive_hit: bool


def app_bucket(confidence: float) -> str | None:
    for name, floor in APP_BUCKETS:
        if confidence >= floor:
            return name
    return None


def shown_cutoff(week: date) -> datetime:
    return datetime.combine(week, _SHOWN_AT, tzinfo=SYDNEY)


def weeks_between(first: date, last: date) -> list[date]:
    out, w = [], promo_week_start(first)
    while w <= last:
        out.append(w)
        w += timedelta(days=7)
    return out


def in_effect(history: list[Pred], cutoff: datetime) -> Pred | None:
    """Latest prediction computed at or before ``cutoff`` (history sorted by computed_at)."""
    i = bisect_right([p.computed_at for p in history], cutoff)
    return history[i - 1] if i else None


def ledger_rows(history_by_product: dict[str, list[Pred]],
                half_weeks: dict[str, set[date]], weeks: list[date]) -> list[tuple]:
    """Ledger rows for ``weeks``: the active, not-already-half claim per product."""
    rows = []
    for pid, history in history_by_product.items():
        halves = half_weeks.get(pid, set())
        for w in weeks:
            p = in_effect(history, shown_cutoff(w))
            if p is None or p.window_end < w or w in halves:
                continue
            rows.append((pid, w, p.window_start, p.window_end, p.confidence,
                         p.method, p.computed_at))
    return rows


def first_shown_claims(rows: list[tuple]) -> list[tuple]:
    """One row per distinct prediction: the first promo week it was shown."""
    first: dict[tuple[str, datetime], tuple] = {}
    for row in sorted(rows, key=lambda r: r[1]):
        first.setdefault((row[0], row[6]), row)
    return list(first.values())


def score_claim(row: tuple, halves: set[date], current_week: date) -> tuple[bool, int, bool] | None:
    """(hit, width, naive_hit) for one ledger claim, or None if not yet resolvable.

    The claim is judged from its shown week W: the remaining window is
    [max(window_start, W), window_end]; any half-price promo week overlapping
    it is a hit. The naive baseline is "a half-price week among the ``width``
    promo weeks after W" — the same width, no timing skill.
    """
    _pid, week, ws, we, _conf, _method, _at = row
    first = promo_week_start(max(ws, week))
    last = promo_week_start(we)
    if last >= current_week:          # the window hasn't fully elapsed
        return None
    covered = weeks_between(first, last)
    hit = any(w in halves for w in covered)
    naive = any(week + timedelta(days=7 * (k + 1)) in halves for k in range(len(covered)))
    return hit, len(covered), naive


def summarize(scored: list[Scored]) -> dict:
    """Hit rate, width, naive baseline, reliability table, ECE and Brier."""
    n = len(scored)
    if not n:
        return {"n": 0}
    bins: dict[int, list[Scored]] = defaultdict(list)
    for s in scored:
        bins[min(int(s.confidence * 10), 9)].append(s)
    table, ece = [], 0.0
    for b in sorted(bins):
        group = bins[b]
        conf = sum(s.confidence for s in group) / len(group)
        acc = sum(s.hit for s in group) / len(group)
        ece += len(group) / n * abs(acc - conf)
        table.append({"bin": f"{b / 10:.1f}-{(b + 1) / 10:.1f}", "n": len(group),
                      "mean_conf": round(conf, 3), "hit_rate": round(acc, 3)})
    return {
        "n": n,
        "hits": sum(s.hit for s in scored),
        "hit_rate": round(sum(s.hit for s in scored) / n, 4),
        "mean_width_weeks": round(sum(s.width for s in scored) / n, 2),
        "naive_hit_rate": round(sum(s.naive_hit for s in scored) / n, 4),
        "ece": round(ece, 4),
        "brier": round(sum((s.confidence - s.hit) ** 2 for s in scored) / n, 4),
        "reliability": table,
    }


# ---------------------------------------------------------------- DB layer

def _load(cur) -> tuple[dict[str, list[Pred]], dict[str, set[date]], dict[str, str]]:
    cur.execute("""select product_id::text, predicted_window_start, predicted_window_end,
                          confidence::float, method, computed_at
                     from predictions order by product_id, computed_at""")
    history: dict[str, list[Pred]] = defaultdict(list)
    for r in cur.fetchall():
        history[r[0]].append(Pred(*r))
    cur.execute("select product_id::text, week_start from specials where is_half_price")
    halves: dict[str, set[date]] = defaultdict(set)
    for pid, wk in cur.fetchall():
        halves[pid].add(promo_week_start(wk))
    cur.execute("select id::text, retailer from products where id in "
                "(select distinct product_id from predictions)")
    retailer = dict(cur.fetchall())
    return history, halves, retailer


def _served_week(cur) -> date | None:
    cur.execute("select max(week_start) from specials")
    return cur.fetchone()[0]


def _upsert_ledger(cur, rows: list[tuple]) -> None:
    for i in range(0, len(rows), _BATCH):
        batch = rows[i:i + _BATCH]
        ph = ",".join(["(%s,%s,%s,%s,%s,%s,%s)"] * len(batch))
        cur.execute(
            f"""insert into prediction_shown
                    (product_id, week_start, window_start, window_end, confidence,
                     method, computed_at)
                values {ph}
                on conflict (product_id, week_start) do nothing""",
            [v for r in batch for v in r],
        )


def _load_ledger(cur) -> list[tuple]:
    cur.execute("""select product_id::text, week_start, window_start, window_end,
                          confidence::float, method, computed_at from prediction_shown""")
    return cur.fetchall()


def _write_results(cur, scored: list[Scored], log: logging.Logger) -> dict:
    now = datetime.now(SYDNEY)
    overall = summarize(scored)
    groups: dict[tuple[str | None, str | None], list[Scored]] = {(None, None): scored}
    for s in scored:
        groups.setdefault((s.retailer, None), []).append(s)
        bucket = app_bucket(s.confidence)
        if bucket:
            groups.setdefault((None, bucket), []).append(s)
    method = scored[0].method if scored else "statistical"
    for (retailer, bucket), group in groups.items():
        summ = summarize(group)
        cur.execute(
            """insert into prediction_eval
                   (computed_at, eval_kind, method, retailer, bucket, n, hit_rate,
                    mean_width_weeks, naive_hit_rate, ece, brier, details)
               values (%s, 'as_shown', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)""",
            (now, method, retailer, bucket, summ["n"], summ.get("hit_rate"),
             summ.get("mean_width_weeks"), summ.get("naive_hit_rate"), summ.get("ece"),
             summ.get("brier"), json.dumps({"reliability": summ.get("reliability", [])})),
        )
    # accuracy_stats: what the product page quotes ("calls like this one landed X%").
    cur.execute("delete from accuracy_stats")
    tiers = {name: [s for s in scored if app_bucket(s.confidence) == name]
             for name, _floor in APP_BUCKETS}
    tiers["overall"] = scored
    for tier, group in tiers.items():
        cur.execute("insert into accuracy_stats (tier, windows_tested, hits, computed_at) "
                    "values (%s, %s, %s, %s)",
                    (tier, len(group), sum(s.hit for s in group), now))
    log.info("predictions_eval.written groups=%d accuracy_stats tiers=%d", len(groups), len(tiers))
    return overall


def run(*, db_url: str, log: logging.Logger, backfill: bool, snapshot: bool,
        score: bool, write_db: bool) -> dict | None:
    with psycopg.connect(db_url, connect_timeout=30) as conn, conn.cursor() as cur:
        history, halves, retailer = _load(cur)
        served = _served_week(cur)
        current = current_promo_week()
        rows: list[tuple] = []
        if backfill or snapshot:
            if backfill:
                first = min(p.computed_at for h in history.values() for p in h)
                weeks = weeks_between(first.astimezone(SYDNEY).date(), served)
            else:
                weeks = [served]          # only once this week's specials are live
            rows = ledger_rows(history, halves, weeks)
            log.info("predictions_eval.ledger weeks=%s..%s rows=%d",
                     weeks[0] if weeks else None, weeks[-1] if weeks else None, len(rows))
            if write_db:
                _upsert_ledger(cur, rows)
                cur.execute("delete from prediction_shown where week_start < %s",
                            (current - timedelta(weeks=_RETENTION_WEEKS),))
        overall = None
        if score:
            # A dry run scores the rows it just built; otherwise score the stored ledger.
            ledger = rows if (rows and not write_db) else _load_ledger(cur)
            scored = []
            oldest_scored = current - timedelta(weeks=_MIN_AGE_WEEKS)
            for claim in first_shown_claims(ledger):
                if claim[1] > oldest_scored:
                    continue
                res = score_claim(claim, halves.get(claim[0], set()), current)
                if res is None:
                    continue
                hit, width, naive = res
                scored.append(Scored(claim[0], retailer.get(claim[0], "?"), claim[1],
                                     claim[4], claim[5], hit, width, naive))
            overall = summarize(scored)
            log.info("predictions_eval.as_shown n=%s hit=%s naive=%s width=%s ece=%s brier=%s",
                     overall.get("n"), overall.get("hit_rate"), overall.get("naive_hit_rate"),
                     overall.get("mean_width_weeks"), overall.get("ece"), overall.get("brier"))
            if write_db and scored:
                _write_results(cur, scored, log)
        if write_db:
            conn.commit()
    return overall


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="predictions_eval")
    parser.add_argument("--backfill", action="store_true",
                        help="Rebuild the ledger from the full predictions history.")
    parser.add_argument("--snapshot", action="store_true",
                        help="Record this promo week's shown predictions (idempotent).")
    parser.add_argument("--score", action="store_true", help="Score resolvable claims.")
    parser.add_argument("--write-db", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    log = configure_logging(verbose=args.verbose)
    if not os.environ.get("SUPABASE_DB_URL"):
        load_dotenv()
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        log.error("SUPABASE_DB_URL not set (env or .env file)")
        return 2
    run(db_url=db_url, log=log, backfill=args.backfill, snapshot=args.snapshot,
        score=args.score, write_db=args.write_db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
