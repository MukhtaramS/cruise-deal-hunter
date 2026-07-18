"""cruiseportal24.com — JSON API scraper (via the cruisec.net widget backend).

cruiseportal24.com is a small Jimdo-built site with no cruise data of its
own; every listing page (e.g. /karibik/) just embeds an iframe pointing at a
third-party whitelabel cruise-search widget: cpx.cruisec.net, running an
AngularJS app for partner/agency id ("aid") 203142. The widget's own search
endpoint is what actually holds the data:

    GET https://cpx.cruisec.net/api/Search/Results/json
        ?aid=203142&url=Meer/all/all/all/all/all/all&page=N

`url` is the widget's internal filter path in `<sea>/<area>/<line>/<ship>/
<duration>/<departure>/<sort>` form; `Meer/all/all/all/all/all/all` is the
canonical "ocean cruises, no filters" search, confirmed against the facet
endpoint (`/api/Search/count/json?aid=203142`), whose own `url` field reports
that exact string as the current-filter default. 10 results/page, `pages`
in the response gives the total. Verified live 2026-07-17: default filters
returned 78,332 total cruises.

Each item has no explicit cabin-type breakdown (unlike Seascanner's
superCategory) — the lead price is the cheapest available cabin, so
cabin_type defaults to "inside" as a reasonable approximation.

⚠️ Detail-page URL NOT independently verified: the JSON has no `link` field,
and cpx.cruisec.net appears to be an SPA with a catch-all route (every path
probed returned 200, including ones that are almost certainly wrong), so an
empirical check couldn't disambiguate. `/product/<productID>?aid=<aid>` is
the most plausible guess by convention. Confirm with real browser DevTools
(Network tab, click a result card) before trusting it for outbound links.
"""

import json
import logging
from datetime import date
from decimal import Decimal

from app.scrapers.base import BaseScraper, CruiseOffer

log = logging.getLogger(__name__)

BASE_URL = "https://cpx.cruisec.net"
API_URL = f"{BASE_URL}/api/Search/Results/json"
API_HEADERS = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}
AID = "203142"  # cruiseportal24.com's partner/agency id in the widget
DEFAULT_SEARCH_URL = "Meer/all/all/all/all/all/all"  # ocean cruises, no filters


class Cruiseportal24Scraper(BaseScraper):
    source = "cruiseportal24"
    max_pages = 10  # 100 offers/run at 10 items/page

    async def fetch(self) -> list[CruiseOffer]:
        offers: list[CruiseOffer] = []
        for page in range(1, self.max_pages + 1):
            text = await self.http_get(
                API_URL,
                params={"aid": AID, "url": DEFAULT_SEARCH_URL, "page": page},
                headers=API_HEADERS,
            )
            page_offers = self.parse_with_fallback(text)
            if not page_offers:
                break  # past the last page (or a page we couldn't parse)
            offers.extend(page_offers)
        return offers

    def parse(self, text: str) -> list[CruiseOffer]:
        """Parse one API response (JSON text). A single malformed item is
        skipped; if ALL items fail, parse_with_fallback escalates to the LLM.
        An {"error": ...} response (e.g. EMPTY RESULT) yields no offers.

        The backend is PHP: when `cruises`' keys don't start at 0 (e.g. page 2
        holds indices 10-19), json_encode emits a JSON *object* instead of an
        array — same data, different shape. Confirmed live: page 1 is a list,
        page 2 is a dict. Normalize both to a list of dicts here."""
        data = json.loads(text)
        items = data.get("cruises") or []
        if isinstance(items, dict):
            items = list(items.values())
        offers: list[CruiseOffer] = []
        for item in items:
            try:
                offers.append(self._parse_item(item))
            except Exception as exc:
                log.warning(
                    "%s: skipping item %r: %s",
                    self.source, (item or {}).get("masterCruiseID"), exc,
                )
        return offers

    def _parse_item(self, item: dict) -> CruiseOffer:
        if item.get("isDisabled"):
            raise ValueError("disabled")
        if item.get("currency") != "EUR":
            raise ValueError(f"non-EUR currency {item.get('currency')!r}")
        departure_port = item["route"].split(" - ")[0].strip()
        if not departure_port:
            raise ValueError("empty departure port")
        return CruiseOffer(
            source=self.source,
            cruise_line=item["cruiseLine"],
            ship=item["ship"],
            title=item["title"],
            departure_port=departure_port,
            departure_date=date.fromisoformat(item["departure_raw"]),
            nights=int(item["duration"]),
            url=f"{BASE_URL}/product/{item['productID']}?aid={AID}",
            cabin_type="inside",  # no cabin breakdown in the listing; lead price
            price_eur=Decimal(str(item["priceUnformatted"]).replace(",", ".")),
        )
