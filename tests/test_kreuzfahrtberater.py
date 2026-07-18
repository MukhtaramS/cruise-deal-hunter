import asyncio
import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.scrapers import SCRAPERS
from app.scrapers.kreuzfahrtberater import (
    SEARCH_URL,
    KreuzfahrtberaterScraper,
    cruise_line_from_logo_url,
)

# real SSR search page saved from https://www.kreuzfahrtberater.de/kreuzfahrten
# on 2026-07-18: Nuxt devalue payload with 10 voyage records (mix of river and
# ocean, one flight-included Royal Caribbean sailing)
FIXTURE_HTML = (
    Path(__file__).parent / "fixtures" / "kreuzfahrtberater.de.html"
).read_text()

# minimal but structurally valid payload with an EMPTY voyage store — the
# shape a past-the-last SSR page produces
EMPTY_STORE_HTML = (
    '<html><body><script type="application/json" id="__NUXT_DATA__">'
    + json.dumps([{"voyages": 1, "totalResultCount": 2}, [], 0])
    + "</script></body></html>"
)


class TestCruiseLineFromLogoUrl:
    def test_multi_word_slug(self):
        url = "https://cdn.krfb.de/cruise-lines/images/holland-america-line.929fa52b.png"
        assert cruise_line_from_logo_url(url) == "Holland America Line"

    def test_acronym_fixup(self):
        url = "https://cdn.krfb.de/cruise-lines/images/msc-cruises.abc123.svg"
        assert cruise_line_from_logo_url(url) == "MSC Cruises"

    def test_hyphenated_brand_loses_hyphen(self):
        # documented artifact: A-ROSA -> "A Rosa"
        url = "https://cdn.krfb.de/cruise-lines/images/a-rosa.73kr4brc.png"
        assert cruise_line_from_logo_url(url) == "A Rosa"


class TestParseFixture:
    def test_parses_all_voyages(self):
        offers = KreuzfahrtberaterScraper().parse(FIXTURE_HTML)
        assert len(offers) == 10
        assert all(o.source == "kreuzfahrtberater" for o in offers)

    def test_first_offer_fields(self):
        offer = KreuzfahrtberaterScraper().parse(FIXTURE_HTML)[0]
        assert offer.ship == "Anesha"
        assert offer.cruise_line == "Phoenix Reisen"
        assert offer.title == "Rheinvergnügen"
        assert offer.departure_port == "Köln"
        assert offer.departure_date == date(2026, 7, 22)
        assert offer.nights == 9  # endDate - startDate, not the "Tage" field
        assert offer.cabin_type == "outside"
        assert offer.price_eur == Decimal("1699")  # "169900" cents -> EUR
        assert offer.url == (
            "https://www.kreuzfahrtberater.de/kreuzfahrt-k283659-rheinvergnuegen-anesha-koeln"
        )

    def test_cabin_class_passthrough(self):
        offers = KreuzfahrtberaterScraper().parse(FIXTURE_HTML)
        by_ship = {o.ship: o.cabin_type for o in offers}
        assert by_ship["Zaandam"] == "inside"
        assert by_ship["Anesha"] == "outside"

    def test_all_records_failing_raises_for_fallback(self):
        # schema drift simulation: rename the prices key in every voyage
        # record -> every record fails -> parse raises so fetch() can route
        # the page through the Groq fallback
        broken = re.sub(r'"prices":\d+', '"pricesX":0', FIXTURE_HTML)
        with pytest.raises(ValueError, match="failed to parse"):
            KreuzfahrtberaterScraper().parse(broken)

    def test_empty_store_returns_no_offers_without_raising(self):
        assert KreuzfahrtberaterScraper().parse(EMPTY_STORE_HTML) == []

    def test_missing_payload_raises(self):
        with pytest.raises(ValueError, match="__NUXT_DATA__"):
            KreuzfahrtberaterScraper().parse("<html><body>leer</body></html>")


class TestFetch:
    def test_fetch_paginates_until_empty_page(self):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if int(request.url.params["page"]) <= 2:
                return httpx.Response(200, text=FIXTURE_HTML)
            return httpx.Response(200, text=EMPTY_STORE_HTML)

        scraper = KreuzfahrtberaterScraper(transport=httpx.MockTransport(handler))
        scraper.min_request_interval = 0
        offers = asyncio.run(scraper.fetch())

        assert len(offers) == 20  # two full pages, then the empty store stops it
        assert len(requests) == 3
        assert all(str(r.url).startswith(SEARCH_URL) for r in requests)
        assert [r.url.params["page"] for r in requests] == ["1", "2", "3"]

    def test_429_wall_keeps_partial_results(self):
        # observed live: a hard 429 budget wall that outlasts all retries —
        # earlier pages' offers must survive instead of the run dying
        def handler(request: httpx.Request) -> httpx.Response:
            if int(request.url.params["page"]) <= 2:
                return httpx.Response(200, text=FIXTURE_HTML)
            return httpx.Response(429)

        scraper = KreuzfahrtberaterScraper(transport=httpx.MockTransport(handler))
        scraper.min_request_interval = 0
        scraper.backoff_base = 0
        offers = asyncio.run(scraper.fetch())
        assert len(offers) == 20  # pages 1-2 kept, page 3's wall breaks the loop

    def test_fetch_respects_max_pages(self):
        scraper = KreuzfahrtberaterScraper(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, text=FIXTURE_HTML)
            )
        )
        scraper.min_request_interval = 0
        offers = asyncio.run(scraper.fetch())
        assert len(offers) == 10 * scraper.max_pages


class TestLLMFallback:
    def test_empty_store_page_never_calls_llm(self, monkeypatch):
        # the deliberate design deviation: a valid-but-empty SSR page is a
        # legitimate end-of-results, not a parse failure
        monkeypatch.setattr(
            "app.llm.extract_offers",
            lambda *a: pytest.fail("LLM must not run for an empty result store"),
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=EMPTY_STORE_HTML)

        scraper = KreuzfahrtberaterScraper(transport=httpx.MockTransport(handler))
        scraper.min_request_interval = 0
        assert asyncio.run(scraper.fetch()) == []

    def test_broken_payload_falls_back_to_llm(self, monkeypatch):
        calls = {}

        def fake_extract(html, source):
            calls["source"] = source
            return []

        monkeypatch.setattr("app.llm.extract_offers", fake_extract)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200, text="<html><body>" + "<p>kaputt</p>" * 100 + "</body></html>"
            )

        scraper = KreuzfahrtberaterScraper(transport=httpx.MockTransport(handler))
        scraper.min_request_interval = 0
        asyncio.run(scraper.fetch())
        assert calls["source"] == "kreuzfahrtberater"

    def test_no_fallback_when_parse_succeeds(self, monkeypatch):
        monkeypatch.setattr(
            "app.llm.extract_offers",
            lambda *a: pytest.fail("LLM fallback must not run when parse works"),
        )
        assert len(KreuzfahrtberaterScraper().parse_with_fallback(FIXTURE_HTML)) == 10


class TestRegistry:
    def test_kreuzfahrtberater_is_registered(self):
        assert KreuzfahrtberaterScraper in SCRAPERS
