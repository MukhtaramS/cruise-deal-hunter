from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.jobs import load_recipients, route_deals
from app.models import AlertSent, Base, Cruise, PriceSnapshot, RouteCountries, User
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


def make_cruise_with_drop(session, route_hash, suffix, price="199", port="Hamburg"):
    cruise = Cruise(
        source="test",
        cruise_line="AIDA",
        ship=f"Ship {suffix}",
        title=f"Cruise {suffix}",
        route_hash=route_hash,
        departure_port=port,
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


def make_user(session, chat_id, **prefs) -> User:
    user = User(chat_id=chat_id, onboarded_at=NOW, **prefs)
    session.add(user)
    session.flush()
    return user


class TestRouteDeals:
    def test_no_ru_user_means_no_llm_calls(self, session, monkeypatch):
        monkeypatch.setattr(
            "app.llm.infer_countries",
            lambda items: pytest.fail("no LLM calls without an RU-passport user"),
        )
        make_cruise_with_drop(session, "r1", "a")
        user = make_user(session, 111, passport_country="EU")
        deliveries = route_deals(session, detect_candidates(session), [user])
        assert len(deliveries[111]) == 1
        assert deliveries[111][0].visa_free is False

    def test_ru_user_only_gets_fully_visa_free_routes(self, session, monkeypatch):
        make_cruise_with_drop(session, "r_tr", "turkey")
        make_cruise_with_drop(session, "r_us", "usa")
        monkeypatch.setattr(
            "app.llm.infer_countries",
            lambda items: [["TR"] if "turkey" in i["title"] else ["US"] for i in items],
        )
        ru = make_user(session, 222, passport_country="RU")
        deliveries = route_deals(session, detect_candidates(session), [ru])
        assert len(deliveries[222]) == 1
        assert deliveries[222][0].deal.route_hash == "r_tr"
        assert deliveries[222][0].visa_free is True

    def test_users_do_not_suppress_each_other(self, session, monkeypatch):
        cruise = make_cruise_with_drop(session, "r1", "a")
        # user A already got this exact price level; user B never did
        session.add(
            AlertSent(cruise_id=cruise.id, price_eur=Decimal("199"), chat_id=111)
        )
        a = make_user(session, 111)
        b = make_user(session, 333)
        deliveries = route_deals(session, detect_candidates(session), [a, b])
        assert 111 not in deliveries  # deduped for A
        assert len(deliveries[333]) == 1  # B still gets it

    def test_alerts_recorded_per_user(self, session):
        make_cruise_with_drop(session, "r1", "a")
        a = make_user(session, 111)
        b = make_user(session, 333)
        route_deals(session, detect_candidates(session), [a, b])
        recorded = set(session.execute(select(AlertSent.chat_id)).scalars())
        assert recorded == {111, 333}


class TestLoadRecipients:
    def test_only_onboarded_users(self, session, monkeypatch):
        monkeypatch.setattr("app.jobs.settings.telegram_chat_id", "", raising=False)
        session.add(User(chat_id=1, onboarded_at=NOW))
        session.add(User(chat_id=2, onboarded_at=None))  # mid-onboarding
        session.flush()
        assert [r.chat_id for r in load_recipients(session)] == [1]

    def test_env_fallback_chat_added_once(self, session, monkeypatch):
        monkeypatch.setattr("app.jobs.settings.telegram_chat_id", "999", raising=False)
        session.add(User(chat_id=1, onboarded_at=NOW))
        session.flush()
        ids = sorted(r.chat_id for r in load_recipients(session))
        assert ids == [1, 999]

    def test_env_fallback_not_duplicated_when_user_exists(self, session, monkeypatch):
        monkeypatch.setattr("app.jobs.settings.telegram_chat_id", "1", raising=False)
        session.add(User(chat_id=1, onboarded_at=NOW))
        session.flush()
        assert [r.chat_id for r in load_recipients(session)] == [1]
