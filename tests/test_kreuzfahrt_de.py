import asyncio
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.scrapers import SCRAPERS
from app.scrapers.kreuzfahrt_de import (
    DEFAULT_VENDORS,
    FILTER_URL,
    ROUTES_URL,
    KreuzfahrtDeScraper,
    parse_vendor_map,
)

FIXTURES = Path(__file__).parent / "fixtures"
# real loadInfiniteScrolling response captured from the cruiseportal.de
# widget (ref=kreuzfahrt) on 2026-07-18: 5 cards, all MSC 1-night cruises
# (cheapest-first sort), incl. one with the "Genua - Marseille" harbor line
FIXTURE_TEXT = (FIXTURES / "kreuzfahrt_de.json").read_text()
# real loadFilterValues response (vendor id -> name map), same capture date
VENDORS_TEXT = (FIXTURES / "kreuzfahrt_de_vendors.json").read_text()


class TestParseVendorMap:
    def test_parses_real_response(self):
        vendors = parse_vendor_map(VENDORS_TEXT)
        assert len(vendors) == 49
        assert vendors["16"] == "MSC Cruises"
        assert vendors["1"] == "AIDA Cruises"
        assert vendors["133"] == "TUI Cruises (Mein Schiff)"

    def test_static_snapshot_matches_captured_response(self):
        # DEFAULT_VENDORS is documented as a real snapshot — keep it honest
        assert parse_vendor_map(VENDORS_TEXT) == DEFAULT_VENDORS


class TestParseFixture:
    def test_parses_all_cards(self):
        offers = KreuzfahrtDeScraper().parse(FIXTURE_TEXT)
        assert len(offers) == 5
        assert all(o.source == "kreuzfahrt_de" for o in offers)
        # vendor id 16 resolved via the (static) map, never the logo filename
        assert {o.cruise_line for o in offers} == {"MSC Cruises"}

    def test_first_offer_fields(self):
        offer = KreuzfahrtDeScraper().parse(FIXTURE_TEXT)[0]
        assert offer.ship == "MSC Virtuosa"
        assert offer.title == "Kurzkreuzfahrt"
        assert offer.nights == 1
        assert offer.departure_port == "Rio de Janeiro"  # harbor line, first stop
        assert offer.departure_date == date(2026, 12, 1)  # the "Gewählter Termin"
        assert offer.price_eur == Decimal("51")
        assert offer.cabin_type == "inside"
        # detail URL points back to kreuzfahrt.de, not the widget host
        assert offer.url == "https://www.kreuzfahrt.de/cruise/1330213"

    def test_gewaehlter_termin_wins_over_other_termine(self):
        # cards may list many alternative dates; the parsed date must be the
        # "Gewählter Termin" the shown price belongs to
        offers = KreuzfahrtDeScraper().parse(FIXTURE_TEXT)
        magnifica = next(o for o in offers if o.ship == "MSC Magnifica")
        assert magnifica.departure_date == date(2026, 10, 17)
        assert magnifica.departure_port == "Genua"

    def test_i_prefixed_vendor_logo_resolves(self):
        # seen live: "/vendor/i1-42-20200211.png" = vendor 42 (Princess
        # Cruises); the i1- prefix must not hide the id
        data = json.loads(FIXTURE_TEXT)
        data["htmlRouten"] = data["htmlRouten"].replace(
            "/vendor/16-20200103.png", "/vendor/i1-42-20200211.png"
        )
        offers = KreuzfahrtDeScraper().parse(json.dumps(data))
        assert len(offers) == 5
        assert {o.cruise_line for o in offers} == {"Princess Cruises"}

    def test_unknown_vendor_id_skips_card(self):
        data = json.loads(FIXTURE_TEXT)
        data["htmlRouten"] = data["htmlRouten"].replace("/vendor/16-", "/vendor/999999-")
        offers = KreuzfahrtDeScraper().parse(json.dumps(data))
        assert offers == []  # accurate data or nothing

    def test_empty_fragment_yields_no_offers(self):
        empty = json.dumps({"status": "ok", "htmlRouten": "", "intCount": 0})
        assert KreuzfahrtDeScraper().parse(empty) == []


class TestFetch:
    @staticmethod
    def run_fetch(handler):
        scraper = KreuzfahrtDeScraper(transport=httpx.MockTransport(handler))
        scraper.min_request_interval = 0
        return scraper, asyncio.run(scraper.fetch())

    def test_fetch_refreshes_vendors_then_paginates_by_offset(self):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if str(request.url).startswith(FILTER_URL):
                return httpx.Response(200, text=VENDORS_TEXT)
            if int(request.url.params["srcStartRoutes"]) < 10:
                return httpx.Response(200, text=FIXTURE_TEXT)
            return httpx.Response(
                200, text=json.dumps({"status": "ok", "htmlRouten": "", "intCount": 0})
            )

        scraper, offers = self.run_fetch(handler)

        assert len(offers) == 10  # two full batches of 5, then the empty one
        assert str(requests[0].url).startswith(FILTER_URL)  # vendor map first
        routes = [r for r in requests if str(r.url).startswith(ROUTES_URL)]
        assert [r.url.params["srcStartRoutes"] for r in routes] == ["0", "5", "10"]
        assert all(r.url.params["intParentSiteID"] == "27803" for r in routes)
        assert all(r.url.params["ref"] == "kreuzfahrt" for r in routes)

    def test_vendor_refresh_failure_falls_back_to_snapshot(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).startswith(FILTER_URL):
                return httpx.Response(404)
            if int(request.url.params["srcStartRoutes"]) == 0:
                return httpx.Response(200, text=FIXTURE_TEXT)
            return httpx.Response(
                200, text=json.dumps({"status": "ok", "htmlRouten": "", "intCount": 0})
            )

        scraper, offers = self.run_fetch(handler)
        # static DEFAULT_VENDORS still resolves vendor 16 -> MSC Cruises
        assert len(offers) == 5
        assert offers[0].cruise_line == "MSC Cruises"

    def test_fetch_respects_max_pages(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).startswith(FILTER_URL):
                return httpx.Response(200, text=VENDORS_TEXT)
            return httpx.Response(200, text=FIXTURE_TEXT)

        scraper, offers = self.run_fetch(handler)
        assert len(offers) == 5 * scraper.max_pages


class TestLLMFallback:
    def test_fallback_on_unparseable_response(self, monkeypatch):
        calls = {}

        def fake_extract(text, source):
            calls["source"] = source
            return []

        monkeypatch.setattr("app.llm.extract_offers", fake_extract)
        not_json = "<html><body>" + "<p>Wartungsarbeiten</p>" * 50 + "</body></html>"
        KreuzfahrtDeScraper().parse_with_fallback(not_json)
        assert calls["source"] == "kreuzfahrt_de"

    def test_no_fallback_when_parse_succeeds(self, monkeypatch):
        monkeypatch.setattr(
            "app.llm.extract_offers",
            lambda *a: pytest.fail("LLM fallback must not run when parse works"),
        )
        assert len(KreuzfahrtDeScraper().parse_with_fallback(FIXTURE_TEXT)) == 5


class TestRegistry:
    def test_kreuzfahrt_de_is_registered(self):
        assert KreuzfahrtDeScraper in SCRAPERS
