"""Seed fake price history to test alerts without waiting 30 days.
Run with: python -m app.seed  (or: make seed)

Creates three cruises under source='seed':
- AIDAnova     — 30 days stable at 1499€, fresh snapshot 199€  -> -87% alert
- MSC Euribia  — stable at 399€ / 7 nights = 57€/night          -> per-night alert
- Mein Schiff 4 — stable at 1099€, no drop                      -> no alert

Then runs the detector and pushes the alerts through the normal send path
(printed to the console if Telegram isn't configured). Re-runnable: wipes
previous source='seed' rows first.
"""

import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.alerts import OutgoingAlert, format_alert, get_alert_chat_ids, send_alerts
from app.db import session_scope
from app.detector import detect_hot_deals
from app.jobs import upsert_cruise
from app.models import AlertSent, Cruise, PriceSnapshot
from app.scrapers import CruiseOffer

log = logging.getLogger(__name__)

SOURCE = "seed"

SEED_OFFERS: list[tuple[CruiseOffer, Decimal]] = [
    # (offer with the FRESH price, stable price for the 30-day history)
    (
        CruiseOffer(
            source=SOURCE,
            cruise_line="AIDA",
            ship="AIDAnova",
            title="Metropolen ab Hamburg",
            departure_port="Hamburg",
            departure_date=date(2026, 9, 12),
            nights=7,
            url="https://example.com/seed/aidanova",
            cabin_type="inside",
            price_eur=Decimal("199"),
        ),
        Decimal("1499"),
    ),
    (
        CruiseOffer(
            source=SOURCE,
            cruise_line="MSC",
            ship="MSC Euribia",
            title="Nordeuropa ab Kiel",
            departure_port="Kiel",
            departure_date=date(2026, 8, 22),
            nights=7,
            url="https://example.com/seed/msc-euribia",
            cabin_type="inside",
            price_eur=Decimal("399"),
        ),
        Decimal("399"),
    ),
    (
        CruiseOffer(
            source=SOURCE,
            cruise_line="TUI Cruises",
            ship="Mein Schiff 4",
            title="Ostsee ab Warnemünde",
            departure_port="Warnemünde",
            departure_date=date(2026, 8, 30),
            nights=9,
            url="https://example.com/seed/mein-schiff-4",
            cabin_type="inside",
            price_eur=Decimal("1099"),
        ),
        Decimal("1099"),
    ),
]


def wipe_previous_seed(session: Session) -> None:
    ids = list(
        session.execute(select(Cruise.id).where(Cruise.source == SOURCE)).scalars()
    )
    if not ids:
        return
    # explicit deletes instead of relying on DB-level cascade, so this also
    # works on SQLite (where the FK pragma is off by default)
    session.execute(delete(AlertSent).where(AlertSent.cruise_id.in_(ids)))
    session.execute(delete(PriceSnapshot).where(PriceSnapshot.cruise_id.in_(ids)))
    session.execute(delete(Cruise).where(Cruise.id.in_(ids)))


def seed(session: Session, now: datetime) -> None:
    wipe_previous_seed(session)
    for offer, stable_price in SEED_OFFERS:
        cruise = upsert_cruise(session, offer)
        # 30 days of history, one snapshot every 12h, at the stable price
        for step in range(1, 61):
            session.add(
                PriceSnapshot(
                    cruise_id=cruise.id,
                    cabin_type=offer.cabin_type,
                    price_eur=stable_price,
                    scraped_at=now - timedelta(hours=12 * step),
                )
            )
        # the fresh snapshot at the offer's (possibly dropped) price
        session.add(
            PriceSnapshot(
                cruise_id=cruise.id,
                cabin_type=offer.cabin_type,
                price_eur=offer.price_eur,
                scraped_at=now,
            )
        )


async def main() -> None:
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        seed(session, now)
        deals = detect_hot_deals(session, since=now - timedelta(seconds=1))
        chat_ids = get_alert_chat_ids(session)
    print(f"Seeded {len(SEED_OFFERS)} cruises with 30 days of history.")
    print(f"Detector flagged {len(deals)} hot deal(s):\n")
    for deal in deals:
        print(format_alert(deal))
        print()
    await send_alerts([OutgoingAlert(deal=d) for d in deals], chat_ids)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(main())
