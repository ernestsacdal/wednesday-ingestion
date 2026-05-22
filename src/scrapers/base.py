"""Shared HTTP and logging utilities for scrapers.

A polite session with retry-on-transient-errors, identifying User-Agent,
and structured logging. Reuse across all scrapers so behaviour is consistent
and easy to change in one place.
"""
from __future__ import annotations

import logging
import time
from typing import Iterator

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

USER_AGENT = (
    "wednesday-ingestion/0.1 "
    "(+https://wednesday.com.au; non-commercial, free forever; contact: dev)"
)


def build_session(*, total_retries: int = 4, backoff_factor: float = 0.5) -> requests.Session:
    """Session with exponential-backoff retries on transient HTTP errors.

    Retries: 502, 503, 504 (server errors that benefit from a wait).
    Backoff: 0.5s, 1s, 2s, 4s (urllib3 default formula).
    """
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-AU,en;q=0.9",
    })
    retry = Retry(
        total=total_retries,
        backoff_factor=backoff_factor,
        status_forcelist=(502, 503, 504),
        allowed_methods=("GET", "POST"),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def polite_get(
    session: requests.Session,
    url: str,
    *,
    log: logging.Logger,
    min_delay_seconds: float = 1.0,
    timeout: float = 20.0,
) -> requests.Response:
    """GET with a polite minimum delay + structured logging."""
    log.info("http.get", extra={"url": url})
    t0 = time.monotonic()
    resp = session.get(url, timeout=timeout)
    dt = time.monotonic() - t0
    log.info(
        "http.response",
        extra={"url": url, "status": resp.status_code, "elapsed_s": round(dt, 2), "bytes": len(resp.content)},
    )
    resp.raise_for_status()
    time.sleep(min_delay_seconds)
    return resp


def configure_logging(verbose: bool = False) -> logging.Logger:
    """Stdlib logging configured for both human and structured-ish output."""
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s %(levelname)-7s %(name)-30s %(message)s"
    logging.basicConfig(level=level, format=fmt, datefmt="%H:%M:%S")
    # Quiet down third-party noise.
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    return logging.getLogger("wednesday")


def batched[T](items: list[T], size: int) -> Iterator[list[T]]:
    """Yield successive size-chunks from items."""
    for i in range(0, len(items), size):
        yield items[i:i + size]
