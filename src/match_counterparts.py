"""Cross-retailer counterpart matcher — precision first.

Links the same product at Coles and Woolworths so the app's "Also at /
Cheaper here" comparison works beyond exact-name matches (~10% of deals).
The two stores rarely spell a product identically, but brand + size +
near-identical residual names identify it reliably.

Match pipeline (every gate must pass; anything uncertain simply does not
link — a missing comparison costs nothing, a wrong one costs trust):
  1. Normalize names (lowercase, strip accents/punctuation).
  2. Parse quantities into a canonical TOTAL per unit family (5x70g ->
     350g; 24x375ml -> 9000ml; 1.2kg -> 1200g), plus pack counts. When
     BOTH names state a quantity the totals must agree, and counts must
     agree when both state one. Most names in this catalogue carry NO
     size at all — those may still match, but only at a higher
     similarity bar with tighter price sanity (price is the size proxy).
  3. Candidate generation via rarest-token postings: each Coles product
     is compared against the Woolies items sharing its 3 most
     identifying words. (First-token blocking misses the main prize:
     Woolies prefixes manufacturers — "Arnott's Tim Tam" — where Coles
     starts at the brand — "Tim Tam".)
  4. Score the full names: 0.5 * token-set Jaccard + 0.5 * SequenceMatcher
     on the sorted-token string. Threshold: 0.60 when a shared quantity
     anchors the pair, 0.75 otherwise.
  5. Price sanity: when both regular prices are known, max/min <= 1.6
     (<= 1.4 for quantity-less pairs) — identical products at the two
     majors are never far apart.
  6. Mutual best: keep a pair only when each side is the other's best
     candidate (one-to-one).

Writes product_aliases rows (alias_type='counterpart', canonical = Coles,
variant = Woolies, confidence = score, created_by='auto'); idempotent —
each run replaces all counterpart rows.

CLI:
    python -m src.match_counterparts --verbose              # dry run + sample
    python -m src.match_counterparts --sample out.json      # dump audit sample
    python -m src.match_counterparts --write-db --verbose   # write links
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sys
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher

import psycopg

from src.env import load_dotenv
from src.scrapers.base import configure_logging

SCORE_THRESHOLD_SIZED = 0.60   # both names state the same quantity
SCORE_THRESHOLD_UNSIZED = 0.75 # no quantity anchor — name must carry it all
PRICE_RATIO_SIZED = 1.6
PRICE_RATIO_UNSIZED = 1.4
_RARE_TOKENS = 3               # postings consulted per Coles item
_BATCH = 500

_MULTI_RE = re.compile(r"(\d+)\s*[x×]\s*(\d+(?:\.\d+)?)\s*(g|kg|ml|l)\b")
_SINGLE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(g|kg|ml|l)\b")
_COUNT_RE = re.compile(
    r"(\d+)\s*(?:pk|pack|packs|each|ea|bags?|rolls?|sheets?|capsules?|tablets?|wipes|pods?|loads?|pairs?|serves?)\b"
)


def _normalize(name: str) -> str:
    s = unicodedata.normalize("NFKD", name.lower())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9.]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _base(qty: float, unit: str) -> tuple[str, float]:
    """(family, base-units): kg->g, l->ml."""
    if unit == "kg":
        return "g", qty * 1000
    if unit == "l":
        return "ml", qty * 1000
    return unit, qty


def parse_quantity(normalized: str) -> tuple[tuple[tuple[str, float], ...], tuple[int, ...]]:
    """Canonical quantity signature: (((family, total), ...), (counts...)).

    Multipacks contribute n*q so "5x70g" and "350g" share the signature.
    Totals are summed per family (g / ml); counts are kept separately so
    "24x375ml" (no count token) still matches "375ml 24pk" via the total.
    """
    totals: dict[str, float] = defaultdict(float)
    counts: list[int] = []
    rest = normalized
    for m in _MULTI_RE.finditer(rest):
        n, qty, unit = m.groups()
        family, base = _base(float(qty), unit)
        totals[family] += int(n) * base
    rest = _MULTI_RE.sub(" ", rest)
    for m in _SINGLE_RE.finditer(rest):
        qty, unit = m.groups()
        family, base = _base(float(qty), unit)
        totals[family] += base
    for m in _COUNT_RE.finditer(rest):
        counts.append(int(m.group(1)))
    sig = tuple(sorted((fam, round(total, 2)) for fam, total in totals.items() if total > 0))
    return sig, tuple(sorted(counts))


# Filler words that would otherwise pull half the catalogue into one
# candidate list. Size-ish tokens (anything starting with a digit) are
# skipped at index time — the size set is already the blocking key.
_STOPWORDS = frozenset(
    "the and with of a in for x ml g kg l pk pack packs each ea value multipack".split()
)


@dataclass
class Item:
    pid: str
    retailer: str
    name: str
    regular_cents: int | None
    norm: str
    tokens: frozenset[str]
    sorted_tokens: str
    quantity: tuple[tuple[str, float], ...]
    counts: tuple[int, ...]

    @property
    def block_key(self) -> tuple | None:
        """Quantity totals when present, else pack counts, else unmatchable."""
        if self.quantity:
            return self.quantity
        if self.counts:
            return ("ct", self.counts)
        return None

    @property
    def index_tokens(self) -> list[str]:
        return [t for t in self.tokens if t not in _STOPWORDS and not t[0].isdigit()]


def _item(pid: str, retailer: str, name: str, regular: int | None) -> Item:
    norm = _normalize(name)
    tokens = frozenset(norm.split())
    quantity, counts = parse_quantity(norm)
    return Item(
        pid=pid, retailer=retailer, name=name, regular_cents=regular,
        norm=norm, tokens=tokens, sorted_tokens=" ".join(sorted(tokens)),
        quantity=quantity, counts=counts,
    )


def score(a: Item, b: Item) -> float:
    union = a.tokens | b.tokens
    jaccard = len(a.tokens & b.tokens) / len(union) if union else 0.0
    seq = SequenceMatcher(None, a.sorted_tokens, b.sorted_tokens).ratio()
    return 0.5 * jaccard + 0.5 * seq


def _price_ok(a: Item, b: Item, ratio_max: float) -> bool:
    if a.regular_cents is None or b.regular_cents is None:
        return True
    lo, hi = sorted((a.regular_cents, b.regular_cents))
    if lo <= 0:
        return True
    return hi / lo <= ratio_max


@dataclass
class Match:
    coles: Item
    woolies: Item
    score: float


# Cheap Jaccard pre-gate before the (slower) SequenceMatcher runs. A pair
# below this token overlap can't reach the score threshold anyway.
_JACCARD_GATE = 0.30


def _counts_ok(a: Item, b: Item) -> bool:
    """Pack counts must agree when BOTH names state one ("36pk" vs "54pk")."""
    if not a.counts or not b.counts:
        return True
    return a.counts == b.counts


# Variant discriminators. STRICT tokens (colors, heat levels) identify a
# different product even when only one side states them ("V Blue" vs "V");
# SOFT tokens (flavors/scents) reject only when both sides state one and
# they conflict ("Citrus" vs "Lily") — a one-sided flavor is usually just
# a longer name for the same product ("Lemon" vs "Lemon Cream").
_STRICT_VARIANT = frozenset(
    "blue red green black white pink purple gold silver yellow "
    "hot mild".split()
)
_SOFT_VARIANT = frozenset(
    "citrus lily lavender rose vanilla chocolate strawberry banana mango "
    "berry apple peach mint peppermint spearmint caramel coffee coconut "
    "honey tropical pineapple raspberry blueberry passionfruit grape "
    "watermelon cherry eucalyptus menthol aloe lemon lime orange medium".split()
)


def _variant_ok(a: Item, b: Item) -> bool:
    if (a.tokens & _STRICT_VARIANT) != (b.tokens & _STRICT_VARIANT):
        return False
    sa, sb = a.tokens & _SOFT_VARIANT, b.tokens & _SOFT_VARIANT
    if sa and sb and not (sa <= sb or sb <= sa):
        return False
    return True


def _brand_ok(a: Item, b: Item) -> bool:
    """The leading token of one name must appear somewhere in the other.

    Both catalogues lead with the brand (or its manufacturer), so a true
    pair always shares it: "Tim Tam ..." appears inside "Arnott's Tim Tam
    ...". This kills the house-brand trap — "Coles Storage Bags" vs "Glad
    Storage Bags" describe the same thing but are different products.
    """
    a_first = a.norm.split(" ", 1)[0] if a.norm else ""
    b_first = b.norm.split(" ", 1)[0] if b.norm else ""
    return (a_first in b.tokens) or (b_first in a.tokens)


def _quantity_gates(a: Item, b: Item) -> tuple[float, float] | None:
    """(score threshold, price ratio cap) for the pair, or None to reject."""
    if a.quantity and b.quantity:
        if a.quantity != b.quantity:
            return None
        return SCORE_THRESHOLD_SIZED, PRICE_RATIO_SIZED
    # One or both names carry no quantity — the name + price must carry it.
    return SCORE_THRESHOLD_UNSIZED, PRICE_RATIO_UNSIZED


def find_matches(items: list[Item], log: logging.Logger) -> list[Match]:
    coles = [it for it in items if it.retailer == "coles" and it.index_tokens]
    woolies = [it for it in items if it.retailer == "woolworths" and it.index_tokens]

    # Inverted index: meaningful token -> woolies items.
    postings: dict[str, list[Item]] = defaultdict(list)
    for w in woolies:
        for tok in w.index_tokens:
            postings[tok].append(w)

    best_for: dict[str, tuple[str, float]] = {}
    pair_lookup: dict[tuple[str, str], Match] = {}
    compared = 0
    for c in coles:
        # Consult only the rarest postings — common words ('chocolate')
        # would pull half the catalogue in; rare words identify products.
        toks = sorted(c.index_tokens, key=lambda t: len(postings.get(t, ())))
        candidates: dict[str, Item] = {}
        for tok in toks[:_RARE_TOKENS]:
            for w in postings.get(tok, []):
                candidates[w.pid] = w
        for w in candidates.values():
            gates = _quantity_gates(c, w)
            if (
                gates is None
                or not _counts_ok(c, w)
                or not _brand_ok(c, w)
                or not _variant_ok(c, w)
            ):
                continue
            threshold, price_cap = gates
            union = c.tokens | w.tokens
            jaccard = len(c.tokens & w.tokens) / len(union) if union else 0.0
            if jaccard < _JACCARD_GATE:
                continue
            compared += 1
            s = score(c, w)
            if s < threshold or not _price_ok(c, w, price_cap):
                continue
            if s > best_for.get(c.pid, ("", -1.0))[1]:
                best_for[c.pid] = (w.pid, s)
            if s > best_for.get(w.pid, ("", -1.0))[1]:
                best_for[w.pid] = (c.pid, s)
            pair_lookup[(c.pid, w.pid)] = Match(c, w, s)

    matches: list[Match] = []
    for (c_pid, w_pid), m in pair_lookup.items():
        if best_for.get(c_pid, ("",))[0] == w_pid and best_for.get(w_pid, ("",))[0] == c_pid:
            matches.append(m)
    log.info("match.scored=%d candidates=%d mutual_best=%d",
             compared, len(pair_lookup), len(matches))
    return matches


def load_items(db_url: str, log: logging.Logger) -> list[Item]:
    items: list[Item] = []
    with psycopg.connect(db_url, connect_timeout=30) as conn, conn.cursor() as cur:
        cur.execute(
            "select id::text, retailer, name, regular_price_cents from products",
        )
        for pid, retailer, name, regular in cur.fetchall():
            items.append(_item(pid, retailer, name, regular))
    log.info("match.loaded products=%d", len(items))
    return items


def write_matches(db_url: str, matches: list[Match], log: logging.Logger) -> None:
    with psycopg.connect(db_url, connect_timeout=30) as conn:
        with conn.cursor() as cur:
            cur.execute("delete from product_aliases where alias_type = 'counterpart'")
            for i in range(0, len(matches), _BATCH):
                batch = matches[i:i + _BATCH]
                ph = ",".join(["(%s,%s,'counterpart',%s,'auto')"] * len(batch))
                flat: list = []
                for m in batch:
                    flat.extend([m.coles.pid, m.woolies.pid, round(min(m.score, 1.0), 2)])
                cur.execute(
                    f"""
                    insert into product_aliases
                        (canonical_product_id, variant_product_id, alias_type, confidence, created_by)
                    values {ph}
                    on conflict (canonical_product_id, variant_product_id) do update set
                        alias_type = excluded.alias_type,
                        confidence = excluded.confidence
                    """,
                    flat,
                )
        conn.commit()
    log.info("match.written counterpart_links=%d", len(matches))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="match_counterparts")
    parser.add_argument("--write-db", action="store_true",
                        help="Replace counterpart rows in product_aliases.")
    parser.add_argument("--sample", metavar="PATH",
                        help="Write a random sample of proposed matches to a JSON file for audit.")
    parser.add_argument("--sample-size", type=int, default=80)
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)
    log = configure_logging(verbose=args.verbose)

    if not os.environ.get("SUPABASE_DB_URL"):
        load_dotenv()
    db_url = os.environ.get("SUPABASE_DB_URL")
    if not db_url:
        log.error("SUPABASE_DB_URL not set (env or .env file)")
        return 2

    items = load_items(db_url, log)
    matches = find_matches(items, log)
    matches.sort(key=lambda m: -m.score)

    print(f"\nproposed counterpart links: {len(matches)}")
    for m in matches[:6]:
        print(f"  {m.score:.2f}  {m.coles.name[:46]:<46} <-> {m.woolies.name[:46]}")

    if args.sample:
        rng = random.Random(42)
        sample = rng.sample(matches, min(args.sample_size, len(matches)))
        payload = [
            {
                "score": round(m.score, 3),
                "coles_name": m.coles.name,
                "coles_regular_cents": m.coles.regular_cents,
                "woolies_name": m.woolies.name,
                "woolies_regular_cents": m.woolies.regular_cents,
                "quantity": [f"{total:g}{fam}" for fam, total in m.coles.quantity],
            }
            for m in sample
        ]
        with open(args.sample, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        print(f"audit sample written: {args.sample} ({len(payload)} pairs)")

    if args.write_db:
        write_matches(db_url, matches, log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
