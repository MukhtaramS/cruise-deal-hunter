import asyncio
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.scrapers import SCRAPERS
from app.scrapers.cruise24 import (
    SEARCH_URL,
    Cruise24Scraper,
    cruise_line_from_detail_link,
    parse_price,
)

# real page saved from https://www.cruise24.de/kreuzfahrt/sort-price
# (2026-07-18), trimmed to 4 cards: one genuine all-cabins-"ausgebucht" card
# (MSC World Europa, id 137502) plus 3 bookable ones. Cabin tables include
# the "Yacht Club" and "Kind" rows that must never become offers.
FIXTURE_HTML = (Path(__file__).parent / "fixtures" / "cruise24.de.html").read_text()


class TestParsePrice:
    def test_dot_decimal(self):
        assert parse_price("ab 154.00€") == Decimal("154.00")

    def test_five_digits_no_thousands_separator(self):
        assert parse_price("ab 21995.00€") == Decimal("21995.00")

    def test_sold_out_raises(self):
        with pytest.raises(ValueError):
            parse_price("ausgebucht")

    def test_zero_price_raises(self):
        # "ab 0.00€" appears on real pages and must not become an offer
        with pytest.raises(ValueError):
            parse_price("ab 0.00€")


class TestCruiseLineFromDetailLink:
    def test_normal_link(self):
        href = "/details/137502/MSC-Cruises/MSC-World-Europa/Genoa,-Marseille,-Barcelona"
        assert cruise_line_from_detail_link(href) == "MSC Cruises"

    def test_unexpected_shape_raises(self):
        with pytest.raises(ValueError):
            cruise_line_from_detail_link("/kreuzfahrt/sort-price")


class TestParseFixture:
    def test_multiple_cabin_offers_per_card(self):
        offers = Cruise24Scraper().parse(FIXTURE_HTML)
        # 3 bookable cards -> 4 + 4 + 3 cabin offers; sold-out card skipped
        assert len(offers) == 11
        assert all(o.source == "cruise24" for o in offers)
        seaside = [o for o in offers if o.ship == "MSC Seaside"]
        assert [(o.cabin_type, o.price_eur) for o in seaside] == [
            ("inside", Decimal("154.00")),
            ("outside", Decimal("204.00")),
            ("balcony", Decimal("244.00")),
            ("suite", Decimal("578.00")),
        ]

    def test_all_sold_out_card_is_skipped(self):
        offers = Cruise24Scraper().parse(FIXTURE_HTML)
        # id 137502 (Genoa/Marseille/Barcelona, 22.04.2027) is fully sold out
        assert not any("137502" in o.url for o in offers)

    def test_yacht_club_and_kind_rows_never_become_offers(self):
        offers = Cruise24Scraper().parse(FIXTURE_HTML)
        assert {o.cabin_type for o in offers} <= {"inside", "outside", "balcony", "suite"}
        # the Kind (child) row on the Seaside card is 22.50€ — if it leaked
        # through it would falsely trigger the €/night hot-deal rule
        assert min(o.price_eur for o in offers) == Decimal("154.00")

    def test_first_offer_fields(self):
        offer = Cruise24Scraper().parse(FIXTURE_HTML)[0]
        assert offer.ship == "MSC Seaside"
        assert offer.cruise_line == "MSC Cruises"
        assert offer.departure_port == "Miami"  # "Miami\t USA, ..." -> "Miami"
        assert offer.departure_date == date(2027, 1, 8)
        assert offer.nights == 3
        assert offer.url == (
            "https://www.cruise24.de/details/133456/MSC-Cruises/MSC-Seaside/USA,-Bahamas"
        )

    def test_shared_url_across_cabin_offers(self):
        # one cruise, several cabin offers, same URL -> pipeline upserts one
        # cruise row and stores one snapshot per cabin
        offers = Cruise24Scraper().parse(FIXTURE_HTML)
        magnifica = [o for o in offers if o.ship == "MSC Magnifica"]
        assert len(magnifica) == 4
        assert len({o.url for o in magnifica}) == 1
        assert len({o.cabin_type for o in magnifica}) == 4


class TestFetch:
    def test_fetch_uses_offset_pagination(self):
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, text=FIXTURE_HTML)

        scraper = Cruise24Scraper(transport=httpx.MockTransport(handler))
        scraper.min_request_interval = 0
        offers = asyncio.run(scraper.fetch())

        assert len(offers) == 11 * scraper.max_pages
        # offset segment, not page number: first page bare, second /50
        assert str(requests[0].url) == SEARCH_URL
        assert str(requests[1].url) == f"{SEARCH_URL}/50"

    def test_fetch_stops_on_empty_page(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/50"):
                return httpx.Response(200, text="<html><body></body></html>")
            return httpx.Response(200, text=FIXTURE_HTML)

        scraper = Cruise24Scraper(transport=httpx.MockTransport(handler))
        scraper.min_request_interval = 0
        offers = asyncio.run(scraper.fetch())
        assert len(offers) == 11


class TestLLMFallback:
    def test_fallback_on_zero_offers_from_nonempty_html(self, monkeypatch):
        calls = {}

        def fake_extract(html, source):
            calls["source"] = source
            return []

        monkeypatch.setattr("app.llm.extract_offers", fake_extract)
        html = "<html><body>" + "<p>Kreuzfahrt Angebote</p>" * 100 + "</body></html>"
        Cruise24Scraper().parse_with_fallback(html)
        assert calls["source"] == "cruise24"

    def test_no_fallback_when_css_parse_succeeds(self, monkeypatch):
        monkeypatch.setattr(
            "app.llm.extract_offers",
            lambda *a: pytest.fail("LLM fallback must not run when CSS parse works"),
        )
        assert len(Cruise24Scraper().parse_with_fallback(FIXTURE_HTML)) == 11


class TestRegistry:
    def test_cruise24_is_registered(self):
        assert Cruise24Scraper in SCRAPERS
