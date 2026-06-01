"""Coles product-image discovery via Brave Search + deterministic CDN.

Why this exists
---------------
``product_images.py`` covers Coles products that *also* exist at Woolworths
by matching against the Woolies API (big national brands — Tim Tam, Cadbury,
Coke). It can never resolve **Coles house brands** (Coles Bakery, Farmers
Market, Curates, Red Tractor): they're sold only at Coles, so there's nothing
to cross-match. Those house brands dominate the remaining image-coverage gap.

Session 13's Playwright-against-coles.com.au approach hit Cloudflare bot
detection on sustained sessions (~1-2% hit rate) and was abandoned.

The breakthrough this module exploits: Coles product images are reachable
**without touching Coles' Cloudflare at all**.

  * A Coles product page URL is ``https://www.coles.com.au/product/<slug>-<id>``
    where ``<id>`` is the real numeric product ID.
  * The image lives at a DETERMINISTIC CDN URL:
    ``https://cdn.productimages.coles.com.au/productimages/<id[0]>/<id>.jpg``
    (verified: ``6238038 -> /6/6238038.jpg``, ``8951301 -> /8/8951301.jpg``).
    The image asset ID equals the product ID.
  * That CDN is a plain image host — NOT Cloudflare-gated like the search SPA.

So the whole problem collapses to: map each name-only Coles product to its
real product ID. We do that with a web search restricted to ``coles.com.au``
via the Brave Search API (free 2,000 queries/month). Parse the ID out of the
result URL, score the candidates by token overlap (the top hit is often the
wrong product), build the CDN URL deterministically, HEAD-verify it, persist.

Public surface:
    lookup_coles_image_brave(session, product_name, log, *, api_key) -> ImageLookupResult
    build_brave_session() -> requests.Session

Per-call cost: 1 GET to Brave's API + 1 HEAD to the CDN. Brave's free tier
caps at 1 query/sec, so the caller paces with ``time.sleep(1.1)``.
"""
from __future__ import annotations

import logging
import re

import requests

from src.scrapers.base import build_session
from src.scrapers.coles_playwright import _searchable_query
from src.scrapers.product_images import (
    _MIN_TOKEN_OVERLAP,
    ImageLookupResult,
    _token_overlap,
    _tokenise,
)

_BRAVE_SEARCH_API = "https://api.search.brave.com/res/v1/web/search"

# Matches a Coles product-page URL and captures the slug + trailing numeric
# product ID, e.g.
#   https://www.coles.com.au/product/coles-bakery-white-vienna-1-each-6238038
#   -> slug="coles-bakery-white-vienna-1-each", id="6238038"
_COLES_PRODUCT_RE = re.compile(
    r"coles\.com\.au/product/(?P<slug>[a-z0-9-]+?)-(?P<id>\d+)/?$"
)


def build_brave_session() -> requests.Session:
    """A plain retrying session — Brave is ordinary JSON-over-HTTPS."""
    return build_session()


def _cdn_image_url(pid: str) -> str:
    """Deterministic Coles CDN image URL for a numeric product ID."""
    return f"https://cdn.productimages.coles.com.au/productimages/{pid[0]}/{pid}.jpg"


def _verify_cdn_image(
    session: requests.Session, url: str, log: logging.Logger,
) -> bool:
    """True if the CDN URL is a real image (200 + image/* content-type).

    HEAD is enough for the Coles CDN (returns 200 + image/jpg). Some CDN
    edges reject HEAD with 403/405 — fall back to a 1-byte ranged GET so a
    quirky edge doesn't cause a false miss. A genuinely-missing image 404s.
    """
    resp = None
    try:
        resp = session.head(url, timeout=15, allow_redirects=True)
    except requests.RequestException as e:
        log.debug("cdn.head_error", extra={"url": url, "error": str(e)})

    if resp is None or resp.status_code in (403, 405):
        try:
            resp = session.get(
                url, headers={"Range": "bytes=0-0"}, timeout=15, allow_redirects=True
            )
        except requests.RequestException as e:
            log.debug("cdn.get_error", extra={"url": url, "error": str(e)})
            return False

    ct = resp.headers.get("Content-Type", "")
    return resp.status_code in (200, 206) and ct.startswith("image/")


def lookup_coles_image_brave(
    session: requests.Session,
    product_name: str,
    log: logging.Logger,
    *,
    api_key: str,
) -> ImageLookupResult:
    """Resolve a Coles product name to its canonical product URL via Brave.

    Never raises. On a transient Brave failure returns an ImageLookupResult
    whose ``error`` starts with ``brave_request_error`` so the caller can
    leave the row retryable instead of stamping it as a confident miss.
    """
    search_name = _searchable_query(product_name)
    if not search_name:
        return ImageLookupResult(None, None, "miss", error="empty product name")

    try:
        resp = session.get(
            _BRAVE_SEARCH_API,
            headers={
                "X-Subscription-Token": api_key,
                "Accept": "application/json",
            },
            params={"q": f"site:coles.com.au {search_name}", "count": 10},
            timeout=20,
        )
    except requests.RequestException as e:
        return ImageLookupResult(None, None, "miss", error=f"brave_request_error: {e}")

    if resp.status_code != 200:
        log.warning(
            "brave.non_200",
            extra={"status": resp.status_code, "query": search_name},
        )
        return ImageLookupResult(
            None, None, "miss",
            error=f"brave_request_error: HTTP {resp.status_code}",
        )

    try:
        payload = resp.json()
    except ValueError as e:
        return ImageLookupResult(
            None, None, "miss", error=f"brave_request_error: bad JSON ({e})"
        )

    results = (payload.get("web") or {}).get("results") or []

    q_tokens = _tokenise(search_name)
    best: tuple[str, str, float] | None = None  # (slug, id, score)
    for r in results:
        url = (r.get("url") or "").strip()
        m = _COLES_PRODUCT_RE.search(url)
        if not m:
            continue
        slug = m.group("slug")
        pid = m.group("id")
        title = (r.get("title") or "").strip()
        # Score against slug + title — the slug embeds the brand/variant
        # tokens (e.g. "coles-bakery-white-vienna") which the title may
        # truncate. Scoring is essential: the top hit is often the wrong
        # product (a "Farmers Market" query surfaced generic "Coles Carrots"
        # first in testing).
        candidate = slug.replace("-", " ") + " " + title
        score = _token_overlap(q_tokens, _tokenise(candidate))
        if score < _MIN_TOKEN_OVERLAP:
            continue
        if best is None or score > best[2]:
            best = (slug, pid, score)

    if best is None:
        return ImageLookupResult(
            None, None, "miss", error="no_coles_result_above_threshold"
        )

    slug, pid, score = best
    canonical_url = f"https://www.coles.com.au/product/{slug}-{pid}"
    image_url = _cdn_image_url(pid)
    log.info(
        "brave.match",
        extra={"query": search_name, "url": canonical_url, "score": round(score, 2)},
    )

    if not _verify_cdn_image(session, image_url, log):
        # We found the right product page but the CDN has no image at the
        # deterministic path (rare). Treat as a confident miss with the
        # canonical URL preserved for debugging.
        log.info("cdn.miss", extra={"image_url": image_url})
        return ImageLookupResult(
            None, canonical_url, "miss", score=score, error="cdn_image_not_found"
        )

    return ImageLookupResult(
        image_url=image_url,
        canonical_product_url=canonical_url,
        method="coles_brave_cdn",
        score=score,
    )


if __name__ == "__main__":
    # Standalone smoke test:
    #   python -m src.scrapers.coles_brave "coles bakery white vienna 1 each"
    # Requires BRAVE_SEARCH_API_KEY in the environment (or .env).
    import os
    import sys

    from src.scrapers.base import configure_logging

    if len(sys.argv) != 2:
        print(
            'Usage: python -m src.scrapers.coles_brave "<product name>"',
            file=sys.stderr,
        )
        sys.exit(2)

    key = os.environ.get("BRAVE_SEARCH_API_KEY")
    if not key:
        # Best-effort .env load so the smoke test works without exporting.
        import pathlib

        env_path = pathlib.Path(__file__).resolve().parents[2] / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line.startswith("BRAVE_SEARCH_API_KEY=") and "=" in line:
                    key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not key:
        print("BRAVE_SEARCH_API_KEY not set — sign up at brave.com/search/api", file=sys.stderr)
        sys.exit(1)

    log = configure_logging(verbose=True)
    session = build_brave_session()
    result = lookup_coles_image_brave(session, sys.argv[1], log, api_key=key)
    print()
    print(f"query          = {sys.argv[1]!r}")
    print(f"image_url      = {result.image_url}")
    print(f"product_url    = {result.canonical_product_url}")
    print(f"method         = {result.method}")
    print(f"score          = {result.score:.2f}")
    print(f"error          = {result.error}")
