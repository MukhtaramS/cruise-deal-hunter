from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import settings
from app.jobs import evaluate_deals
from app.models import AlertSent, Base, Cruise, PriceSnapshot, RouteCountries
from app.profiles import PROFILES
from app.visa import VISA_FREE_RU, all_visa_free, ensure_countries, get_cached_countries

NOW = datetime.now(timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


class TestAllVisaFree:
    def test_all_ports_visa_free(self):
        assert all_visa_free({"TR", "ME"}, VISA_FREE_RU)

    def test_one_schengen_port_fails(self):
        assert not all_visa_free({"TR", "GR"}, VISA_FREE_RU)

    def test_unknown_countries_fail_conservatively(self):
        assert not all_visa_free(set(), VISA_FREE_RU)


class TestEnsureCountries:
    def test_cached_routes_do_not_hit_llm(self, session, monkeypatch):
        session.add(RouteCountries(route_hash="r1", countries="TR,ME"))
        monkeypatch.setattr(
            "app.llm.infer_countries",
            lambda items: pytest.fail("LLM must not be called for cached routes"),
        )
        result = ensure_countries(session, [("r1", "Adria ab Istanbul", "Ship", "Istanbul")])
        assert result == {"r1": {"TR", "ME"}}

    def test_missing_routes_are_inferred_and_cached(self, session, monkeypatch):
        monkeypatch.setattr("app.llm.infer_countries", lambda items: [["TR"], []])
        result = ensure_countries(
            session,
            [("r1", "Türkei intensiv", "S1", "Istanbul"), ("r2", "???", "S2", "Nowhere")],
        )
        assert result == {"r1": {"TR"}, "r2": set()}
        cached = get_cached_countries(session, {"r1", "r2"})
        assert cached == {"r1": {"TR"}, "r2": set()}  # unknown cached as empty

    def test_llm_failure_caches_nothing(self, session, monkeypatch):
        def boom(items):
            raise RuntimeError("groq down")

        monkeypatch.setattr("app.llm.infer_countries", boom)
        result = ensure_countries(session, [("r1", "T", "S", "P")])
        assert result == {}
        assert get_cached_countries(session, {"r1"}) == {}  # eligible for retry


def make_cruise_with_drop(session, route_hash, suffix, price="199"):
    cruise = Cruise(
        source="test",
        cruise_line="AIDA",
        ship=f"Ship {suffix}",
        title=f"Cruise {suffix}",
        route_hash=route_hash,
        departure_port="Port",
        departure_date=date(2026, 9, 12),
        nights=7,
        url=f"https://example.com/{suffix}",
    )
    session.add(cruise)
    session.flush()
    for step in range(1, 61):
        session.add(
            PriceSnapshot(
                cruise_id=cruise.id,
                cabin_type="inside",
                price_eur=Decimal("1499"),
                scraped_at=NOW - timedelta(hours=12 * step),
            )
        )
    session.add(
        PriceSnapshot(
            cruise_id=cruise.id,
            cabin_type="inside",
            price_eur=Decimal(price),
            scraped_at=NOW,
        )
    )
    return cruise


def detect_candidates(session):
    from app.detector import find_hot_deals

    return find_hot_deals(session, since=NOW - timedelta(seconds=1))


class TestEvaluateDeals:
    def test_default_profile_ignores_visa_and_skips_llm(self, session, monkeypatch):
        monkeypatch.setattr(
            "app.llm.infer_countries",
            lambda items: pytest.fail("no LLM calls for the default profile"),
        )
        make_cruise_with_drop(session, "r1", "a")
        alerts = evaluate_deals(session, detect_candidates(session), [PROFILES["default"]])
        assert len(alerts) == 1
        assert alerts[0].visa_free is False

    def test_visa_ru_only_alerts_fully_visa_free_routes(self, session, monkeypatch):
        make_cruise_with_drop(session, "r_tr", "turkey")
        make_cruise_with_drop(session, "r_us", "usa")
        monkeypatch.setattr(
            "app.llm.infer_countries",
            lambda items: [["TR"] if "turkey" in i["title"] else ["US"] for i in items],
        )
        alerts = evaluate_deals(session, detect_candidates(session), [PROFILES["visa_ru"]])
        assert len(alerts) == 1
        assert alerts[0].deal.route_hash == "r_tr"
        assert alerts[0].visa_free is True

    def test_all_profiles_send_one_message_recorded_for_both(self, session, monkeypatch):
        make_cruise_with_drop(session, "r_tr", "turkey")
        monkeypatch.setattr("app.llm.infer_countries", lambda items: [["TR"]])
        profiles = [PROFILES["default"], PROFILES["visa_ru"]]
        alerts = evaluate_deals(session, detect_candidates(session), profiles)
        assert len(alerts) == 1  # one message, badge instead of duplicate
        assert alerts[0].visa_free is True
        recorded = set(
            session.execute(select(AlertSent.profile)).scalars()
        )
        assert recorded == {"default", "visa_ru"}

    def test_profiles_do_not_suppress_each_other(self, session, monkeypatch):
        cruise = make_cruise_with_drop(session, "r_tr", "turkey")
        # default profile alerted this price earlier; visa_ru never did
        session.add(
            AlertSent(cruise_id=cruise.id, price_eur=Decimal("199"), profile="default")
        )
        monkeypatch.setattr("app.llm.infer_countries", lambda items: [["TR"]])
        alerts = evaluate_deals(
            session, detect_candidates(session), [PROFILES["visa_ru"]]
        )
        assert len(alerts) == 1  # visa_ru's dedup is independent of default's

    def test_non_visa_free_deal_under_all_recorded_only_for_default(
        self, session, monkeypatch
    ):
        make_cruise_with_drop(session, "r_us", "usa")
        monkeypatch.setattr("app.llm.infer_countries", lambda items: [["US"]])
        profiles = [PROFILES["default"], PROFILES["visa_ru"]]
        alerts = evaluate_deals(session, detect_candidates(session), profiles)
        assert len(alerts) == 1
        assert alerts[0].visa_free is False
        recorded = set(session.execute(select(AlertSent.profile)).scalars())
        assert recorded == {"default"}
