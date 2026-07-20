"""Hot-deal rules. Pure Python / SQL — no LLM involved.

A hot deal is either:
- current price < HOT_DEAL_MEDIAN_RATIO (default 40%) of the median price
  for that cruise + cabin type over the last 30 days, or
- price per night < HOT_DEAL_MAX_PRICE_PER_NIGHT (default 60 EUR).

The median is computed per cabin type, not per cruise overall — otherwise a
normal inside-cabin price would look like a "drop" against a median that
includes suites. It is computed in Python (statistics.median) rather than SQL
so the logic runs identically on Postgres and on SQLite in tests; snapshot
counts per cruise are tiny (≤ ~180 in a 30-day window).
"""

import statistics
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import AlertSent, PriceSnapshot


def is_hot_deal(price_eur: Decimal, median_30d: Decimal | None, nights: int) -> bool:
    if median_30d is not None and price_eur < Decimal(str(settings.hot_deal_median_ratio)) * median_30d:
        return True
    if nights > 0 and price_eur / nights < Decimal(str(settings.hot_deal_max_price_per_night)):
        return True
    return False


def median_price_last_30d(
    session: Session, cruise_id: int, cabin_type: str
) -> Decimal | None:
    since = datetime.now(timezone.utc) - timedelta(days=30)
    prices = list(
        session.execute(
            select(PriceSnapshot.price_eur).where(
                PriceSnapshot.cruise_id == cruise_id,
                PriceSnapshot.cabin_type == cabin_type,
                PriceSnapshot.scraped_at >= since,
            )
        ).scalars()
    )
    if not prices:
        return None
    return Decimal(statistics.median(prices))


def alerted_at_or_below(
    session: Session, cruise_id: int, price_eur: Decimal, chat_id: int = 0
) -> bool:
    """True if this user already got an alert for this cruise at the same or
    a lower price — only a fresh new low justifies another alert. Dedup is
    scoped per chat so users never suppress each other. chat_id 0 is the
    legacy/seed sentinel used by detect_hot_deals."""
    stmt = (
        select(AlertSent.cruise_id)
        .where(
            AlertSent.cruise_id == cruise_id,
            AlertSent.price_eur <= price_eur,
            AlertSent.chat_id == chat_id,
        )
        .limit(1)
    )
    return session.execute(stmt).first() is not None
