import asyncio
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.scrapers import SCRAPERS
from app.scrapers.dreamlines import (
    DreamlinesScraper,
    parse_german_date,
    parse_german_price,
    parse_nights,
)

FIXTURE_HTML = (Path(__file__).parent / "fixtures" / "dreamlines.html").read_text()


class TestGermanFormatHelpers:
    def test_price_with_thousands_dot(self):
        assert parse_german_price("ab 1.499 € p.P.") == Decimal("1499")

    def test_price_with_cents(self):
        assert parse_german_price("899,00 €") == Decimal("899.00")

    def test_price_plain(self):
        assert parse_german_price("549 €") == Decimal("549")

    def test_price_missing_raises(self):
        with pytest.raises(ValueError):
            parse_german_price("Preis auf Anfrage")

    def test_numeric_date(self):
        assert parse_german_date("12.09.2026") == date(2026, 9, 12)

    def test_month_name_date(self):
        assert parse_german_date("14. Aug. 2026") == date(2026, 8, 14)

    def test_nights(self):
        assert parse_nights("7 Nächte") == 7


class TestParseFixture:
    def test_parses_valid_cards_and_skips_broken_one(self):
        offers = DreamlinesScraper().parse(FIXTURE_HTML)
        # fixture has 6 cards; one has no price and must be skipped
        assert len(offers) == 5
        assert all(o.source == "dreamlines" for o in offers)
        assert "AIDAperla" not in {o.ship for o in offers}

    def test_first_offer_fields(self):
        offer = DreamlinesScraper().parse(FIXTURE_HTML)[0]
        assert offer.ship == "AIDAcosma"
        assert offer.cruise_line == "AIDA Cruises"
        assert offer.title == "Mittelmeer mit Ibiza"
        assert offer.nights == 7
        assert offer.departure_port == "Palma de Mallorca"  # "ab/bis " stripped
        assert offer.departure_date == date(2026, 9, 12)
        assert offer.price_eur == Decimal("1499")
        assert offer.url == "https://www.dreamlines.de/kreuzfahrt/118234-mittelmeer-mit-ibiza"

    def test_month_name_card(self):
        offers = DreamlinesScraper().parse(FIXTURE_HTML)
        mein_schiff = next(o for o in offers if o.ship == "Mein Schiff 7")
        assert mein_schiff.departure_date == date(2026, 8, 14)
        assert mein_schiff.nights == 10


class TestLLMFallback:
    def test_fallback_on_zero_offers_from_nonempty_html(self, monkeypatch):
        calls = {}

        def fake_extract(html, source):
            calls["source"] = source
            return []

        monkeypatch.setattr("app.llm.extract_offers", fake_extract)
        html = "<html><body>" + "<p>Kreuzfahrten Angebote</p>" * 100 + "</body></html>"
        DreamlinesScraper().parse_with_fallback(html)
        assert calls["source"] == "dreamlines"

    def test_no_fallback_when_css_parse_succeeds(self, monkeypatch):
        monkeypatch.setattr(
            "app.llm.extract_offers",
            lambda *a: pytest.fail("LLM fallback must not run when CSS parse works"),
        )
        offers = DreamlinesScraper().parse_with_fallback(FIXTURE_HTML)
        assert len(offers) == 5

    def test_no_fallback_for_genuinely_empty_page(self, monkeypatch):
        monkeypatch.setattr(
            "app.llm.extract_offers",
            lambda *a: pytest.fail("LLM fallback must not run for empty pages"),
        )
        assert DreamlinesScraper().parse_with_fallback("") == []


def make_fast_scraper(handler) -> DreamlinesScraper:
    scraper = DreamlinesScraper(transport=httpx.MockTransport(handler))
    scraper.min_request_interval = 0  # no throttling in tests
    scraper.backoff_base = 0  # no backoff sleeps in tests
    return scraper


class TestRetry:
    def test_retries_on_429_then_succeeds(self):
        attempts = []

        def handler(request):
            attempts.append(request.url)
            if len(attempts) < 3:
                return httpx.Response(429)
            return httpx.Response(200, text="ok")

        scraper = make_fast_scraper(handler)
        assert asyncio.run(scraper.http_get("https://example.com")) == "ok"
        assert len(attempts) == 3

    def test_retries_on_500(self):
        attempts = []

        def handler(request):
            attempts.append(1)
            if len(attempts) == 1:
                return httpx.Response(503)
            return httpx.Response(200, text="ok")

        scraper = make_fast_scraper(handler)
        assert asyncio.run(scraper.http_get("https://example.com")) == "ok"
        assert len(attempts) == 2

    def test_gives_up_after_max_retries(self):
        attempts = []

        def handler(request):
            attempts.append(1)
            return httpx.Response(500)

        scraper = make_fast_scraper(handler)
        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(scraper.http_get("https://example.com"))
        assert len(attempts) == scraper.max_retries + 1

    def test_no_retry_on_plain_4xx(self):
        attempts = []

        def handler(request):
            attempts.append(1)
            return httpx.Response(404)

        scraper = make_fast_scraper(handler)
        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(scraper.http_get("https://example.com"))
        assert len(attempts) == 1


class TestRegistry:
    def test_dreamlines_is_not_registered_while_cloudflare_blocked(self):
        assert DreamlinesScraper not in SCRAPERS
