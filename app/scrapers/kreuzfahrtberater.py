"""kreuzfahrtberater.de — Nuxt 3 SSR payload (devalue-encoded), no open API.

The site is a Nuxt/Vue app (same company as Seascanner — the entry bundle
embeds a seascanner.com brand config; backend is same-origin). Its POST API
(`/api/site/*`, `/api/voyage/getVoyageDetails`, ...) has no discoverable
list/search endpoint — every guess 404'd while `/api/site/getConfiguration`
answered 200, so the pattern was right but the name isn't exposed. What IS
fully server-rendered is the search page itself:

    GET https://www.kreuzfahrtberater.de/kreuzfahrten?page=N

Each page embeds a `<script id="__NUXT_DATA__">` payload in Nuxt's *devalue*
format: one flat JSON array where every nested value is an integer INDEX
into the same array (recursion by reference). The voyage-search store is the
dict holding `voyages` + `totalResultCount` (31,734 total at recon
2026-07-18); `voyages` lists 10 records per page and `?page=N` SSRs disjoint
pages (verified: page 1/2 share zero ids). `_resolve()` walks the index
graph, unwrapping Vue wrapper nodes (["Ref", i], ["ShallowReactive", i], …).

Voyage records are the richest of any source: absolute detail `url`, ISO
`startDate`/`endDate`, `departurePortName`, per-booking-type prices with an
explicit English `cabinClass`, and clean skip flags.

Quirks:
- Prices are integer CENTS as strings ("169900" = 1699.00 EUR).
- nights = endDate - startDate (the `duration` field counts "Tage" and the
  marketing titles disagree with it; date arithmetic is unambiguous).
- Price preference: `prices.cruiseOnly.priceData`, falling back to
  `prices.combined.priceData` (flight-included) when cruise-only is null.
  Non-EUR or missing price → skip; `isPricingOnRequest` / `isNotOpenYet`
  → skip.
- Cruise line: no name field, only logo URLs on cdn.krfb.de whose basename
  is a clean slug ("holland-america-line.929fa52b.png"); hyphens→spaces +
  capitalize, with a small acronym fix-up (msc→MSC, aida→AIDA, ...). A
  genuinely hyphenated brand (A-ROSA → "A Rosa") loses its hyphen — same
  artifact class as other logo-derived sources.
- Empty-store pages return [] from parse() WITHOUT raising, and fetch()
  calls parse() directly (parse_with_fallback only on exception). Rationale:
  a past-the-end SSR page is still ~1 MB of valid HTML with an empty store —
  routing that through parse_with_fallback would burn a pointless Groq call
  on every run's final page. A missing payload/store or an
  all-records-failed page still raises → Groq fallback as usual.
- Includes river cruises (isRiver=True), same as the JT-platform sources.
- **Aggressive rate limiting (hit live 2026-07-18)**: at the default
  1 req/3s the site served exactly 6 pages then answered a hard 429 wall
  that outlasted the full 35s retry/backoff ladder — a request-count
  budget, not a rate limit. Mitigations: max_pages=5, a doubled
  min_request_interval (6s), and fetch() keeps PARTIAL results if a page
  still fails with an HTTP error after retries, instead of discarding the
  whole run.
"""

import json
import logging
import re
from datetime import date
from decimal import Decimal

import httpx

from app.scrapers.base import BaseScraper, CruiseOffer

log = logging.getLogger(__name__)

BASE_URL = "https://www.kreuzfahrtberater.de"
SEARCH_URL = f"{BASE_URL}/kreuzfahrten"

PAYLOAD_RE = re.compile(
    r'<script[^>]*id="__NUXT_DATA__"[^>]*>(.*?)</script>', re.S
)

# Vue/devalue wrapper nodes: ["Ref", <idx>] etc.
WRAPPER_TAGS = {"Ref", "ShallowRef", "Reactive", "ShallowReactive", "Set", "Map"}

CABIN_CLASSES = {"inside", "outside", "balcony", "suite"}

ACRONYMS = {"msc": "MSC", "aida": "AIDA", "ncl": "NCL", "tui": "TUI", "hx": "HX"}


def cruise_line_from_logo_url(url: str) -> str:
    """'https://cdn.krfb.de/cruise-lines/images/holland-america-line.929fa52b.png'
    -> 'Holland America Line' (slug = basename up to the first dot)."""
    slug = url.rsplit("/", 1)[-1].split(".")[0]
    words = [w for w in slug.split("-") if w]
    if not words:
        raise ValueError(f"cannot derive cruise line from {url!r}")
    return " ".join(ACRONYMS.get(w.lower(), w.capitalize()) for w in words)


def _resolve(data: list, idx, depth: int = 0):
    """Dereference one node of a Nuxt devalue payload (ints are indices into
    the flat array; dicts/lists hold indices, not values)."""
    if depth > 12 or not isinstance(idx, int) or not (0 <= idx < len(data)):
        return None
    node = data[idx]
    if isinstance(node, dict):
        return {k: _resolve(data, i, depth + 1) for k, i in node.items()}
    if isinstance(node, list):
        if node and isinstance(node[0], str) and node[0] in WRAPPER_TAGS:
            if len(node) > 1 and isinstance(node[1], int):
                return _resolve(data, node[1], depth + 1)
            return None
        return [_resolve(data, i, depth + 1) for i in node]
    return node  # str / float / bool / None


class KreuzfahrtberaterScraper(BaseScraper):
    source = "kreuzfahrtberater"
    # the site enforces a request-count budget: 6 pages then a hard 429 wall
    # at 1 req/3s (observed live). Stay under it and pace slower.
    max_pages = 5  # 5 SSR pages × 10 voyages = 50 offers/run
    min_request_interval = 6.0

    async def fetch(self) -> list[CruiseOffer]:
        offers: list[CruiseOffer] = []
        for page in range(1, self.max_pages + 1):
            try:
                html = await self.http_get(SEARCH_URL, params={"page": page})
            except httpx.HTTPStatusError as exc:
                # rate-limit wall despite retries — keep what we already have
                log.warning(
                    "%s: page %d failed (%s) — keeping %d offers from earlier pages",
                    self.source, page, exc, len(offers),
                )
                break
            try:
                page_offers = self.parse(html)
            except Exception:
                # broken payload/schema -> normal Groq-fallback path
                page_offers = self.parse_with_fallback(html)
            if not page_offers:
                break  # past the last page (empty store) or unrecoverable
            offers.extend(page_offers)
        return offers

    def parse(self, html: str) -> list[CruiseOffer]:
        """Extract the devalue payload, locate the voyage-search store, and
        build offers. Raises if the payload/store is missing or every record
        fails (schema drift); returns [] only for a genuinely empty store."""
        m = PAYLOAD_RE.search(html)
        if m is None:
            raise ValueError("no __NUXT_DATA__ payload in page")
        data = json.loads(m.group(1))
        store = next(
            (
                node
                for node in data
                if isinstance(node, dict)
                and "voyages" in node
                and "totalResultCount" in node
            ),
            None,
        )
        if store is None:
            raise ValueError("no voyage-search store in payload")
        voyage_indices = data[store["voyages"]]
        if not isinstance(voyage_indices, list) or not voyage_indices:
            return []  # legitimately empty result page

        offers: list[CruiseOffer] = []
        for idx in voyage_indices:
            try:
                offers.append(self._build_offer(_resolve(data, idx)))
            except Exception as exc:
                log.warning("%s: skipping voyage: %s", self.source, exc)
        if not offers:
            raise ValueError(
                f"all {len(voyage_indices)} voyage records failed to parse"
            )
        return offers

    def _build_offer(self, v: dict | None) -> CruiseOffer:
        if not isinstance(v, dict):
            raise ValueError("unresolvable voyage record")
        if v.get("isPricingOnRequest"):
            raise ValueError("pricing on request")
        if v.get("isNotOpenYet"):
            raise ValueError("not open for booking yet")

        prices = v.get("prices") or {}
        price_data = (prices.get("cruiseOnly") or {}).get("priceData") or (
            prices.get("combined") or {}
        ).get("priceData")
        if not price_data:
            raise ValueError("no price data")
        price = price_data.get("price") or {}
        if price.get("currency") != "EUR":
            raise ValueError(f"non-EUR currency {price.get('currency')!r}")
        price_eur = Decimal(str(price["amount"])) / 100  # integer cents

        start = date.fromisoformat(v["startDate"])
        nights = (date.fromisoformat(v["endDate"]) - start).days
        if nights <= 0:
            raise ValueError(f"non-positive nights ({nights})")

        cabin = price_data.get("cabinClass")
        cabin_type = cabin if cabin in CABIN_CLASSES else "inside"

        return CruiseOffer(
            source=self.source,
            cruise_line=cruise_line_from_logo_url(v["operatorLogoUrl"]),
            ship=v["shipName"],
            title=v.get("shortTitle") or v["title"],
            departure_port=v["departurePortName"],
            departure_date=start,
            nights=nights,
            url=v["url"],
            cabin_type=cabin_type,
            price_eur=price_eur,
        )
