"""kreuzfahrt.de — AJAX HTML-fragment API via the cruiseportal.de widget.

kreuzfahrt.de (singular — NOT kreuzfahrten.de, which we also scrape) is a
Yii/PHP site with no cruise data of its own: its /search page embeds an
iframe from **cruiseportal.de** (`/modul/list?ref=kreuzfahrt&...`), the same
JT-Touristik platform that powers kreuzfahrten.de (identical image paths and
special IDs on both). The iframe shell loads results client-side
(InfiniteScrolling.js) from:

    GET https://www.cruiseportal.de/modul/site3/ajax/routesCruise.ax.html
        ?ref=kreuzfahrt&la=de&srcShipType=1&srcBookedUp=0
        &srcPriceMin=0&srcPriceMax=0&srcOrderBy=pr_MinPriceInnen_ASC
        &action=loadInfiniteScrolling&intParentSiteID=27803&srcStartRoutes=<offset>

Response: JSON {"status": "ok", "htmlRouten": "<fragment>", "intCount": 5} —
an HTML fragment of 5 `li#cruiseItemN` cards per batch; `srcStartRoutes` is
the offset. `intParentSiteID=27803` and the param set mirror what the site's
own iframe sends (SITE_ID/URL_DOMAIN constants in the /modul/list shell,
verified live 2026-07-18). `srcBookedUp=0` excludes sold-out sailings
server-side; `srcShipType=1` = ocean cruises (the site's own /search
default). Detail links point back to kreuzfahrt.de (/cruise/<id>, verified
200).

Field quirks (all confirmed on real fragments):
- A card can list many departure dates ("10 Termine verfügbar"); only the
  "Gewählter Termin" date matches the displayed price, and it's the first
  dd.mm.yyyy in the `.date` block — which is exactly what
  `parse_german_date`'s regex search picks.
- Price format "p.P. ab € 51,-" == kreuzfahrten.de's (same platform), so
  `parse_price` is reused from there.
- Departure port: the `.route-list-item-bottom` bar holds "Genua -
  Marseille" (departure - arrival) as a DIRECT text node next to child
  elements — extracted with text(deep=False) so the "Routeninfo" span
  doesn't bleed in.
- Cruise line: no text field; the vendor logo filename is just a numeric id
  ("16-20200103.png" → vendor 16). The module's own filter endpoint
  (`vs3/ajax/routesFilter.ax.html?...action=loadFilterValues&uriName=
  v_VendorID`) returns the authoritative id→name map, refreshed once per
  fetch; DEFAULT_VENDORS below is a real snapshot (2026-07-18, 49 vendors)
  used as fallback so --file mode, tests, and a failed refresh keep
  working. Unknown vendor ids skip the card rather than pollute data.
"""

import json
import logging
import re
from decimal import Decimal

from selectolax.parser import HTMLParser, Node

from app.scrapers.base import BaseScraper, CruiseOffer
from app.scrapers.dreamlines import parse_german_date, parse_nights
from app.scrapers.kreuzfahrten import parse_price

log = logging.getLogger(__name__)

WIDGET_BASE = "https://www.cruiseportal.de/modul"
ROUTES_URL = f"{WIDGET_BASE}/site3/ajax/routesCruise.ax.html"
FILTER_URL = f"{WIDGET_BASE}/vs3/ajax/routesFilter.ax.html"
SITE_ID = "27803"  # kreuzfahrt.de's parent-site id inside the widget
BATCH_SIZE = 5  # fixed by the widget

COMMON_PARAMS = {
    "ref": "kreuzfahrt",
    "la": "de",
    "srcShipType": "1",  # ocean; mirrors the site's own /search default
    "srcBookedUp": "0",  # server-side sold-out filter
    "srcPriceMin": "0",
    "srcPriceMax": "0",
    "srcOrderBy": "pr_MinPriceInnen_ASC",  # cheapest first
}

# real snapshot of the widget's vendor filter, 2026-07-18 — fallback only,
# fetch() refreshes it live each run
DEFAULT_VENDORS: dict[str, str] = {
    "119": "1AVista Reisen",
    "49": "A-ROSA",
    "1": "AIDA Cruises",
    "54": "Amadeus Flusskreuzfahrten",
    "216": "Aranui",
    "415": "AROYA Cruises",
    "111": "Azamara Cruises",
    "13": "Carnival Cruise Line",
    "30": "Celebrity Cruises",
    "427": "Celebrity River Cruises",
    "183": "Celestyal Cruises",
    "53": "Color Line",
    "436": "COMPASS Kreuzfahrten",
    "6": "Costa Kreuzfahrten",
    "7": "CroisiEurope",
    "37": "Crystal Cruises",
    "20": "Cunard",
    "110": "DCS Touristik",
    "163": "Disney Cruise Line",
    "218": "Emerald Cruises",
    "309": "Explora Journeys",
    "18": "Hapag-Lloyd Cruises",
    "259": "Havila Voyages",
    "22": "Holland America Line",
    "8": "Hurtigruten",
    "277": "HX Expeditions",
    "16": "MSC Cruises",
    "4": "Nicko Cruises",
    "11": "Norwegian Cruise Line (NCL)",
    "69": "Oceania Cruises",
    "177": "P&O Cruises",
    "10": "Phoenix",
    "41": "plantours",
    "60": "Ponant",
    "42": "Princess Cruises",
    "35": "Regent Seven Seas Cruises",
    "263": "Riva Tours",
    "365": "Riverside Luxury Cruises",
    "23": "Royal Caribbean International",
    "196": "Scenic Luxury Cruises & Tours",
    "185": "SE-Tours",
    "29": "Seabourn",
    "34": "Sea Cloud Cruises",
    "52": "SeaDream Yacht Club",
    "36": "Silversea Cruises",
    "124": "Star Clippers",
    "133": "TUI Cruises (Mein Schiff)",
    "198": "VIVA Cruises",
    "25": "Windstar Cruises",
}

# some logo filenames carry an "i<N>-" prefix before the vendor id
# ("/vendor/i1-42-20200211.png" = vendor 42) — seen live during the first
# production cycle, where it caused Princess Cruises cards to be skipped
VENDOR_ID_RE = re.compile(r"/vendor/(?:i\d+-)?(\d+)-")


class KreuzfahrtDeScraper(BaseScraper):
    # slug deliberately differs from the bare domain: "kreuzfahrt" would be
    # one typo away from the existing "kreuzfahrten" source
    source = "kreuzfahrt_de"
    max_pages = 10  # 10 batches × 5 cards = 50 cruises/run (+1 vendor request)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._vendors: dict[str, str] = dict(DEFAULT_VENDORS)

    async def fetch(self) -> list[CruiseOffer]:
        await self._refresh_vendors()
        offers: list[CruiseOffer] = []
        for page in range(self.max_pages):
            text = await self.http_get(
                ROUTES_URL,
                params={
                    **COMMON_PARAMS,
                    "action": "loadInfiniteScrolling",
                    "intParentSiteID": SITE_ID,
                    "srcStartRoutes": page * BATCH_SIZE,
                },
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            page_offers = self.parse_with_fallback(text)
            if not page_offers:
                break  # past the last batch (or a batch we couldn't parse)
            offers.extend(page_offers)
        return offers

    async def _refresh_vendors(self) -> None:
        """Merge the live vendor id→name map over the static snapshot. On any
        failure the snapshot alone carries the run."""
        try:
            text = await self.http_get(
                FILTER_URL,
                params={
                    "ref": COMMON_PARAMS["ref"],
                    "la": COMMON_PARAMS["la"],
                    "action": "loadFilterValues",
                    "uriName": "v_VendorID",
                },
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            live = parse_vendor_map(text)
            if live:
                self._vendors = {**DEFAULT_VENDORS, **live}
                log.info("%s: vendor map refreshed (%d entries)", self.source, len(live))
        except Exception:
            log.exception("%s: vendor map refresh failed — using static snapshot", self.source)

    def parse(self, text: str) -> list[CruiseOffer]:
        """Parse one loadInfiniteScrolling JSON response. A single broken
        card is skipped; if ALL fail, parse_with_fallback escalates."""
        fragment = json.loads(text).get("htmlRouten") or ""
        if not fragment.strip():
            return []
        tree = HTMLParser(fragment)
        offers: list[CruiseOffer] = []
        for card in tree.css("li[id^=cruiseItem]"):
            try:
                offers.append(self._parse_card(card))
            except Exception as exc:
                log.warning("%s: skipping card: %s", self.source, exc)
        return offers

    def _parse_card(self, card: Node) -> CruiseOffer:
        def text(selector: str, deep: bool = True) -> str:
            node = card.css_first(selector)
            if node is None:
                raise ValueError(f"missing {selector!r}")
            return re.sub(r"\s+", " ", node.text(deep=deep)).strip()

        link = card.css_first("div.ship-name a")
        if link is None or not link.attributes.get("href"):
            raise ValueError("missing ship link")
        ship = re.sub(r"\s+", " ", link.text(strip=True))
        url = link.attributes["href"]  # absolute, points back to kreuzfahrt.de

        route_name = text("div.route-name")
        nights = parse_nights(route_name)
        title = re.sub(r"^\d+\s*N(?:ächte|acht)\s*", "", route_name).strip() or route_name

        # "Gewählter Termin: 01.12.2026 - 02.12.2026" — first date is departure
        departure_date = parse_german_date(text("div.date"))

        price_eur: Decimal = parse_price(text("div.prices span.price"))

        # harbor line is a DIRECT text node ("Genua - Marseille") between the
        # map icon and the "Routeninfo" span
        harbors = text("div.route-list-item-bottom", deep=False)
        departure_port = harbors.split(" - ")[0].strip()
        if not departure_port:
            raise ValueError("empty departure port")

        logo = card.css_first("img.vendorPic")
        if logo is None or not logo.attributes.get("src"):
            raise ValueError("missing vendor logo")
        m = VENDOR_ID_RE.search(logo.attributes["src"])
        if not m:
            raise ValueError(f"no vendor id in {logo.attributes['src']!r}")
        cruise_line = self._vendors.get(m.group(1))
        if cruise_line is None:
            raise ValueError(f"unknown vendor id {m.group(1)}")

        return CruiseOffer(
            source=self.source,
            cruise_line=cruise_line,
            ship=ship,
            title=title,
            departure_port=departure_port,
            departure_date=departure_date,
            nights=nights,
            url=url,
            cabin_type="inside",  # list shows the cheapest (inside-sorted) price
            price_eur=price_eur,
        )


def parse_vendor_map(text: str) -> dict[str, str]:
    """Parse the loadFilterValues response ({"status": "ok", "html":
    "<select>...<option value='16'>MSC Cruises</option>..."})."""
    html = json.loads(text).get("html") or ""
    return {
        m.group(1): m.group(2).strip()
        for m in re.finditer(r"<option[^>]*value=.?(\d+).?[^>]*>\s*([^<]+?)\s*</option>", html)
    }
