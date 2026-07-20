from decimal import Decimal
from types import SimpleNamespace

from app.matching import (
    CARIBBEAN,
    MEDITERRANEAN,
    NORTHERN_EUROPE,
    deal_matches_user,
    normalize_port,
    parse_departure_prefs,
    port_region,
)


def user(**overrides):
    defaults = dict(
        budget_per_night_max=None,
        trip_length_pref=None,
        departure_prefs=None,
        passport_country=None,
    )
    return SimpleNamespace(**{**defaults, **overrides})


def matches(u, *, nights=7, ppn="80", port="Hamburg", countries=None):
    return deal_matches_user(
        u,
        nights=nights,
        price_per_night=Decimal(ppn),
        departure_port=port,
        countries=countries or set(),
    )


class TestPortRegion:
    def test_plain_port(self):
        assert port_region("Hamburg") == NORTHERN_EUROPE

    def test_port_with_state_suffix(self):
        assert port_region("Fort Lauderdale, Florida") == CARIBBEAN

    def test_port_with_parenthetical(self):
        assert port_region("Palma, Mallorca (Balearen)") == MEDITERRANEAN
        assert port_region("Genua (Portofino)") == MEDITERRANEAN

    def test_unknown_port(self):
        assert port_region("Ushuaia") is None

    def test_normalize(self):
        assert normalize_port("Long Beach (Los Angeles), Kalifornien") == "long beach"


class TestParsePrefs:
    def test_none_and_empty(self):
        assert parse_departure_prefs(None) is None
        assert parse_departure_prefs("") is None

    def test_csv(self):
        assert parse_departure_prefs("mediterranean,caribbean") == {
            "mediterranean",
            "caribbean",
        }


class TestDealMatchesUser:
    def test_no_prefs_matches_everything(self):
        assert matches(user())

    def test_budget_filter(self):
        u = user(budget_per_night_max=Decimal("60"))
        assert matches(u, ppn="59")
        assert matches(u, ppn="60")  # inclusive: <= budget
        assert not matches(u, ppn="61")

    def test_trip_length_filter(self):
        u = user(trip_length_pref="2-4")
        assert matches(u, nights=3)
        assert not matches(u, nights=7)
        assert matches(user(trip_length_pref="10+"), nights=14)
        assert not matches(user(trip_length_pref="10+"), nights=9)

    def test_departure_prefs_filter(self):
        u = user(departure_prefs="mediterranean")
        assert matches(u, port="Barcelona")
        assert not matches(u, port="Hamburg")

    def test_unknown_port_fails_for_pref_setting_users_only(self):
        picky = user(departure_prefs="mediterranean")
        assert not matches(picky, port="Ushuaia")  # conservative: no guessing
        assert matches(user(), port="Ushuaia")  # no pref -> fine

    def test_ru_passport_requires_visa_free(self):
        ru = user(passport_country="RU")
        assert matches(ru, countries={"TR"})
        assert not matches(ru, countries={"TR", "GR"})
        assert not matches(ru, countries=set())  # unknown -> conservative

    def test_non_ru_passports_ignore_visa(self):
        for passport in ("EU", "UK", "KZ", "other", None):
            assert matches(user(passport_country=passport), countries=set())

    def test_all_filters_combined(self):
        u = user(
            budget_per_night_max=Decimal("120"),
            trip_length_pref="5-9",
            departure_prefs="mediterranean,northern_europe",
            passport_country="RU",
        )
        assert matches(u, nights=7, ppn="100", port="Barcelona", countries={"TR"})
        assert not matches(u, nights=7, ppn="130", port="Barcelona", countries={"TR"})
        assert not matches(u, nights=12, ppn="100", port="Barcelona", countries={"TR"})
        assert not matches(u, nights=7, ppn="100", port="Miami", countries={"TR"})
        assert not matches(u, nights=7, ppn="100", port="Barcelona", countries={"GR"})
