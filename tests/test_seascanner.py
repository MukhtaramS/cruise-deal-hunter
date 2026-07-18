import asyncio
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.scrapers import SCRAPERS
from app.scrapers.seascanner import API_URL, SeascannerScraper

# real API response saved from https://www.seascanner.de/api/packages/search
FIXTURE_TEXT = (Path(__file__).parent / "fixtures" / "seascanner.json").read_text()


class TestParseFixture:
    def test_parses_all_bookable_items(self):
        offers = SeascannerScraper().parse(FIXTURE_TEXT)
        assert len(offers) == 8
        assert all(o.source == "seascanner" for o in offers)

    def test_first_offer_fields(self):
        offer = SeascannerScraper().parse(FIXTURE_TEXT)[0]
        assert offer.ship == "Norwegian Getaway"
        assert offer.cruise_line == "Norwegian Cruise Line"
        assert offer.departure_port == "Miami"
        assert offer.departure_date == date(2026, 7, 20)
        assert offer.nights == 4
        assert offer.cabin_type == "inside"  # superCategory INSIDE
        assert offer.price_eur == Decimal("385")
        assert offer.url.startswith("https://www.seascanner.de/reisen/")

    def test_sold_out_item_is_skipped(self):
        data = json.loads(FIXTURE_TEXT)
        data["items"][0]["soldOut"] = True
        offers = SeascannerScraper().parse(json.dumps(data))
        assert len(offers) == 7
        assert "Norwegian Getaway" not in {o.ship for o in offers}

    def test_item_without_price_is_skipped(self):
        data = json.loads(FIXTURE_TEXT)
        del data["items"][0]["price"]
        offers = SeascannerScraper().parse(json.dumps(data))
        assert len(offers) == 7

    def test_cabin_type_mapping(self):
        data = json.loads(FIXTURE_TEXT)
        data["items"][0]["price"]["superCategory"] = "BALCONY"
        data["items"][1]["price"]["superCategory"] = "UNKNOWN_FUTURE_VALUE"
        offers = SeascannerScraper().parse(json.dumps(data))
        assert offers[0].cabin_type == "balcony"
        assert offers[1].cabin_type == "inside"  # unknown -> conservative default


class TestFetch:
    def test_fetch_paginates_and_sends_domain_header(self):
        requests: list[httpx.Request] = []
        page = json.dumps(
            {"items": json.loads(FIXTURE_TEXT)["items"], "numberOfPages": 869}
        )

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if int(request.url.params["pageNumber"]) < 2:
                return httpx.Response(200, text=page)
            return httpx.Response(200, text=json.dumps({"items": []}))

        scraper = SeascannerScraper(transport=httpx.MockTransport(handler))
        scraper.min_request_interval = 0
        offers = asyncio.run(scraper.fetch())

        assert len(offers) == 16  # two full pages, then the empty page stops it
        assert len(requests) == 3
        assert all(r.headers["domain"] == "www.seascanner.de" for r in requests)
        assert all(str(r.url).startswith(API_URL) for r in requests)

    def test_fetch_respects_max_pages(self):
        page = json.dumps({"items": json.loads(FIXTURE_TEXT)["items"]})
        scraper = SeascannerScraper(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, text=page))
        )
        scraper.min_request_interval = 0
        offers = asyncio.run(scraper.fetch())
        assert len(offers) == 8 * scraper.max_pages


class TestLLMFallback:
    def test_fallback_on_unparseable_response(self, monkeypatch):
        calls = {}

        def fake_extract(text, source):
            calls["source"] = source
            return []

        monkeypatch.setattr("app.llm.extract_offers", fake_extract)
        not_json = "<html><body>" + "<p>maintenance page</p>" * 50 + "</body></html>"
        SeascannerScraper().parse_with_fallback(not_json)
        assert calls["source"] == "seascanner"

    def test_no_fallback_when_parse_succeeds(self, monkeypatch):
        monkeypatch.setattr(
            "app.llm.extract_offers",
            lambda *a: pytest.fail("LLM fallback must not run when parse works"),
        )
        assert len(SeascannerScraper().parse_with_fallback(FIXTURE_TEXT)) == 8


class TestRegistry:
    def test_seascanner_is_registered(self):
        assert SeascannerScraper in SCRAPERS
