from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.bot import best_current_match
from app.models import Base, Cruise, PriceSnapshot, User

NOW = datetime.now(timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def add_cruise(session, *, ship, port, nights, price, days_ahead=60):
    cruise = Cruise(
        source="test",
        cruise_line="Line",
        ship=ship,
        title=f"{ship} trip",
        route_hash=f"rh-{ship}",
        departure_port=port,
        departure_date=date.today() + timedelta(days=days_ahead),
        nights=nights,
        url=f"https://example.com/{ship}",
    )
    session.add(cruise)
    session.flush()
    session.add(
        PriceSnapshot(
            cruise_id=cruise.id,
            cabin_type="inside",
            price_eur=Decimal(price),
            scraped_at=NOW,
        )
    )
    return cruise


def make_user(**prefs) -> User:
    defaults = dict(chat_id=1, onboarded_at=NOW)
    return User(**{**defaults, **prefs})


class TestBestCurrentMatch:
    def test_picks_cheapest_per_night_among_matching(self, session):
        add_cruise(session, ship="Cheap", port="Hamburg", nights=7, price="350")   # 50/n
        add_cruise(session, ship="Mid", port="Kiel", nights=7, price="700")        # 100/n
        deal = best_current_match(session, make_user())
        assert deal is not None and deal.ship == "Cheap"

    def test_respects_budget_and_region_prefs(self, session):
        add_cruise(session, ship="CheapCarib", port="Miami", nights=7, price="350")
        add_cruise(session, ship="MedPick", port="Barcelona", nights=7, price="700")
        user = make_user(departure_prefs="mediterranean")
        deal = best_current_match(session, user)
        assert deal is not None and deal.ship == "MedPick"

    def test_past_departures_are_ignored(self, session):
        add_cruise(session, ship="Gone", port="Hamburg", nights=7, price="70", days_ahead=-3)
        assert best_current_match(session, make_user()) is None

    def test_no_match_returns_none(self, session):
        add_cruise(session, ship="Pricey", port="Hamburg", nights=7, price="7000")
        user = make_user(budget_per_night_max=Decimal("60"))
        assert best_current_match(session, user) is None

    def test_sample_deal_carries_footer_data(self, session):
        add_cruise(session, ship="Cheap", port="Hamburg", nights=7, price="350")
        deal = best_current_match(session, make_user())
        assert deal.scraped_at is not None  # footer "Price checked X min ago"
