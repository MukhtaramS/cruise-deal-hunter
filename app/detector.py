"""Post-scrape hot-deal detection.

Runs over DB state rather than in-memory offers: every cruise/cabin that got
a fresh snapshot this run is evaluated against its 30-day price history.
Detected deals are recorded in alerts_sent within the caller's transaction,
so detection and dedup commit (or roll back) together.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deals import alerted_at_or_below, is_hot_deal, median_price_last_30d
from app.models import AlertSent, PriceSnapshot


@dataclass(frozen=True)
class HotDeal:
    """Plain-value snapshot of a detected deal — safe to use after the DB
    session is closed."""

    cruise_id: int
    title: str
    ship: str
    cruise_line: str
    departure_port: str
    departure_date: date
    nights: int
    cabin_type: str
    url: str
    source: str
    route_hash: str
    price_eur: Decimal
    median_30d: Decimal | None

    @property
    def price_per_night(self) -> Decimal:
        return self.price_eur / self.nights

    @property
    def discount_pct(self) -> int | None:
        """Whole-percent discount vs the 30-day median (87 means -87%)."""
        if self.median_30d is None or self.median_30d == 0:
            return None
        return round((1 - self.price_eur / self.median_30d) * 100)


def latest_snapshots(session: Session, since: datetime | None = None) -> list[PriceSnapshot]:
    """Newest snapshot per (cruise, cabin_type), optionally only among
    snapshots scraped since `since`."""
    stmt = select(PriceSnapshot).order_by(
        PriceSnapshot.cruise_id,
        PriceSnapshot.cabin_type,
        PriceSnapshot.scraped_at.desc(),
    )
    if since is not None:
        stmt = stmt.where(PriceSnapshot.scraped_at >= since)
    latest: dict[tuple[int, str], PriceSnapshot] = {}
    for snap in session.execute(stmt).scalars():
        latest.setdefault((snap.cruise_id, snap.cabin_type), snap)
    return list(latest.values())


def find_hot_deals(session: Session, since: datetime) -> list[HotDeal]:
    """Pure detection: evaluate every fresh snapshot against the deal rules
    and return candidates. No dedup, no alerts_sent writes — that's the
    caller's job (profile-aware, see app/jobs.py:evaluate_deals)."""
    deals: list[HotDeal] = []
    for snap in latest_snapshots(session, since=since):
        cruise = snap.cruise
        median = median_price_last_30d(session, cruise.id, snap.cabin_type)
        if not is_hot_deal(snap.price_eur, median, cruise.nights):
            continue
        deals.append(
            HotDeal(
                cruise_id=cruise.id,
                title=cruise.title,
                ship=cruise.ship,
                cruise_line=cruise.cruise_line,
                departure_port=cruise.departure_port,
                departure_date=cruise.departure_date,
                nights=cruise.nights,
                cabin_type=snap.cabin_type,
                url=cruise.url,
                source=cruise.source,
                route_hash=cruise.route_hash,
                price_eur=snap.price_eur,
                median_30d=median,
            )
        )
    return deals


def detect_hot_deals(session: Session, since: datetime) -> list[HotDeal]:
    """Single-profile ('default') convenience: find deals, skip those already
    alerted at the same or a lower price, record the rest in alerts_sent.
    Used by the seed script; the scheduler pipeline uses find_hot_deals +
    jobs.evaluate_deals for profile-aware alerting."""
    deals: list[HotDeal] = []
    for deal in find_hot_deals(session, since):
        if alerted_at_or_below(session, deal.cruise_id, deal.price_eur, "default"):
            continue
        session.add(
            AlertSent(cruise_id=deal.cruise_id, price_eur=deal.price_eur, profile="default")
        )
        deals.append(deal)
    return deals
