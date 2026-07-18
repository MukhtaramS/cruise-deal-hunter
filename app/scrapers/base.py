"""Scraper contract for all German cruise portals.

Parsing strategy, in order of preference:
1. CSS selectors via selectolax (fast, free) — implement `parse(html)` and
   route it through `parse_with_fallback` in `fetch()`.
2. If selectors break (crash, or 0 offers from a non-empty page),
   `parse_with_fallback` calls `app.llm.extract_offers` (Groq).
3. Playwright only if the portal is unusable without JS — install via the
   `browser` extra and keep it out of the default path.

Built into `http_get` for every scraper:
- rate limit: 1 request per `min_request_interval` seconds (default 3)
- realistic browser headers (DEFAULT_HEADERS)
- retry with exponential backoff on 429 / 5xx / transport errors,
  honoring Retry-After when present
"""

import asyncio
import hashlib
import logging
import time
from abc import ABC, abstractmethod
from datetime import date
from decimal import Decimal
from typing import ClassVar

import httpx
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept-Language": "de-DE,de;q=0.9",
}

# pages shorter than this are treated as genuinely empty (no LLM fallback)
MIN_MEANINGFUL_HTML = 500


class CruiseOffer(BaseModel):
    """Normalized offer as returned by every scraper."""

    source: str  # portal slug, must match the scraper's `source`
    cruise_line: str
    ship: str
    title: str
    departure_port: str
    departure_date: date
    nights: int = Field(gt=0)
    url: str
    cabin_type: str = "inside"  # inside | outside | balcony | suite
    price_eur: Decimal = Field(gt=0)

    @property
    def price_per_night(self) -> Decimal:
        return self.price_eur / self.nights

    @property
    def route_hash(self) -> str:
        """Portal-independent identity of the physical cruise. Two offers with
        the same hash are the same sailing listed on different portals."""
        key = "|".join(
            [
                self.cruise_line.strip().lower(),
                self.ship.strip().lower(),
                self.departure_port.strip().lower(),
                self.departure_date.isoformat(),
                str(self.nights),
            ]
        )
        return hashlib.sha1(key.encode()).hexdigest()


class BaseScraper(ABC):
    """Subclass per portal. Set `source`, implement `fetch()`; server-rendered
    portals also implement `parse(html)` so the CLI's --file mode and the LLM
    fallback work. Register the subclass in app/scrapers/__init__.py:SCRAPERS.
    """

    source: ClassVar[str]
    min_request_interval: ClassVar[float] = 3.0  # seconds between requests
    max_retries: ClassVar[int] = 3  # on 429 / 5xx / transport errors
    backoff_base: ClassVar[float] = 5.0  # 5s, 10s, 20s

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        # transport is injectable for tests (httpx.MockTransport)
        self._transport = transport
        self._last_request_at: float | None = None

    @abstractmethod
    async def fetch(self) -> list[CruiseOffer]:
        """Scrape the portal and return all current offers."""

    def parse(self, html: str) -> list[CruiseOffer]:
        """CSS-selector parse of a results page. Override in scrapers for
        server-rendered portals; JSON-API scrapers may leave it unimplemented."""
        raise NotImplementedError(f"{type(self).__name__} does not parse HTML")

    def parse_with_fallback(self, html: str) -> list[CruiseOffer]:
        """`parse`, falling back to the Groq extractor when selectors crash or
        yield 0 offers from a page that clearly isn't empty."""
        try:
            offers = self.parse(html)
        except Exception:
            log.exception("%s: CSS parse crashed — trying LLM fallback", self.source)
            offers = []
        if offers:
            return offers
        if len(html.strip()) < MIN_MEANINGFUL_HTML:
            return []
        log.warning(
            "%s: 0 offers from non-empty HTML — trying LLM fallback", self.source
        )
        from app.llm import extract_offers  # lazy: app.llm imports CruiseOffer from here

        return extract_offers(html, self.source)

    async def _throttle(self) -> None:
        if self._last_request_at is not None:
            wait = self.min_request_interval - (time.monotonic() - self._last_request_at)
            if wait > 0:
                await asyncio.sleep(wait)
        self._last_request_at = time.monotonic()

    async def http_get(self, url: str, **kwargs) -> str:
        for attempt in range(self.max_retries + 1):
            await self._throttle()
            try:
                async with httpx.AsyncClient(
                    headers=DEFAULT_HEADERS,
                    timeout=30,
                    follow_redirects=True,
                    transport=self._transport,
                ) as client:
                    resp = await client.get(url, **kwargs)
            except httpx.TransportError as exc:
                if attempt >= self.max_retries:
                    raise
                delay = self.backoff_base * 2**attempt
                log.warning(
                    "%s: %s on %s — retry %d/%d in %.0fs",
                    self.source, type(exc).__name__, url,
                    attempt + 1, self.max_retries, delay,
                )
                await asyncio.sleep(delay)
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt >= self.max_retries:
                    resp.raise_for_status()
                delay = self.backoff_base * 2**attempt
                retry_after = resp.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    delay = max(delay, float(retry_after))
                log.warning(
                    "%s: HTTP %d on %s — retry %d/%d in %.0fs",
                    self.source, resp.status_code, url,
                    attempt + 1, self.max_retries, delay,
                )
                await asyncio.sleep(delay)
                continue

            resp.raise_for_status()
            return resp.text
        raise AssertionError("unreachable")
