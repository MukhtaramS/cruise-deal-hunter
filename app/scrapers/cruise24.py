"""cruise24.de — server-rendered HTML, jQuery-era site, no API.

No framework, no iframe, no JSON endpoint in the JS (main.min.js is just
jQuery + owl-carousel; search-url-generator.js only builds path-segment
filter URLs client-side). Results are baked into the listing page:

    GET https://www.cruise24.de/kreuzfahrt/sort-price          # offset 0
    GET https://www.cruise24.de/kreuzfahrt/sort-price/50       # offset 50
    GET https://www.cruise24.de/kreuzfahrt/sort-price/100      # ...

Pagination is a trailing OFFSET segment (not a page number): 50 cards per
page, "Seite: 1 von 193" (~9.6k offers) at recon time. `sort-price` sorts
ascending — cheapest first, ideal for deal hunting. Verified live
2026-07-18 (offset 0 and 50 return disjoint card sets); raw save in
samples/cruise24.de.html.

This is the FIRST scraper that emits multiple cabin types per cruise:
each card carries a real cabin table (Innen/Außen/Balkon/Suite ...), so one
card can yield up to four CruiseOffers sharing the same URL but differing in
cabin_type + price. The pipeline supports this natively — store_snapshots
upserts the cruise once by (source, url) and writes one snapshot per offer,
and the detector medians per (cruise, cabin_type).

Quirks (all confirmed on real pages):
- Cabin rows beyond our four types are SKIPPED: "Yacht Club" (MSC's
  suite-class enclave; mapping it to "suite" would collide with the real
  Suite row in the same scrape) and crucially "Kind" — a child supplement
  price (e.g. 22.50€ for 3 nights) that would falsely trigger the
  <60€/night hot-deal rule if it ever became an offer.
- Prices use DOT decimals with no thousands separator even at 5 digits
  ("21995.00€") — unusual for a German site, needs its own parser.
- "ausgebucht" rows and "ab 0.00€" rows are skipped; a card whose entire
  cabin table is sold out (a real example is in the fixture) yields no
  offers but doesn't kill the page.
- The card's headline `big_price` is redundant with the cheapest cabin row
  and is ignored (on the all-sold-out card it even shows a stale price).
- Departure port = first stop of `div.rout`, where port and country are
  separated by a TAB ("Miami\\t USA" → "Miami").
- Cruise line has no text field; it's the 2nd path segment of the detail
  link (`/details/137502/MSC-Cruises/...` → "MSC Cruises"). Hyphens become
  spaces, so a genuinely hyphenated brand (e.g. A-ROSA) would lose its
  hyphen — same artifact class as kreuzfahrten.de's logo-filename
  derivation.
"""

import logging
import re
from datetime import date
from decimal import Decimal

from selectolax.parser import HTMLParser, Node

from app.scrapers.base import BaseScraper, CruiseOffer
from app.scrapers.dreamlines import parse_nights

log = logging.getLogger(__name__)

BASE_URL = "https://www.cruise24.de"
SEARCH_URL = f"{BASE_URL}/kreuzfahrt/sort-price"
PAGE_SIZE = 50  # fixed by the site; the trailing URL segment is an offset

CABIN_TYPES = {
    "innen": "inside",
    "außen": "outside",
    "aussen": "outside",
    "balkon": "balcony",
    "suite": "suite",
    # deliberately unmapped: "yacht club" (would collide with suite),
    # "kind" (child supplement, not a cabin — see module docstring)
}


def parse_price(text: str) -> Decimal:
    """'ab 154.00€' -> 154.00; '21995.00€' -> 21995.00 (dot decimal, no
    thousands separator). Raises on 'ausgebucht' and on 0.00."""
    m = re.search(r"(\d+(?:\.\d{1,2})?)\s*€", text)
    if not m:
        raise ValueError(f"no price in {text!r}")
    price = Decimal(m.group(1))
    if price <= 0:
        raise ValueError(f"non-positive price in {text!r}")
    return price


def cruise_line_from_detail_link(href: str) -> str:
    """'/details/137502/MSC-Cruises/MSC-World-Europa/...' -> 'MSC Cruises'"""
    parts = href.strip("/").split("/")
    if len(parts) < 3 or parts[0] != "details":
        raise ValueError(f"unexpected detail link {href!r}")
    name = parts[2].replace("-", " ").strip()
    if not name:
        raise ValueError(f"empty cruise line in {href!r}")
    return name


class Cruise24Scraper(BaseScraper):
    source = "cruise24"
    max_pages = 2  # 2 × 50 cards; up to 4 cabin offers per card

    async def fetch(self) -> list[CruiseOffer]:
        offers: list[CruiseOffer] = []
        for page in range(self.max_pages):
            offset = page * PAGE_SIZE
            url = SEARCH_URL if offset == 0 else f"{SEARCH_URL}/{offset}"
            html = await self.http_get(url)
            page_offers = self.parse_with_fallback(html)
            if not page_offers:
                break  # past the last page (or a page we couldn't parse)
            offers.extend(page_offers)
        return offers

    def parse(self, html: str) -> list[CruiseOffer]:
        tree = HTMLParser(html)
        offers: list[CruiseOffer] = []
        for card in tree.css("div.list-teaser"):
            try:
                offers.extend(self._parse_card(card))
            except Exception as exc:
                log.warning("%s: skipping card: %s", self.source, exc)
        return offers

    def _parse_card(self, card: Node) -> list[CruiseOffer]:
        link = card.css_first("div.headlineboxleft a")
        if link is None or not link.attributes.get("href"):
            raise ValueError("missing detail link")
        href = link.attributes["href"]
        title = re.sub(r"\s+", " ", link.text(strip=True))

        ship_node = card.css_first("div.cruiselinerrow span")
        if ship_node is None:
            raise ValueError("missing ship")
        ship = re.sub(r"\s+", " ", ship_node.text(strip=True))

        time_node = card.css_first("div.date time")
        if time_node is None or not time_node.attributes.get("content"):
            raise ValueError("missing departure time")
        departure_date = date.fromisoformat(time_node.attributes["content"][:10])

        duration_node = card.css_first("span.duration")
        if duration_node is None:
            raise ValueError("missing duration")
        nights = parse_nights(duration_node.text())

        rout_node = card.css_first("div.rout")
        if rout_node is None:
            raise ValueError("missing route")
        # first stop; port and country are TAB-separated: "Miami\t USA, ..."
        first_stop = rout_node.text().strip().split(",")[0]
        departure_port = re.sub(r"\s+", " ", first_stop.split("\t")[0]).strip()
        if not departure_port:
            raise ValueError("empty departure port")

        shared = dict(
            source=self.source,
            cruise_line=cruise_line_from_detail_link(href),
            ship=ship,
            title=title,
            departure_port=departure_port,
            departure_date=departure_date,
            nights=nights,
            url=f"{BASE_URL}{href}",
        )

        offers: list[CruiseOffer] = []
        for row in card.css("div.cabinrow"):
            label_node = row.css_first("div.cabinfirst")
            price_node = row.css_first("div.cabinthird")
            if label_node is None or price_node is None:
                continue
            cabin_type = CABIN_TYPES.get(label_node.text(strip=True).lower())
            if cabin_type is None:
                continue  # Yacht Club, Kind, future labels — not our cabins
            try:
                price_eur = parse_price(price_node.text())
            except ValueError:
                continue  # "ausgebucht" / "ab 0.00€"
            offers.append(
                CruiseOffer(**shared, cabin_type=cabin_type, price_eur=price_eur)
            )
        if not offers:
            raise ValueError("no bookable cabin rows (all sold out or unmapped)")
        return offers
