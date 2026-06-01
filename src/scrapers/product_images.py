"""Name-based product image discovery via the Woolworths internal API.

Why this shape and not "scrape both retailers"?

Phase 1 probe of Coles + Woolies search pages showed:
- **Woolies has a usable internal JSON API.** The SPA POSTs to
  https://www.woolworths.com.au/apis/ui/Search/products and gets back
  clean product data including `LargeImageFile` URLs on the
  `cdn0.woolworths.media` CDN. No JS rendering needed.
- **Coles is gated.** Their search page returns a 4.5KB shell to non-
  browser requests (likely Cloudflare bot detection), and we couldn't
  find an unauthenticated JSON endpoint. Direct access would need
  Playwright OR deeper reverse-engineering — both bigger work items
  than this session's scope.

Pragmatic strategy this session:
1. **Woolies products**: hit the Woolies API directly. Clean, fast.
2. **Coles products**: query the *same* Woolies API by product name. If
   Woolies has a token-overlap match (≥60%), reuse that image. This
   works because big-brand grocery items (Tim Tams, Cadbury, Coke etc.)
   are the same physical product with the same packaging at both
   retailers. House brands (Coles brand bread, Woolworths Free Range
   Eggs) won't match and stay null.
3. **Misses** keep image_url=null. The mobile app falls back to the
   retailer pill — identical to today's behaviour.

A Coles-direct adapter is the obvious next step (separate session +
Playwright). Until then this gets us photos on ~50-70% of products
without any browser automation.

Public surface:
    lookup_image(session, retailer, product_name, log) -> ImageLookupResult
    build_image_session() -> requests.Session

Per-call cost: 1 POST to Woolies API + a 0.3s polite delay. ~500ms total.
"""
from __future__ import annotations

import logging
import re
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Literal

import requests

# Woolworths' internal search API. Stable enough that we can hit it directly.
# The SPA's GET search page is a thin wrapper around this POST.
_WOOLIES_SEARCH_API = "https://www.woolworths.com.au/apis/ui/Search/products"
_WOOLIES_REFERER = "https://www.woolworths.com.au/shop/search/products"

# Browser-like headers — the API endpoint rejects requests without a real UA.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-AU,en;q=0.9",
    "Content-Type": "application/json",
}

# Match-strictness threshold. 0.6 = 60% of query tokens must appear in the
# candidate's name. Empirically rejects "tim tam candle" when we asked for
# "tim tam original 200g".
_MIN_TOKEN_OVERLAP = 0.6

# Polite delay between requests (per session, per worker).
_DELAY_SECONDS = 0.3

# Normalise punctuation + collapse whitespace before tokenising.
_NORMALISE_RE = re.compile(r"[^a-z0-9\s]+")
_WHITESPACE_RE = re.compile(r"\s+")


ImageLookupMethod = Literal[
    "woolies_api_direct",         # Woolies product matched against Woolies API
    "woolies_api_cross_retailer", # Coles product matched against Woolies API by name
    "coles_sitemap_cdn",          # Coles product resolved via the product sitemap -> deterministic CDN
    "woolworths_sitemap_cdn",     # Woolies product resolved via the product sitemap -> deterministic CDN
    "miss",
]


@dataclass
class ImageLookupResult:
    image_url: str | None
    canonical_product_url: str | None
    method: ImageLookupMethod
    score: float = 0.0  # token overlap of best match; 0.0 on miss
    error: str | None = None


def build_image_session() -> requests.Session:
    """Session pre-loaded with browser-like headers + Woolies search cookies.

    Each worker should call this once. The initial GET primes Akamai's
    cookie jar so subsequent POSTs to the API don't get challenged.
    """
    s = requests.Session()
    s.headers.update(_BROWSER_HEADERS)
    # Prime cookies. Failure here is non-fatal; the API often works without.
    try:
        s.get(_WOOLIES_REFERER, timeout=15)
    except requests.RequestException:
        pass
    return s


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def lookup_image(
    session: requests.Session,
    retailer: str,
    product_name: str,
    log: logging.Logger,
) -> ImageLookupResult:
    """Find a product image URL for (retailer, product_name).

    Strategy:
      * retailer == 'woolworths'  → direct Woolies API match
      * retailer == 'coles'       → cross-retailer match via Woolies API

    Never raises. On any error returns ImageLookupResult(image_url=None,
    error="...") so the backfill loop stays simple.
    """
    if not product_name.strip():
        return ImageLookupResult(None, None, "miss", error="empty product name")

    try:
        products = _woolies_search(session, product_name)
    except requests.RequestException as e:
        return ImageLookupResult(
            None, None, "miss", error=f"woolies_request_error: {type(e).__name__}: {e}",
        )
    except ValueError as e:
        return ImageLookupResult(
            None, None, "miss", error=f"woolies_parse_error: {e}",
        )

    best = _pick_best_match(product_name, products)
    if best is None:
        return ImageLookupResult(
            None, None, "miss", error="no_woolies_match_above_threshold",
        )

    image_url, product_url, score = best
    if not image_url:
        return ImageLookupResult(
            None, product_url, "miss", error="match_had_no_image_url",
        )

    method: ImageLookupMethod = (
        "woolies_api_direct" if retailer == "woolworths" else "woolies_api_cross_retailer"
    )
    return ImageLookupResult(
        image_url=image_url,
        canonical_product_url=product_url,
        method=method,
        score=score,
    )


# ---------------------------------------------------------------------------
# Woolworths API adapter
# ---------------------------------------------------------------------------

def _woolies_search(session: requests.Session, query: str) -> list[dict[str, Any]]:
    """POST to the Woolies search API; return flattened product dicts.

    The response shape is `{Products: [{Products: [<product>, ...]}, ...]}`
    — the outer Products is a list of "tiles" (sometimes promotional
    groupings) and each tile has an inner Products list with the actual
    SKUs. We flatten to a single list of product dicts.
    """
    payload = {
        "SearchTerm": query,
        "PageNumber": 1,
        "PageSize": 24,
        "SortType": "TraderRelevance",
        "Location": "/shop/search/products",
        "Filters": [],
    }
    # Referer must be ASCII-safe — smart quotes / accented chars break latin-1
    # encoding that the requests lib uses on header values.
    encoded_q = urllib.parse.quote_plus(query)
    headers = {**_BROWSER_HEADERS, "Referer": f"{_WOOLIES_REFERER}?searchTerm={encoded_q}"}
    resp = session.post(_WOOLIES_SEARCH_API, json=payload, headers=headers, timeout=20)
    time.sleep(_DELAY_SECONDS)
    if resp.status_code != 200:
        raise ValueError(f"http_{resp.status_code}")
    try:
        body = resp.json()
    except ValueError as e:
        raise ValueError(f"non_json_response: {e}")

    flattened: list[dict[str, Any]] = []
    for tile in body.get("Products", []) or []:
        inner = tile.get("Products") if isinstance(tile, dict) else None
        if isinstance(inner, list):
            flattened.extend(p for p in inner if isinstance(p, dict))
    return flattened


def _pick_best_match(
    query: str, products: list[dict[str, Any]],
) -> tuple[str | None, str, float] | None:
    """Return (image_url, product_url, score) for the best-matching product."""
    q_tokens = _tokenise(query)
    if not q_tokens:
        return None

    best: tuple[str | None, str, float] | None = None
    for p in products:
        name = p.get("Name") or ""
        if not name:
            continue
        score = _token_overlap(q_tokens, _tokenise(name))
        if score < _MIN_TOKEN_OVERLAP:
            continue

        image_url = (
            p.get("LargeImageFile")
            or p.get("MediumImageFile")
            or p.get("SmallImageFile")
        )
        slug = p.get("UrlFriendlyName") or ""
        stockcode = p.get("Stockcode")
        product_url = (
            f"https://www.woolworths.com.au/shop/productdetails/{stockcode}/{slug}"
            if stockcode and slug else ""
        )

        if best is None or score > best[2]:
            best = (image_url, product_url, score)

    return best


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tokenise(s: str) -> set[str]:
    """Lowercase + strip punctuation + split. Drops 1-char tokens."""
    s = s.lower()
    s = _NORMALISE_RE.sub(" ", s)
    s = _WHITESPACE_RE.sub(" ", s).strip()
    return {tok for tok in s.split(" ") if len(tok) > 1}


def _token_overlap(q: set[str], candidate: set[str]) -> float:
    if not q:
        return 0.0
    return len(q & candidate) / len(q)


if __name__ == "__main__":
    # Standalone smoke test:
    #   python -m src.scrapers.product_images "tim tam original 200g" coles
    import sys
    from src.scrapers.base import configure_logging

    if len(sys.argv) != 3 or sys.argv[2] not in ("coles", "woolworths"):
        print(
            'Usage: python -m src.scrapers.product_images "<product name>" <coles|woolworths>',
            file=sys.stderr,
        )
        sys.exit(2)

    log = configure_logging(verbose=True)
    session = build_image_session()
    result = lookup_image(session, sys.argv[2], sys.argv[1], log)
    print()
    print(f"retailer       = {sys.argv[2]}")
    print(f"query          = {sys.argv[1]!r}")
    print(f"image_url      = {result.image_url}")
    print(f"product_url    = {result.canonical_product_url}")
    print(f"method         = {result.method}")
    print(f"score          = {result.score:.2f}")
    print(f"error          = {result.error}")
