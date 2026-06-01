"""Coles product-image discovery via the public product sitemap + the
deterministic image CDN. Card-free, key-free, quota-free.

Why this shape
--------------
``product_images.py`` resolves Coles products that also exist at Woolworths
(big national brands) by cross-matching the Woolies API. It can't touch Coles
**house brands** (Coles Bakery, Farmers Market, Curates, Red Tractor) — sold
only at Coles — which dominate the remaining image-coverage gap.

Two earlier attempts were rejected:
  * Session 13 Playwright against coles.com.au's search SPA -> Cloudflare
    bot-detection killed sustained sessions (~1-2% hit rate).
  * A Brave / DuckDuckGo web-search channel -> Brave now needs a card on file;
    DuckDuckGo's HTML endpoint hard-blocks scrapers with a 202 JS challenge.

The winning, card-free path: **Coles publishes its full product catalogue as
a static XML sitemap** (listed for crawlers in robots.txt; ``Allow:
/product/*-*``). Each entry is ``https://www.coles.com.au/product/<slug>-<id>``
where ``<id>`` is the real numeric product ID. We download the sitemap ONCE,
build a local slug-token index, and match each of our name-only Coles products
against it with token-overlap scoring + a brand anchor. The matched ID yields
the image via the deterministic CDN
``https://cdn.productimages.coles.com.au/productimages/<id[0]>/<id>.jpg``
(verified: 6238038 -> /6/6238038.jpg) — a plain image host, NOT Cloudflare-
gated.

Cloudflare note: the sitemap host *is* behind the same Cloudflare as the
search SPA, but it allows a short burst before rate-challenging. Since a full
catalogue build needs only ~3 requests (index + 2 children) and the per-image
CDN HEADs hit a different, ungated host, a single spaced build per run stays
comfortably under the limit. ``build_coles_product_index`` detects the HTML
challenge (no ``<loc>`` in the body) and backs off; on persistent challenge it
raises so the caller can skip + retry later (a missed run is harmless — the
mobile app falls back to the retailer pill).

Public surface:
    build_coles_product_index(session, log) -> ColesProductIndex
    match_coles_image(index, session, product_name, log) -> ImageLookupResult
    build_sitemap_session() -> requests.Session
"""
from __future__ import annotations

import logging
import re
import time

import requests

from src.scrapers.coles_playwright import _searchable_query
from src.scrapers.product_images import (
    _MIN_TOKEN_OVERLAP,
    ImageLookupResult,
    _token_overlap,
    _tokenise,
)

_SITEMAP_INDEX = "https://www.coles.com.au/sitemap/sitemap-index-products.xml"
_LOC_RE = re.compile(r"<loc>(.*?)</loc>")
# Coles product URL: .../product/<slug>-<numeric-id>
_COLES_PRODUCT_RE = re.compile(r"coles\.com\.au/product/([a-z0-9-]+?)-(\d+)/?$")

# Browser-complete headers — Cloudflare challenges bare requests UAs harder.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "sec-ch-ua": '"Chromium";v="120", "Not(A:Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
}

# Spacing between the ~3 sitemap requests, and backoff when challenged.
_REQUEST_SPACING_S = 3.0
_CHALLENGE_BACKOFFS_S = (15, 45, 90)


def build_sitemap_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_BROWSER_HEADERS)
    return s


def _cdn_image_url(pid: str) -> str:
    """Deterministic Coles CDN image URL for a numeric product ID."""
    return f"https://cdn.productimages.coles.com.au/productimages/{pid[0]}/{pid}.jpg"


class SitemapChallenged(RuntimeError):
    """Raised when Cloudflare persistently challenges the sitemap fetch."""


def _fetch_sitemap(session: requests.Session, url: str, log: logging.Logger) -> str:
    """GET a sitemap document, retrying through Cloudflare HTML challenges.

    A challenge response is a ~6KB text/html interstitial with no <loc>.
    Raises SitemapChallenged if every attempt is challenged.
    """
    last_len = 0
    for wait in (0, *_CHALLENGE_BACKOFFS_S):
        if wait:
            log.info("coles_sitemap.challenge_backoff seconds=%d url=%s", wait, url)
            time.sleep(wait)
        resp = session.get(url, timeout=60)
        last_len = len(resp.text)
        if resp.status_code == 200 and "<loc>" in resp.text:
            return resp.text
    raise SitemapChallenged(f"persistent Cloudflare challenge for {url} (last_len={last_len})")


class ColesProductIndex:
    """In-memory index of (id, slug, slug_tokens) for the whole Coles catalogue."""

    def __init__(self, entries: list[tuple[str, str, frozenset[str]]]):
        self._entries = entries

    def __len__(self) -> int:
        return len(self._entries)

    def best_match(self, product_name: str) -> tuple[str, str, float] | None:
        """Return (id, slug, score) for the best catalogue match, or None.

        Scoring: token overlap of the (normalised) query against each slug,
        gated at _MIN_TOKEN_OVERLAP. A brand anchor rejects same-category
        wrong-brand decoys (a "Nescafe coffee sachets" query must not match a
        "moccona coffee sachets" slug): the candidate's lead (brand) token must
        appear in the query, or the query's lead token in the candidate. Ties
        break toward the candidate whose token count is closest to the query's.
        """
        cleaned = _searchable_query(product_name)
        q = _tokenise(cleaned)
        if not q:
            return None
        q_words = [w for w in re.sub(r"[^a-z0-9\s]+", " ", cleaned.lower()).split() if len(w) > 1]
        q_first = q_words[0] if q_words else None

        best_key: tuple[float, int] | None = None
        best: tuple[str, str, float] | None = None
        for pid, slug, toks in self._entries:
            score = _token_overlap(q, toks)
            if score < _MIN_TOKEN_OVERLAP:
                continue
            slug_first = slug.split("-", 1)[0]
            if slug_first not in q and (q_first is None or q_first not in toks):
                continue
            key = (score, -abs(len(toks) - len(q)))
            if best_key is None or key > best_key:
                best_key = key
                best = (pid, slug, score)
        return best


def build_coles_product_index(
    session: requests.Session, log: logging.Logger,
) -> ColesProductIndex:
    """Download the Coles product sitemap (~3 requests) and build the index.

    Raises SitemapChallenged if Cloudflare won't serve the sitemap.
    """
    t0 = time.monotonic()
    idx_xml = _fetch_sitemap(session, _SITEMAP_INDEX, log)
    children = _LOC_RE.findall(idx_xml)
    entries: list[tuple[str, str, frozenset[str]]] = []
    for child in children:
        time.sleep(_REQUEST_SPACING_S)
        xml = _fetch_sitemap(session, child, log)
        for url in _LOC_RE.findall(xml):
            m = _COLES_PRODUCT_RE.search(url)
            if not m:
                continue
            slug, pid = m.group(1), m.group(2)
            entries.append((pid, slug, frozenset(_tokenise(slug.replace("-", " ")))))
    log.info(
        "coles_sitemap.index_built products=%d children=%d elapsed_s=%.1f",
        len(entries), len(children), time.monotonic() - t0,
    )
    return ColesProductIndex(entries)


def _verify_cdn_image(
    session: requests.Session, url: str, log: logging.Logger,
) -> bool:
    """True if the CDN URL is a real image (200 + image/* content-type).

    HEAD suffices for the Coles CDN; a 1-byte ranged GET is the fallback for
    edges that reject HEAD. A genuinely-missing image 404s.
    """
    resp = None
    try:
        resp = session.head(url, timeout=15, allow_redirects=True)
    except requests.RequestException as e:
        log.debug("cdn.head_error url=%s err=%s", url, e)
    if resp is None or resp.status_code in (403, 405):
        try:
            resp = session.get(
                url, headers={"Range": "bytes=0-0"}, timeout=15, allow_redirects=True
            )
        except requests.RequestException as e:
            log.debug("cdn.get_error url=%s err=%s", url, e)
            return False
    ct = resp.headers.get("Content-Type", "")
    return resp.status_code in (200, 206) and ct.startswith("image/")


def match_coles_image(
    index: ColesProductIndex,
    session: requests.Session,
    product_name: str,
    log: logging.Logger,
) -> ImageLookupResult:
    """Match a Coles product name against the sitemap index + verify its image.

    Never raises. Returns method="coles_sitemap_cdn" on a verified hit, else a
    miss (image discovery for a Coles row is best-effort).
    """
    if not product_name.strip():
        return ImageLookupResult(None, None, "miss", error="empty product name")

    best = index.best_match(product_name)
    if best is None:
        return ImageLookupResult(None, None, "miss", error="no_sitemap_match")

    pid, slug, score = best
    canonical_url = f"https://www.coles.com.au/product/{slug}-{pid}"
    image_url = _cdn_image_url(pid)
    if not _verify_cdn_image(session, image_url, log):
        log.info("coles_sitemap.cdn_miss image_url=%s", image_url)
        return ImageLookupResult(
            None, canonical_url, "miss", score=score, error="cdn_image_not_found"
        )
    return ImageLookupResult(
        image_url=image_url,
        canonical_product_url=canonical_url,
        method="coles_sitemap_cdn",
        score=score,
    )


if __name__ == "__main__":
    # Standalone smoke test (no key needed):
    #   python -m src.scrapers.coles_sitemap "coles bakery white vienna 1 each"
    import sys

    from src.scrapers.base import configure_logging

    if len(sys.argv) != 2:
        print('Usage: python -m src.scrapers.coles_sitemap "<product name>"', file=sys.stderr)
        sys.exit(2)

    log = configure_logging(verbose=True)
    session = build_sitemap_session()
    try:
        index = build_coles_product_index(session, log)
    except SitemapChallenged as e:
        print(f"sitemap unavailable (Cloudflare challenge): {e}", file=sys.stderr)
        sys.exit(1)
    result = match_coles_image(index, session, sys.argv[1], log)
    print()
    print(f"query        = {sys.argv[1]!r}")
    print(f"image_url    = {result.image_url}")
    print(f"product_url  = {result.canonical_product_url}")
    print(f"method       = {result.method}")
    print(f"score        = {result.score:.2f}")
    print(f"error        = {result.error}")
