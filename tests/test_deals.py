from datetime import date
from decimal import Decimal

from app.deals import is_hot_deal
from app.scrapers.base import CruiseOffer


def make_offer(**overrides) -> CruiseOffer:
    defaults = dict(
        source="portal-a",
        cruise_line="AIDA",
        ship="AIDAnova",
        title="Westeuropa ab Hamburg",
        departure_port="Hamburg",
        departure_date=date(2026, 9, 15),
        nights=7,
        url="https://example.com/offer/1",
        cabin_type="inside",
        price_eur=Decimal("799"),
    )
    return CruiseOffer(**{**defaults, **overrides})


class TestIsHotDeal:
    def test_big_drop_below_40_pct_of_median(self):
        assert is_hot_deal(Decimal("399"), median_30d=Decimal("1000"), nights=7)

    def test_price_at_40_pct_of_median_is_not_hot(self):
        # strictly below the threshold, not at it (5 nights keeps the
        # per-night rule out of the way: 400/5 = 80 EUR/night)
        assert not is_hot_deal(Decimal("400"), median_30d=Decimal("1000"), nights=5)

    def test_cheap_per_night_without_median(self):
        # 350 / 7 = 50 EUR/night < 60
        assert is_hot_deal(Decimal("350"), median_30d=None, nights=7)

    def test_per_night_at_threshold_is_not_hot(self):
        # 420 / 7 = 60 EUR/night, not strictly below
        assert not is_hot_deal(Decimal("420"), median_30d=None, nights=7)

    def test_normal_price_no_median(self):
        assert not is_hot_deal(Decimal("999"), median_30d=None, nights=7)

    def test_normal_price_with_median(self):
        assert not is_hot_deal(Decimal("900"), median_30d=Decimal("1000"), nights=7)


class TestRouteHash:
    def test_same_sailing_on_different_portals_matches(self):
        a = make_offer(source="portal-a", url="https://a.example/1", price_eur=Decimal("799"))
        b = make_offer(source="portal-b", url="https://b.example/xyz", price_eur=Decimal("749"))
        assert a.route_hash == b.route_hash

    def test_normalization_is_case_and_whitespace_insensitive(self):
        a = make_offer(ship="AIDAnova", departure_port="Hamburg")
        b = make_offer(ship=" aidanova ", departure_port="HAMBURG")
        assert a.route_hash == b.route_hash

    def test_different_departure_date_differs(self):
        a = make_offer()
        b = make_offer(departure_date=date(2026, 9, 22))
        assert a.route_hash != b.route_hash


class TestCruiseOffer:
    def test_price_per_night(self):
        offer = make_offer(price_eur=Decimal("700"), nights=7)
        assert offer.price_per_night == Decimal("100")

    def test_rejects_zero_nights(self):
        import pytest

        with pytest.raises(ValueError):
            make_offer(nights=0)
