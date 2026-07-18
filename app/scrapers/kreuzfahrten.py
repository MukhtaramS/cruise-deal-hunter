"""kreuzfahrten.de — server-rendered search results, no JS framework, no API.

The homepage has no JSON API or JS bundle worth grepping (unlike seascanner
and cruiseportal24) — it's a classic PHP-style site, and the unfiltered
search results page IS server-rendered HTML with real offers baked in:

    GET https://www.kreuzfahrten.de/termin/
        ?srcOrderBy=c_dateDepart_ASC&strOrderBy=c_dateDepart_ASC
        &srcPriceMin=0&srcPriceMax=0&page=N&per-page=10

`srcPriceMin=0&srcPriceMax=0` is the site's own sentinel for "no price
filter" (confirmed: the bare `/termin/` with no params at all reports the
same total, "19.117 Routen, 46.223 Reisen", as pages built with these params
in their own pagination links — verified live 2026-07-18, saved to
samples/kreuzfahrten.de.html). 10 cards per page.

Each result is a `div.routeListItem` with `data-cruise-id`, ship name, a
combined "N Nächte <destination>" line, departure/arrival ports, one
departure date, and a lead ("ab") price. Detail page: `/termin/<cruise-id>.html`.

Two things this listing does NOT give cleanly:
- **Cabin type**: no breakdown shown in the compact card (only one lead
  price) — cabin_type defaults to "inside", same convention as the other
  two scrapers' lead-price handling.
- **Cruise line**: there's no dedicated text field for it. The vendor logo
  `<img alt>` looked like the obvious source but is unreliable in practice —
  confirmed on real cards: sometimes empty (Cunard, Norwegian), sometimes a
  generic placeholder ("Impressionen" on a Royal Caribbean card) instead of
  the real line name. `_cruise_line_from_logo_url` derives it from the logo
  filename instead (e.g. "Carnival-Cruise-Lines-13.png" -> "Carnival Cruise
  Lines"), which was reliable across every real card sampled.

Sold-out / price-on-request cards ("Ausgebucht", `class="priceInquiry"`
visible with `class="price hidden"`) are skipped — real, naturally-occurring
example kept in tests/fixtures/kreuzfahrten.de.html.
"""

import logging
import re
from datetime import date
from decimal import Decimal

from selectolax.parser import HTMLParser, Node

from app.scrapers.base import BaseScraper, CruiseOffer
from app.scrapers.dreamlines import parse_german_date, parse_nights

log = logging.getLogger(__name__)

BASE_URL = "https://www.kreuzfahrten.de"
SEARCH_URL = f"{BASE_URL}/termin/"

LOGO_FILENAME_RE = re.compile(r"\.(png|jpe?g|svg)(\?.*)?$", re.IGNORECASE)
TRAILING_ID_RE = re.compile(r"(-\d+)+$")


def parse_price(text: str) -> Decimal:
    """'ab € 1.462,-' -> 1462; '€ 260,-' -> 260. The site never shows real
    cents — the trailing ",-" is a fixed placeholder, not a decimal."""
    normalized = text.replace("\xa0", " ")
    m = re.search(r"€\s*([\d.]+),-", normalized)
    if not m:
        raise ValueError(f"no price in {text!r}")
    return Decimal(m.group(1).replace(".", ""))


def cruise_line_from_logo_url(url: str) -> str:
    """Derive a display cruise-line name from the vendor logo filename — the
    <img alt> text is unreliable (often empty or a generic placeholder like
    "Impressionen", confirmed on real cards). Filenames look like
    'Carnival-Cruise-Lines-13.png' or 'norwegian-cruise-line-ncl-11-
    20260107.png': strip the trailing run of '-<digits>' id/date segments,
    then space out hyphens. Already-mixed-case stems are kept as-is (most
    lines); all-lowercase stems get title-cased. Known artifact: a trailing
    abbreviation baked into the slug (e.g. '...-ncl') survives as an extra
    title-cased word."""
    stem = url.rsplit("/", 1)[-1]
    stem = LOGO_FILENAME_RE.sub("", stem)
    stem = TRAILING_ID_RE.sub("", stem)
    # platform filename variant: an "i<N>-" prefix before the actual name
    # ("i1-holland-america-line-22.png") — without stripping it, the line
    # came out as "I1 Holland America Line" in production
    stem = re.sub(r"^i\d+-", "", stem)
    name = stem.replace("-", " ").replace("_", " ").strip()
    if not name:
        raise ValueError(f"cannot derive cruise line from {url!r}")
    return name.title() if name == name.lower() else name


class KreuzfahrtenScraper(BaseScraper):
    source = "kreuzfahrten"
    max_pages = 10  # 100 offers/run at 10 items/page

    async def fetch(self) -> list[CruiseOffer]:
        offers: list[CruiseOffer] = []
        for page in range(1, self.max_pages + 1):
            html = await self.http_get(
                SEARCH_URL,
                params={
                    "srcOrderBy": "c_dateDepart_ASC",
                    "strOrderBy": "c_dateDepart_ASC",
                    "srcPriceMin": 0,
                    "srcPriceMax": 0,
                    "page": page,
                    "per-page": 10,
                },
            )
            page_offers = self.parse_with_fallback(html)
            if not page_offers:
                break  # past the last page (or a page we couldn't parse)
            offers.extend(page_offers)
        return offers

    def parse(self, html: str) -> list[CruiseOffer]:
        tree = HTMLParser(html)
        offers: list[CruiseOffer] = []
        for card in tree.css("div.routeListItem"):
            try:
                offers.append(self._parse_card(card))
            except Exception as exc:
                log.warning("%s: skipping card: %s", self.source, exc)
        return offers

    def _parse_card(self, card: Node) -> CruiseOffer:
        cruise_id = card.attributes.get("data-cruise-id")
        if not cruise_id:
            raise ValueError("missing data-cruise-id")

        def text(selector: str) -> str:
            node = card.css_first(selector)
            if node is None:
                raise ValueError(f"missing {selector!r}")
            return re.sub(r"\s+", " ", node.text(strip=True)).strip()

        ship = text("div.shipName a.lnkCruise")

        route_text = text("div.routeName")
        nights = parse_nights(route_text)
        title = re.sub(r"^\d+\s*N(?:ächte|acht)\s*", "", route_text).strip() or route_text

        harbor_text = text("div.harborNames")
        departure_port = harbor_text.split(" - ")[0].strip()
        if not departure_port:
            raise ValueError("empty departure port")

        date_node = card.css_first("[data-datum-von]")
        if date_node is None:
            raise ValueError("missing data-datum-von")
        departure_date: date = parse_german_date(date_node.attributes["data-datum-von"])

        price_node = card.css_first("div.preisWrapper span.price")
        if price_node is None or "hidden" in (price_node.attributes.get("class") or "").split():
            raise ValueError("no visible price (sold out or price on request)")
        price_eur = parse_price(price_node.text())

        logo_node = card.css_first("img.vendorPic")
        if logo_node is None or not logo_node.attributes.get("src"):
            raise ValueError("missing vendor logo")
        cruise_line = cruise_line_from_logo_url(logo_node.attributes["src"])

        return CruiseOffer(
            source=self.source,
            cruise_line=cruise_line,
            ship=ship,
            title=title,
            departure_port=departure_port,
            departure_date=departure_date,
            nights=nights,
            url=f"{BASE_URL}/termin/{cruise_id}.html",
            cabin_type="inside",  # no cabin breakdown in the compact listing
            price_eur=price_eur,
        )
