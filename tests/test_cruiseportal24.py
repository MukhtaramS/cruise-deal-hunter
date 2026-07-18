import asyncio
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.scrapers import SCRAPERS
from app.scrapers.cruiseportal24 import API_URL, Cruiseportal24Scraper

# real API response saved from https://cpx.cruisec.net/api/Search/Results/json
# (the widget backend cruiseportal24.com embeds), trimmed to 5 real items plus
# one deliberately disabled item to exercise the skip-one-bad-item path.
FIXTURE_TEXT = (Path(__file__).parent / "fixtures" / "cruiseportal24.json").read_text()

# real page-2 response: PHP's json_encode serializes `cruises` as an OBJECT
# (keys "10".."12") instead of an array here, since the keys don't start at 0.
DICT_SHAPE_FIXTURE_TEXT = (
    Path(__file__).parent / "fixtures" / "cruiseportal24_page2_dict_shape.json"
).read_text()


class TestParseFixture:
    def test_parses_enabled_items_and_skips_disabled_one(self):
        offers = Cruiseportal24Scraper().parse(FIXTURE_TEXT)
        assert len(offers) == 5
        assert all(o.source == "cruiseportal24" for o in offers)

    def test_first_offer_fields(self):
        offer = Cruiseportal24Scraper().parse(FIXTURE_TEXT)[0]
        assert offer.ship == "Jewel of the Seas"
        assert offer.cruise_line == "Royal Caribbean"
        assert offer.departure_port == "Fort Lauderdale"
        assert offer.departure_date == date(2026, 8, 24)
        assert offer.nights == 4
        assert offer.cabin_type == "inside"
        assert offer.price_eur == Decimal("156")
        assert offer.url == "https://cpx.cruisec.net/product/RWFLLFLL322438?aid=203142"

    def test_departure_port_is_first_route_stop(self):
        offers = Cruiseportal24Scraper().parse(FIXTURE_TEXT)
        ports = {o.departure_port for o in offers}
        assert "Fort Lauderdale" in ports
        assert "Miami" in ports
        assert all(" - " not in p for p in ports)  # only the first stop kept

    def test_disabled_item_is_skipped(self):
        offers = Cruiseportal24Scraper().parse(FIXTURE_TEXT)
        assert "BROKEN-DISABLED-EXAMPLE" not in {
            o.title for o in offers
        }  # sanity: fixture's broken item never surfaces as a real offer

    def test_non_eur_item_is_skipped(self):
        data = json.loads(FIXTURE_TEXT)
        data["cruises"][0]["currency"] = "USD"
        offers = Cruiseportal24Scraper().parse(json.dumps(data))
        assert len(offers) == 4

    def test_error_response_yields_no_offers(self):
        error_response = json.dumps({"error": {"code": 107, "msg": "EMPTY RESULT"}})
        assert Cruiseportal24Scraper().parse(error_response) == []

    def test_dict_shaped_cruises_are_parsed_like_a_list(self):
        # PHP json_encode quirk: non-zero-starting keys serialize `cruises`
        # as an object, not an array. Confirmed live on page 2.
        offers = Cruiseportal24Scraper().parse(DICT_SHAPE_FIXTURE_TEXT)
        assert len(offers) == 3
        assert all(o.source == "cruiseportal24" for o in offers)
        assert {o.ship for o in offers} == {"Reflection", "Summit"}


class TestFetch:
    def test_fetch_paginates_and_sends_expected_params(self):
        requests: list[httpx.Request] = []
        page = FIXTURE_TEXT

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if int(request.url.params["page"]) < 3:  # scraper pages start at 1
                return httpx.Response(200, text=page)
            return httpx.Response(
                200, text=json.dumps({"error": {"code": 107, "msg": "EMPTY RESULT"}})
            )

        scraper = Cruiseportal24Scraper(transport=httpx.MockTransport(handler))
        scraper.min_request_interval = 0
        offers = asyncio.run(scraper.fetch())

        assert len(offers) == 10  # two full pages of 5 valid items, then it stops
        assert len(requests) == 3
        assert all(r.headers["X-Requested-With"] == "XMLHttpRequest" for r in requests)
        assert all(str(r.url).startswith(API_URL) for r in requests)
        assert all(r.url.params["aid"] == "203142" for r in requests)
        assert all(
            r.url.params["url"] == "Meer/all/all/all/all/all/all" for r in requests
        )

    def test_fetch_respects_max_pages(self):
        scraper = Cruiseportal24Scraper(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, text=FIXTURE_TEXT))
        )
        scraper.min_request_interval = 0
        offers = asyncio.run(scraper.fetch())
        assert len(offers) == 5 * scraper.max_pages


class TestLLMFallback:
    def test_fallback_on_unparseable_response(self, monkeypatch):
        calls = {}

        def fake_extract(text, source):
            calls["source"] = source
            return []

        monkeypatch.setattr("app.llm.extract_offers", fake_extract)
        not_json = "<html><body>" + "<p>maintenance page</p>" * 50 + "</body></html>"
        Cruiseportal24Scraper().parse_with_fallback(not_json)
        assert calls["source"] == "cruiseportal24"

    def test_no_fallback_when_parse_succeeds(self, monkeypatch):
        monkeypatch.setattr(
            "app.llm.extract_offers",
            lambda *a: pytest.fail("LLM fallback must not run when parse works"),
        )
        assert len(Cruiseportal24Scraper().parse_with_fallback(FIXTURE_TEXT)) == 5

    def test_no_fallback_on_empty_result_error(self, monkeypatch):
        # EMPTY RESULT is a legitimate "no offers" response, not a parse
        # failure — it must not burn an LLM call.
        monkeypatch.setattr(
            "app.llm.extract_offers",
            lambda *a: pytest.fail("LLM fallback must not run for EMPTY RESULT"),
        )
        error_response = json.dumps({"error": {"code": 107, "msg": "EMPTY RESULT"}})
        assert Cruiseportal24Scraper().parse_with_fallback(error_response) == []


class TestRegistry:
    def test_cruiseportal24_is_registered(self):
        assert Cruiseportal24Scraper in SCRAPERS
