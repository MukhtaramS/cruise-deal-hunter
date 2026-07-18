"""Seascanner (seascanner.de) — JSON API scraper.

The site is a React app (Dreamlake platform); search results are NOT in the
HTML but come from a JSON endpoint discovered via the JS bundles:

    GET https://www.seascanner.de/api/packages/search?pageSize=N&pageNumber=N
    required header:  domain: www.seascanner.de   (else 400 "Domain is required")

Response: {"items": [...], "numberOfPages": ..., "totalNumberOfItems": ~7000}.
Each item carries ship/cruise line under cruiseSelection, ISO startDate,
numberOfNights, departurePort, and a lead price (cheapest cabin) with
superCategory INSIDE/OUTSIDE/BALCONY/SUITE. Detail pages live at
/reisen/<item code>. Verified against the live API on 2026-07-10; the fixture
tests/fixtures/seascanner.json is a real saved response.
"""

import json
import logging
from datetime import date
from decimal import Decimal

from app.scrapers.base import BaseScraper, CruiseOffer

log = logging.getLogger(__name__)

BASE_URL = "https://www.seascanner.de"
API_URL = f"{BASE_URL}/api/packages/search"
API_HEADERS = {"domain": "www.seascanner.de", "Accept": "application/json"}

CABIN_TYPES = {
    "INSIDE": "inside",
    "OUTSIDE": "outside",
    "OCEANVIEW": "outside",
    "BALCONY": "balcony",
    "SUITE": "suite",
}


class SeascannerScraper(BaseScraper):
    source = "seascanner"
    page_size = 50
    max_pages = 4  # 200 offers per run; the API exposes ~7000 total

    async def fetch(self) -> list[CruiseOffer]:
        offers: list[CruiseOffer] = []
        for page in range(self.max_pages):
            text = await self.http_get(
                API_URL,
                params={"pageSize": self.page_size, "pageNumber": page},
                headers=API_HEADERS,
            )
            page_offers = self.parse_with_fallback(text)
            if not page_offers:
                break  # past the last page (or a page we couldn't parse)
            offers.extend(page_offers)
        return offers

    def parse(self, text: str) -> list[CruiseOffer]:
        """Parse one API response (JSON text). A single malformed item is
        skipped; if ALL items fail, parse_with_fallback escalates to the LLM."""
        items = json.loads(text).get("items") or []
        offers: list[CruiseOffer] = []
        for item in items:
            try:
                offers.append(self._parse_item(item))
            except Exception as exc:
                log.warning(
                    "%s: skipping item %r: %s",
                    self.source, (item or {}).get("code"), exc,
                )
        return offers

    def _parse_item(self, item: dict) -> CruiseOffer:
        if item.get("soldOut"):
            raise ValueError("sold out")
        price = item["price"]
        if not price.get("bookable", True):
            raise ValueError("not bookable")
        cruise = item.get("cruiseSelection") or {}
        return CruiseOffer(
            source=self.source,
            cruise_line=(cruise.get("cruiseLine") or {})["name"],
            ship=(cruise.get("ship") or {})["name"],
            title=item.get("description") or item["title"],
            departure_port=item["departurePort"]["name"],
            departure_date=date.fromisoformat(item["startDate"]),
            nights=item["numberOfNights"],
            url=f"{BASE_URL}/reisen/{item['code']}",
            cabin_type=CABIN_TYPES.get(price.get("superCategory", ""), "inside"),
            price_eur=Decimal(str(price["amount"])),
        )
