from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.alerts import format_alert
from app.detector import HotDeal, detect_hot_deals
from app.models import AlertSent, Base, Cruise, PriceSnapshot

NOW = datetime.now(timezone.utc)


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def make_cruise(session, **overrides) -> Cruise:
    defaults = dict(
        source="portal-a",
        cruise_line="AIDA",
        ship="AIDAnova",
        title="Metropolen ab Hamburg",
        route_hash="abc123",
        departure_port="Hamburg",
        departure_date=date(2026, 9, 12),
        nights=7,
        url="https://example.com/offer/1",
    )
    cruise = Cruise(**{**defaults, **overrides})
    session.add(cruise)
    session.flush()
    return cruise


def add_history(session, cruise, price, days=30, every_hours=12):
    steps = days * 24 // every_hours
    for step in range(1, steps + 1):
        session.add(
            PriceSnapshot(
                cruise_id=cruise.id,
                cabin_type="inside",
                price_eur=Decimal(price),
                scraped_at=NOW - timedelta(hours=every_hours * step),
            )
        )


def add_fresh(session, cruise, price, at=NOW):
    session.add(
        PriceSnapshot(
            cruise_id=cruise.id,
            cabin_type="inside",
            price_eur=Decimal(price),
            scraped_at=at,
        )
    )


def detect(session, since=NOW - timedelta(seconds=1)):
    return detect_hot_deals(session, since=since)


class TestDetection:
    def test_price_drop_is_detected(self, session):
        cruise = make_cruise(session)
        add_history(session, cruise, "1499")
        add_fresh(session, cruise, "199")
        deals = detect(session)
        assert len(deals) == 1
        assert deals[0].discount_pct == 87
        assert deals[0].median_30d == Decimal("1499")

    def test_deal_is_recorded_in_alerts_sent(self, session):
        cruise = make_cruise(session)
        add_history(session, cruise, "1499")
        add_fresh(session, cruise, "199")
        detect(session)
        rows = session.execute(select(AlertSent)).scalars().all()
        assert len(rows) == 1
        assert rows[0].price_eur == Decimal("199")

    def test_normal_price_is_not_flagged(self, session):
        cruise = make_cruise(session)
        add_history(session, cruise, "1499")
        add_fresh(session, cruise, "1399")
        assert detect(session) == []

    def test_stale_snapshot_is_ignored(self, session):
        cruise = make_cruise(session)
        add_history(session, cruise, "1499")
        # the drop happened before `since` — some earlier run's business
        add_fresh(session, cruise, "199", at=NOW - timedelta(hours=6))
        assert detect(session) == []

    def test_per_night_rule_without_drop(self, session):
        cruise = make_cruise(session)
        add_history(session, cruise, "399")  # 399 / 7 = 57 EUR/night
        add_fresh(session, cruise, "399")
        deals = detect(session)
        assert len(deals) == 1
        assert deals[0].discount_pct == 0


class TestAlertDedup:
    def test_no_realert_at_same_price(self, session):
        cruise = make_cruise(session)
        add_history(session, cruise, "1499")
        add_fresh(session, cruise, "199")
        assert len(detect(session)) == 1
        assert detect(session) == []  # same snapshot, already alerted

    def test_no_realert_at_higher_but_still_hot_price(self, session):
        cruise = make_cruise(session)
        add_history(session, cruise, "1499")
        add_fresh(session, cruise, "199", at=NOW - timedelta(minutes=2))
        detect(session, since=NOW - timedelta(minutes=3))
        add_fresh(session, cruise, "299")  # still hot, but not a new low
        assert detect(session) == []

    def test_realert_on_new_low(self, session):
        cruise = make_cruise(session)
        add_history(session, cruise, "1499")
        add_fresh(session, cruise, "199", at=NOW - timedelta(minutes=2))
        detect(session, since=NOW - timedelta(minutes=3))
        add_fresh(session, cruise, "149")  # new low -> alert again
        deals = detect(session)
        assert len(deals) == 1
        assert deals[0].price_eur == Decimal("149")


class TestFormatAlert:
    def make_deal(self, **overrides) -> HotDeal:
        defaults = dict(
            cruise_id=1,
            title="Metropolen ab Hamburg",
            ship="AIDAnova",
            cruise_line="AIDA",
            departure_port="Hamburg",
            departure_date=date(2026, 9, 12),
            nights=7,
            cabin_type="inside",
            url="https://example.com/offer/1",
            source="portal-a",
            route_hash="abc123",
            price_eur=Decimal("199"),
            median_30d=Decimal("1499"),
        )
        return HotDeal(**{**defaults, **overrides})

    def test_matches_spec(self):
        text = format_alert(self.make_deal())
        lines = text.splitlines()
        assert lines[0] == "🔥 -87% | AIDAnova, 7 nights, Hamburg, 12.09"
        assert lines[1] == "199€ (was 1499€ median)"
        assert lines[2] == "https://example.com/offer/1"

    def test_per_night_variant_when_no_drop(self):
        text = format_alert(
            self.make_deal(price_eur=Decimal("399"), median_30d=Decimal("399"))
        )
        assert text.splitlines()[0] == "🔥 57€/night | AIDAnova, 7 nights, Hamburg, 12.09"
