"""Dreamlines (dreamlines.de) — server-rendered search results.

⚠️ SELECTORS NOT YET VERIFIED AGAINST THE REAL SITE. The fixture in
tests/fixtures/dreamlines.html is synthetic (modeled on a typical German SSR
cruise listing) because no real page save was available when this was built —
dreamlines.de sits behind Cloudflare, which 403s plain HTTP clients. Before
first production run: save a real results page (browser: View Source → save
to samples/dreamlines.html), update the selectors in `_parse_card`, and
replace the fixture. If Cloudflare also blocks httpx at runtime, this portal
is a Playwright candidate (see the `browser` extra).

The German-format helpers (price/date/nights) are real and tested.
"""

import logging
import re
from datetime import date
from decimal import Decimal
from urllib.parse import urljoin

from selectolax.parser import HTMLParser, Node

from app.scrapers.base import BaseScraper, CruiseOffer

log = logging.getLogger(__name__)

BASE_URL = "https://www.dreamlines.de"
SEARCH_URL = f"{BASE_URL}/kreuzfahrten"

GERMAN_MONTHS = {
    "jan": 1, "feb": 2, "mär": 3, "mrz": 3, "apr": 4, "mai": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "okt": 10, "nov": 11, "dez": 12,
}


def parse_german_price(text: str) -> Decimal:
    """'ab 1.499 €' -> 1499; '899,00 €' -> 899.00; '549 €' -> 549"""
    m = re.search(r"(\d{1,3}(?:\.\d{3})+|\d+)(,\d{2})?\s*€", text)
    if not m:
        raise ValueError(f"no price in {text!r}")
    whole = m.group(1).replace(".", "")
    cents = (m.group(2) or "").replace(",", ".")
    return Decimal(whole + cents)


def parse_german_date(text: str) -> date:
    """'12.09.2026' or '14. Aug. 2026'"""
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
    if m:
        day, month, year = (int(g) for g in m.groups())
        return date(year, month, day)
    m = re.search(r"(\d{1,2})\.?\s+([A-Za-zÄÖÜäöü]{3,})\.?\s+(\d{4})", text)
    if m:
        month = GERMAN_MONTHS.get(m.group(2)[:3].lower())
        if month:
            return date(int(m.group(3)), month, int(m.group(1)))
    raise ValueError(f"no date in {text!r}")


def parse_nights(text: str) -> int:
    m = re.search(r"(\d+)\s*(?:Nächte|Nacht)", text)
    if not m:
        raise ValueError(f"no nights in {text!r}")
    return int(m.group(1))


class DreamlinesScraper(BaseScraper):
    source = "dreamlines"

    async def fetch(self) -> list[CruiseOffer]:
        html = await self.http_get(SEARCH_URL)
        return self.parse_with_fallback(html)

    def parse(self, html: str) -> list[CruiseOffer]:
        tree = HTMLParser(html)
        offers: list[CruiseOffer] = []
        for card in tree.css("article.cruise-card"):
            try:
                offers.append(self._parse_card(card))
            except Exception as exc:
                # one broken card shouldn't kill the page; if ALL cards fail,
                # parse_with_fallback sees 0 offers and calls the LLM
                log.warning("%s: skipping card: %s", self.source, exc)
        return offers

    def _parse_card(self, card: Node) -> CruiseOffer:
        def text(selector: str) -> str:
            node = card.css_first(selector)
            if node is None:
                raise ValueError(f"missing {selector!r}")
            return node.text(strip=True)

        link = card.css_first("a.cruise-card__link")
        if link is None or not link.attributes.get("href"):
            raise ValueError("missing link")

        # "ab Barcelona" / "ab/bis Palma de Mallorca" -> port only
        port = re.sub(r"^ab(?:/bis)?\s+", "", text(".cruise-card__port"))

        return CruiseOffer(
            source=self.source,
            cruise_line=text(".cruise-card__line"),
            ship=text(".cruise-card__ship"),
            title=text(".cruise-card__title"),
            departure_port=port,
            departure_date=parse_german_date(text(".cruise-card__date")),
            nights=parse_nights(text(".cruise-card__nights")),
            url=urljoin(BASE_URL, link.attributes["href"]),
            cabin_type="inside",  # listing shows the cheapest ("ab") inside price
            price_eur=parse_german_price(text(".cruise-card__price")),
        )
