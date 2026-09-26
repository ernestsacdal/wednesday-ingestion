"""Isotonic (PAV) calibration: turn a raw prediction score into an honest hit rate.

The statistical predictor's confidence is a heuristic
(0.5 * cycle_score + 0.5 * dispersion_score), not a probability: scored as
users saw them, predictions shown at 0.80-0.90 landed ~52%. Calibration maps
each raw score to the hit rate actually observed for similar scores, fitted
per retailer on as-shown outcomes (src/eval/predictions_eval.py), so the
"X% confident" the app prints is what happened, not what the formula hoped.

Pool-adjacent-violators gives the best non-decreasing step function; blocks
are then merged until each holds at least ``min_block`` outcomes so a handful
of lucky claims can't produce a 0.95. Outputs are clamped to [0.05, 0.95].

Fits come only from the RAW era (claims computed before RAW_ERA_END): after
that the stored confidence is itself calibrated, and refitting on it would
calibrate a calibration. Prediction v2 fits its own map from walk-forward
raw scores and reuses these functions.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from src.weeks import SYDNEY

# First calibrated predictor run; ledger claims before this carry raw scores.
RAW_ERA_END = datetime(2026, 9, 27, tzinfo=SYDNEY)

CLAMP_LO, CLAMP_HI = 0.05, 0.95
MIN_BLOCK = 200


@dataclass(frozen=True)
class Block:
    upper: float      # highest raw score in the block
    value: float      # observed hit rate for the block
    n: int


def pav_fit(pairs: list[tuple[float, bool]], *, min_block: int = MIN_BLOCK) -> list[Block]:
    """Non-decreasing step function from (raw score, hit) pairs."""
    if not pairs:
        return []
    pts = sorted(pairs)
    # Collapse identical scores first (the heuristic score is 2-decimal).
    blocks: list[list[float]] = []            # [upper, hits, n]
    for score, hit in pts:
        if blocks and blocks[-1][0] == score:
            blocks[-1][1] += hit
            blocks[-1][2] += 1
        else:
            blocks.append([score, float(hit), 1])
    # Pool adjacent violators.
    stack: list[list[float]] = []
    for b in blocks:
        stack.append(list(b))
        while len(stack) > 1 and stack[-2][1] / stack[-2][2] >= stack[-1][1] / stack[-1][2]:
            upper, hits, n = stack.pop()
            stack[-1] = [upper, stack[-1][1] + hits, stack[-1][2] + n]
    # Enforce a minimum block size by merging small blocks into a neighbour
    # (merging adjacent monotone blocks keeps the function monotone).
    changed = True
    while changed and len(stack) > 1:
        changed = False
        for i, b in enumerate(stack):
            if b[2] >= min_block:
                continue
            j = i + 1 if i + 1 < len(stack) else i - 1
            lo, hi = sorted((i, j))
            merged = [stack[hi][0], stack[lo][1] + stack[hi][1], stack[lo][2] + stack[hi][2]]
            stack[lo:hi + 1] = [merged]
            changed = True
            break
    return [Block(upper=b[0], value=round(b[1] / b[2], 4), n=int(b[2])) for b in stack]


def apply(blocks: list[Block], score: float) -> float:
    """Calibrated probability for ``score`` (clamped). Identity-free: no fit -> clamp."""
    if not blocks:
        return round(min(max(score, CLAMP_LO), CLAMP_HI), 2)
    for b in blocks:
        if score <= b.upper:
            return round(min(max(b.value, CLAMP_LO), CLAMP_HI), 2)
    return round(min(max(blocks[-1].value, CLAMP_LO), CLAMP_HI), 2)


def to_json(blocks: list[Block]) -> list[dict]:
    return [{"upper": b.upper, "value": b.value, "n": b.n} for b in blocks]


def from_json(raw: list[dict]) -> list[Block]:
    return [Block(float(b["upper"]), float(b["value"]), int(b["n"])) for b in raw]


def store(cur, retailer: str, blocks: list[Block], *, method: str = "statistical") -> None:
    n = sum(b.n for b in blocks)
    cur.execute(
        """insert into prediction_eval (eval_kind, method, retailer, n, details)
           values ('calibration', %s, %s, %s, %s::jsonb)""",
        (method, retailer, n, json.dumps({"blocks": to_json(blocks),
                                          "raw_era_end": RAW_ERA_END.isoformat()})),
    )


def load_latest(cur, *, method: str = "statistical") -> dict[str, list[Block]]:
    """Most recent stored map per retailer ({} if none)."""
    cur.execute(
        """select distinct on (retailer) retailer, details
             from prediction_eval
            where eval_kind = 'calibration' and method = %s and retailer is not null
            order by retailer, computed_at desc""",
        (method,),
    )
    return {r: from_json(d["blocks"]) for r, d in cur.fetchall()}
