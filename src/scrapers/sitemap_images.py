"""Product-image discovery via a retailer's public product sitemap + its
deterministic image CDN. Card-free, key-free, quota-free. Covers both Coles
and Woolworths.

Why this shape
--------------
``product_images.py`` (the Woolies internal-API path) resolves big national
brands sold at both retailers, but misses **house brands** and any product its
name-search can't match. Session 13's Playwright-against-the-SPA hit Cloudflare;
Brave Search now needs a card; DuckDuckGo hard-blocks scrapers.

The winning, card-free path: **both retailers publish their full catalogue as a
static XML sitemap** (listed for crawlers in robots.txt). Each entry embeds the
real numeric product ID, and each retailer serves images from a DETERMINISTIC
CDN keyed by that ID:

  * Coles      ``.../product/<slug>-<id>``
                 -> ``cdn.productimages.coles.com.au/productimages/<id[0]>/<id>.jpg``
                 (verified: 6238038 -> /6/6238038.jpg)
  * Woolworths ``.../shop/productdetails/<id>/<slug>``
                 -> ``cdn0.woolworths.media/content/wowproductimages/large/<id>.jpg``

We download the sitemap ONCE, build a local slug-token index, and match each
name-only product against it with token-overlap scoring + a brand anchor. The
matched ID builds the CDN URL, which we HEAD-verify on the (ungated) image host.

Cloudflare note: the sitemap host is behind Cloudflare, which allows a short
burst (~5 requests) before rate-challenging with a multi-minute cooldown. A
full build needs only ~3-4 requests (index + product children) and the per-
image CDN HEADs hit a different, ungated host, so a single spaced build per run
stays under the limit. ``build_product_index`` detects the HTML challenge (no
``<loc>`` in the body), backs off, and raises SitemapChallenged on persistent
failure so the caller can skip + retry later (a missed run is harmless).

Public surface:
    build_product_index(session, retailer, log) -> ProductIndex
    match_image(index, session, retailer, product_name, log) -> ImageLookupResult
    build_sitemap_session() -> requests.Session
    RETAILERS  (the supported retailer keys)
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Callable

import requests

from src.scrapers.coles_playwright import _searchable_query
from src.scrapers.product_images import (
    _MIN_TOKEN_OVERLAP,
    ImageLookupResult,
    _token_overlap,
    _tokenise,
)

_LOC_RE = re.compile(r"<loc>(.*?)</loc>")

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

_REQUEST_SPACING_S = 3.0
_CHALLENGE_BACKOFFS_S = (15, 45, 90)


@dataclass(frozen=True)
class _RetailerSitemap:
    index_url: str
    # Named groups <id> and <slug>; order varies by retailer.
    url_re: re.Pattern[str]
    cdn_url: Callable[[str], str]
    product_url: Callable[[str, str], str]  # (slug, id) -> canonical URL
    method: str


RETAILER_CONFIG: dict[str, _RetailerSitemap] = {
    "coles": _RetailerSitemap(
        index_url="https://www.coles.com.au/sitemap/sitemap-index-products.xml",
        url_re=re.compile(r"coles\.com\.au/product/(?P<slug>[a-z0-9-]+?)-(?P<id>\d+)/?$"),
        cdn_url=lambda pid: f"https://cdn.productimages.coles.com.au/productimages/{pid[0]}/{pid}.jpg",
        product_url=lambda slug, pid: f"https://www.coles.com.au/product/{slug}-{pid}",
        method="coles_sitemap_cdn",
    ),
    "woolworths": _RetailerSitemap(
        index_url="https://www.woolworths.com.au/sitemap_index.xml",
        url_re=re.compile(r"woolworths\.com\.au/shop/productdetails/(?P<id>\d+)/(?P<slug>[a-z0-9-]+)/?$"),
        cdn_url=lambda pid: f"https://cdn0.woolworths.media/content/wowproductimages/large/{pid}.jpg",
        product_url=lambda slug, pid: f"https://www.woolworths.com.au/shop/productdetails/{pid}/{slug}",
        method="woolworths_sitemap_cdn",
    ),
}

RETAILERS = tuple(RETAILER_CONFIG)


def build_sitemap_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(_BROWSER_HEADERS)
    return s


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
            log.info("sitemap.challenge_backoff seconds=%d url=%s", wait, url)
            time.sleep(wait)
        resp = session.get(url, timeout=60)
        last_len = len(resp.text)
        if resp.status_code == 200 and "<loc>" in resp.text:
            return resp.text
    raise SitemapChallenged(f"persistent Cloudflare challenge for {url} (last_len={last_len})")


class ProductIndex:
    """In-memory index of (id, slug, slug_tokens) for a retailer's catalogue."""

    def __init__(self, entries: list[tuple[str, str, frozenset[str]]]):
        self._entries = entries

    def __len__(self) -> int:
        return len(self._entries)

    def best_match(self, product_name: str) -> tuple[str, str, float] | None:
        """Return (id, slug, score) for the best catalogue match, or None.

        Token overlap of the (normalised) query against each slug, gated at
        _MIN_TOKEN_OVERLAP. A brand anchor rejects same-category wrong-brand
        decoys (a "Nescafe coffee sachets" query must not match a "moccona
        coffee sachets" slug): the candidate's lead (brand) token must appear
        in the query, or the query's lead token in the candidate. Ties break
        toward the candidate whose token count is closest to the query's.
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


def build_product_index(
    session: requests.Session, retailer: str, log: logging.Logger,
) -> ProductIndex:
    """Download a retailer's product sitemap and build the match index.

    Raises SitemapChallenged if Cloudflare won't serve the sitemap, or
    ValueError for an unknown retailer.
    """
    cfg = RETAILER_CONFIG.get(retailer)
    if cfg is None:
        raise ValueError(f"unknown retailer: {retailer!r}")

    t0 = time.monotonic()
    idx_xml = _fetch_sitemap(session, cfg.index_url, log)
    # Only the product child sitemaps (skip recipes/stores/etc.).
    children = [c for c in _LOC_RE.findall(idx_xml) if "product" in c.lower()]
    entries: list[tuple[str, str, frozenset[str]]] = []
    for child in children:
        time.sleep(_REQUEST_SPACING_S)
        xml = _fetch_sitemap(session, child, log)
        for url in _LOC_RE.findall(xml):
            m = cfg.url_re.search(url)
            if not m:
                continue
            slug, pid = m.group("slug"), m.group("id")
            entries.append((pid, slug, frozenset(_tokenise(slug.replace("-", " ")))))
    log.info(
        "sitemap.index_built retailer=%s products=%d children=%d elapsed_s=%.1f",
        retailer, len(entries), len(children), time.monotonic() - t0,
    )
    return ProductIndex(entries)


def _verify_cdn_image(
    session: requests.Session, url: str, log: logging.Logger,
) -> bool:
    """True if the CDN URL is a real image (200/206 + image/* content-type)."""
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


def match_image(
    index: ProductIndex,
    session: requests.Session,
    retailer: str,
    product_name: str,
    log: logging.Logger,
) -> ImageLookupResult:
    """Match a product name against the sitemap index + verify its CDN image.

    Never raises. Returns method="<retailer>_sitemap_cdn" on a verified hit,
    else a miss (image discovery is best-effort).
    """
    cfg = RETAILER_CONFIG[retailer]
    if not product_name.strip():
        return ImageLookupResult(None, None, "miss", error="empty product name")

    best = index.best_match(product_name)
    if best is None:
        return ImageLookupResult(None, None, "miss", error="no_sitemap_match")

    pid, slug, score = best
    canonical_url = cfg.product_url(slug, pid)
    image_url = cfg.cdn_url(pid)
    if not _verify_cdn_image(session, image_url, log):
        log.info("sitemap.cdn_miss retailer=%s image_url=%s", retailer, image_url)
        return ImageLookupResult(
            None, canonical_url, "miss", score=score, error="cdn_image_not_found"
        )
    return ImageLookupResult(
        image_url=image_url,
        canonical_product_url=canonical_url,
        method=cfg.method,
        score=score,
    )


if __name__ == "__main__":
    # Standalone smoke test (no key needed):
    #   python -m src.scrapers.sitemap_images coles "coles bakery white vienna 1 each"
    #   python -m src.scrapers.sitemap_images woolworths "tim tam original 200g"
    import sys

    from src.scrapers.base import configure_logging

    if len(sys.argv) != 3 or sys.argv[1] not in RETAILERS:
        print(f'Usage: python -m src.scrapers.sitemap_images <{"|".join(RETAILERS)}> "<product name>"',
              file=sys.stderr)
        sys.exit(2)

    retailer, name = sys.argv[1], sys.argv[2]
    log = configure_logging(verbose=True)
    session = build_sitemap_session()
    try:
        index = build_product_index(session, retailer, log)
    except SitemapChallenged as e:
        print(f"sitemap unavailable (Cloudflare challenge): {e}", file=sys.stderr)
        sys.exit(1)
    result = match_image(index, session, retailer, name, log)
    print()
    print(f"retailer     = {retailer}")
    print(f"query        = {name!r}")
    print(f"image_url    = {result.image_url}")
    print(f"product_url  = {result.canonical_product_url}")
    print(f"method       = {result.method}")
    print(f"score        = {result.score:.2f}")
    print(f"error        = {result.error}")
