import asyncio
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.scrapers import SCRAPERS
from app.scrapers.kreuzfahrten import (
    SEARCH_URL,
    KreuzfahrtenScraper,
    cruise_line_from_logo_url,
    parse_price,
)

# real page saved from https://www.kreuzfahrten.de/termin/ (2026-07-18),
# trimmed to 6 real cards. Two of them (Carnival Radiance, Carnival Magic)
# are genuine sold-out/"Preis auf Anfrage" cards from the live site, kept to
# exercise the skip-no-price path — 4 cards have a real bookable price.
FIXTURE_HTML = (Path(__file__).parent / "fixtures" / "kreuzfahrten.de.html").read_text()


class TestParsePrice:
    def test_thousands_dot(self):
        assert parse_price("ab\xa0€\xa01.462,-") == Decimal("1462")

    def test_no_thousands(self):
        assert parse_price("€\xa0260,-") == Decimal("260")

    def test_missing_price_raises(self):
        with pytest.raises(ValueError):
            parse_price("Preis auf Anfrage")


class TestCruiseLineFromLogoUrl:
    def test_mixed_case_stem_kept_as_is(self):
        url = "https://www.kreuzfahrten.de/data/pictures/vendor/logo/Carnival-Cruise-Lines-13.png?w=90"
        assert cruise_line_from_logo_url(url) == "Carnival Cruise Lines"

    def test_short_mixed_case_stem(self):
        url = "https://www.kreuzfahrten.de/data/pictures/vendor/logo/Color-Line-53.png?w=90"
        assert cruise_line_from_logo_url(url) == "Color Line"

    def test_lowercase_stem_with_date_suffix_is_title_cased(self):
        url = "https://www.kreuzfahrten.de/data/pictures/vendor/logo/cunard-20-20210901-1.png?w=90"
        assert cruise_line_from_logo_url(url) == "Cunard"

    def test_lowercase_stem_with_abbreviation_artifact(self):
        # documented limitation: trailing non-numeric abbreviation survives
        url = "https://www.kreuzfahrten.de/data/pictures/vendor/logo/norwegian-cruise-line-ncl-11-20260107.png?w=90"
        assert cruise_line_from_logo_url(url) == "Norwegian Cruise Line Ncl"

    def test_i_prefix_is_stripped(self):
        # seen live: "i1-holland-america-line-..." produced the line name
        # "I1 Holland America Line" until the prefix was stripped
        url = "https://www.kreuzfahrten.de/data/pictures/vendor/logo/i1-holland-america-line-22-20240827.png?w=90"
        assert cruise_line_from_logo_url(url) == "Holland America Line"

    def test_alt_text_is_never_used(self):
        # regression guard: alt is sometimes empty or a generic placeholder
        # like "Impressionen" on real cards — must not leak into the name
        url = "https://www.kreuzfahrten.de/data/pictures/vendor/logo/royal-caribbean-international-23-20240827.png?w=90"
        assert cruise_line_from_logo_url(url) == "Royal Caribbean International"


class TestParseFixture:
    def test_skips_sold_out_cards_and_parses_the_rest(self):
        offers = KreuzfahrtenScraper().parse(FIXTURE_HTML)
        assert len(offers) == 4
        assert all(o.source == "kreuzfahrten" for o in offers)
        skipped = {"Carnival Radiance", "Carnival Magic"}
        assert skipped.isdisjoint({o.ship for o in offers})

    def test_first_offer_fields(self):
        offer = KreuzfahrtenScraper().parse(FIXTURE_HTML)[0]
        assert offer.ship == "Color Fantasy"
        assert offer.cruise_line == "Color Line"
        assert offer.departure_port == "Kiel"
        assert offer.departure_date == date(2026, 7, 19)
        assert offer.nights == 2
        assert offer.cabin_type == "inside"
        assert offer.price_eur == Decimal("260")
        assert offer.url == "https://www.kreuzfahrten.de/termin/1286696.html"

    def test_title_is_route_text_without_nights_prefix(self):
        offers = KreuzfahrtenScraper().parse(FIXTURE_HTML)
        titles = {o.ship: o.title for o in offers}
        assert titles["Color Fantasy"] == "Fantasy Cruise"
        assert "Nächte" not in titles["Color Fantasy"]


class TestFetch:
    def test_fetch_paginates_and_sends_expected_params(self):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if int(request.url.params["page"]) < 3:
                return httpx.Response(200, text=FIXTURE_HTML)
            return httpx.Response(200, text="<html><body></body></html>")

        scraper = KreuzfahrtenScraper(transport=httpx.MockTransport(handler))
        scraper.min_request_interval = 0
        offers = asyncio.run(scraper.fetch())

        assert len(offers) == 8  # two full pages of 4 valid cards, then it stops
        assert len(requests) == 3
        assert all(str(r.url).startswith(SEARCH_URL) for r in requests)
        assert all(r.url.params["per-page"] == "10" for r in requests)
        assert [r.url.params["page"] for r in requests] == ["1", "2", "3"]

    def test_fetch_respects_max_pages(self):
        scraper = KreuzfahrtenScraper(
            transport=httpx.MockTransport(lambda r: httpx.Response(200, text=FIXTURE_HTML))
        )
        scraper.min_request_interval = 0
        offers = asyncio.run(scraper.fetch())
        assert len(offers) == 4 * scraper.max_pages


class TestLLMFallback:
    def test_fallback_on_zero_offers_from_nonempty_html(self, monkeypatch):
        calls = {}

        def fake_extract(html, source):
            calls["source"] = source
            return []

        monkeypatch.setattr("app.llm.extract_offers", fake_extract)
        html = "<html><body>" + "<p>Kreuzfahrten Angebote</p>" * 100 + "</body></html>"
        KreuzfahrtenScraper().parse_with_fallback(html)
        assert calls["source"] == "kreuzfahrten"

    def test_no_fallback_when_css_parse_succeeds(self, monkeypatch):
        monkeypatch.setattr(
            "app.llm.extract_offers",
            lambda *a: pytest.fail("LLM fallback must not run when CSS parse works"),
        )
        offers = KreuzfahrtenScraper().parse_with_fallback(FIXTURE_HTML)
        assert len(offers) == 4

    def test_no_fallback_for_genuinely_empty_page(self, monkeypatch):
        monkeypatch.setattr(
            "app.llm.extract_offers",
            lambda *a: pytest.fail("LLM fallback must not run for empty pages"),
        )
        assert KreuzfahrtenScraper().parse_with_fallback("") == []


class TestRegistry:
    def test_kreuzfahrten_is_registered(self):
        assert KreuzfahrtenScraper in SCRAPERS
