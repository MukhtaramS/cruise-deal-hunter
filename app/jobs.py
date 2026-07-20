"""The scrape pipeline: scrape all sources -> detect -> route per user ->
alert. `python -m app.jobs` runs one cycle; the scheduler runs it every 4h.

Per-user routing replaced the old PROFILE system: every onboarded user gets
deals filtered by their own preferences (budget, trip length, departure
regions, passport/visa) with an independent dedup ledger per chat."""

import asyncio
import logging
from collections import defaultdict
from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.alerts import OutgoingAlert, send_alerts
from app.config import settings
from app.db import session_scope
from app.deals import alerted_at_or_below
from app.detector import HotDeal, find_hot_deals, latest_snapshots
from app.matching import deal_matches_user
from app.models import AlertSent, Cruise, PriceSnapshot, User
from app.scrapers import SCRAPERS, CruiseOffer
from app.visa import VISA_FREE_RU, all_visa_free, ensure_countries

log = logging.getLogger(__name__)


async def collect_offers(sources: set[str] | None = None) -> list[CruiseOffer]:
    """Run registered scrapers; `sources` limits to those slugs (None = all)."""
    offers: list[CruiseOffer] = []
    for scraper_cls in SCRAPERS:
        if sources is not None and scraper_cls.source not in sources:
            continue
        scraper = scraper_cls()
        try:
            found = await scraper.fetch()
            log.info("%s: %d offers", scraper.source, len(found))
            offers.extend(found)
        except Exception:
            log.exception("%s: scraper failed, skipping", scraper.source)
    return offers


def upsert_cruise(session: Session, offer: CruiseOffer) -> Cruise:
    cruise = session.execute(
        select(Cruise).where(Cruise.source == offer.source, Cruise.url == offer.url)
    ).scalar_one_or_none()
    if cruise is None:
        cruise = Cruise(
            source=offer.source,
            cruise_line=offer.cruise_line,
            ship=offer.ship,
            title=offer.title,
            route_hash=offer.route_hash,
            departure_port=offer.departure_port,
            departure_date=offer.departure_date,
            nights=offer.nights,
            url=offer.url,
        )
        session.add(cruise)
        session.flush()
    else:
        cruise.title = offer.title
        cruise.route_hash = offer.route_hash
    return cruise


def store_snapshots(
    session: Session, offers: list[CruiseOffer], scraped_at: datetime
) -> None:
    for offer in offers:
        cruise = upsert_cruise(session, offer)
        session.add(
            PriceSnapshot(
                cruise_id=cruise.id,
                cabin_type=offer.cabin_type,
                price_eur=offer.price_eur,
                scraped_at=scraped_at,
            )
        )


def load_recipients(session: Session) -> list:
    """Every onboarded user, plus TELEGRAM_CHAT_ID from .env as an
    unfiltered pseudo-user (backward compat) when it isn't already a user."""
    recipients: list = list(
        session.execute(select(User).where(User.onboarded_at.is_not(None))).scalars()
    )
    if settings.telegram_chat_id:
        try:
            env_chat = int(settings.telegram_chat_id)
        except ValueError:
            env_chat = None
        if env_chat and all(u.chat_id != env_chat for u in recipients):
            recipients.append(
                SimpleNamespace(
                    chat_id=env_chat,
                    first_name=None,
                    home_region=None,
                    passport_country=None,
                    budget_per_night_max=None,
                    trip_length_pref=None,
                    departure_prefs=None,
                )
            )
    return recipients


def needs_visa_data(recipients: list) -> bool:
    return any((r.passport_country or "").upper() == "RU" for r in recipients)


def route_deals(
    session: Session, deals: list[HotDeal], recipients: list
) -> dict[int, list[OutgoingAlert]]:
    """Match every candidate deal against every recipient's preferences,
    dedup per (cruise, price, chat) and record the ledger rows in the same
    transaction as the snapshots."""
    if not deals or not recipients:
        return {}
    countries: dict[str, set[str]] = {}
    if needs_visa_data(recipients):
        countries = ensure_countries(
            session,
            [(d.route_hash, d.title, d.ship, d.departure_port) for d in deals],
        )

    deliveries: dict[int, list[OutgoingAlert]] = defaultdict(list)
    for deal in deals:
        route_countries = countries.get(deal.route_hash, set())
        for user in recipients:
            if not deal_matches_user(
                user,
                nights=deal.nights,
                price_per_night=deal.price_per_night,
                departure_port=deal.departure_port,
                countries=route_countries,
            ):
                continue
            if alerted_at_or_below(session, deal.cruise_id, deal.price_eur, user.chat_id):
                continue
            session.add(
                AlertSent(
                    cruise_id=deal.cruise_id,
                    price_eur=deal.price_eur,
                    chat_id=user.chat_id,
                )
            )
            deliveries[user.chat_id].append(
                OutgoingAlert(
                    deal=deal,
                    visa_free=bool(route_countries)
                    and all_visa_free(route_countries, VISA_FREE_RU),
                )
            )
    return dict(deliveries)


def infer_fresh_route_countries(session: Session, since: datetime) -> None:
    """Populate the route_countries cache for every cruise scraped this run,
    so /visafree and RU-passport matching have data beyond just the hot deals."""
    routes = [
        (c.route_hash, c.title, c.ship, c.departure_port)
        for snap in latest_snapshots(session, since=since)
        for c in [snap.cruise]
    ]
    ensure_countries(session, routes)


async def run_scrape() -> None:
    """One full cycle: scrape all sources -> store snapshots -> detect ->
    per-user match/dedup -> send Telegram alerts."""
    run_started = datetime.now(timezone.utc)
    log.info("Scrape cycle started (%d scraper(s) registered)", len(SCRAPERS))
    offers = await collect_offers()
    with session_scope() as session:
        store_snapshots(session, offers, scraped_at=run_started)
        recipients = load_recipients(session)
        if needs_visa_data(recipients):
            infer_fresh_route_countries(session, since=run_started)
        candidates = find_hot_deals(session, since=run_started)
        deliveries = route_deals(session, candidates, recipients)
    await send_alerts(deliveries)
    log.info(
        "Scrape cycle done: %d offers, %d candidate deal(s), %d alert(s) to %d user(s)",
        len(offers), len(candidates),
        sum(len(a) for a in deliveries.values()), len(deliveries),
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run_scrape())
