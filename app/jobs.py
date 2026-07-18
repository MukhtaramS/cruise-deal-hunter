"""The scrape pipeline: scrape (per active profiles) -> detect -> evaluate
per profile -> alert. `python -m app.jobs` runs one cycle; the scheduler runs
it every 4 hours. PROFILE selects the configuration (see app/profiles.py)."""

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.alerts import OutgoingAlert, get_alert_chat_ids, send_alerts
from app.db import session_scope
from app.detector import HotDeal, find_hot_deals, latest_snapshots
from app.models import AlertSent, Cruise, PriceSnapshot
from app.profiles import Profile, active_profiles, wanted_sources
from app.scrapers import SCRAPERS, CruiseOffer
from app.visa import all_visa_free, ensure_countries

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


def evaluate_deals(
    session: Session, deals: list[HotDeal], profiles: list[Profile]
) -> list[OutgoingAlert]:
    """Decide, per profile, which candidate deals get alerted; record them in
    alerts_sent (per profile). Each deal produces at most ONE OutgoingAlert
    even when several profiles pass — visa passage becomes a badge, not a
    duplicate message."""
    visa_active = any(p.visa_filter for p in profiles)
    countries: dict[str, set[str]] = {}
    if visa_active and deals:
        countries = ensure_countries(
            session,
            [(d.route_hash, d.title, d.ship, d.departure_port) for d in deals],
        )

    from app.deals import alerted_at_or_below

    outgoing: list[OutgoingAlert] = []
    for deal in deals:
        route_countries = countries.get(deal.route_hash, set())
        passing = [
            p
            for p in profiles
            if p.visa_filter is None or all_visa_free(route_countries, p.visa_filter)
        ]
        to_record = [
            p
            for p in passing
            if not alerted_at_or_below(session, deal.cruise_id, deal.price_eur, p.name)
        ]
        if not to_record:
            continue
        for p in to_record:
            session.add(
                AlertSent(
                    cruise_id=deal.cruise_id, price_eur=deal.price_eur, profile=p.name
                )
            )
        visa_free = any(p.visa_filter is not None for p in passing)
        outgoing.append(OutgoingAlert(deal=deal, visa_free=visa_free))
    return outgoing


def infer_fresh_route_countries(session: Session, since: datetime) -> None:
    """Populate the route_countries cache for every cruise scraped this run,
    so /visafree has data beyond just the hot deals."""
    routes = [
        (c.route_hash, c.title, c.ship, c.departure_port)
        for snap in latest_snapshots(session, since=since)
        for c in [snap.cruise]
    ]
    ensure_countries(session, routes)


async def run_scrape() -> None:
    """One full cycle: scrape the active profiles' sources -> store snapshots
    -> detect -> per-profile filter/dedup -> send Telegram alerts."""
    profiles = active_profiles()
    run_started = datetime.now(timezone.utc)
    log.info(
        "Scrape cycle started (profiles: %s, %d scraper(s) registered)",
        ", ".join(p.name for p in profiles), len(SCRAPERS),
    )
    offers = await collect_offers(wanted_sources(profiles))
    with session_scope() as session:
        store_snapshots(session, offers, scraped_at=run_started)
        if any(p.visa_filter for p in profiles):
            infer_fresh_route_countries(session, since=run_started)
        candidates = find_hot_deals(session, since=run_started)
        alerts = evaluate_deals(session, candidates, profiles)
        chat_ids = get_alert_chat_ids(session)
    await send_alerts(alerts, chat_ids)
    log.info(
        "Scrape cycle done: %d offers, %d candidate deal(s), %d alert(s) sent",
        len(offers), len(candidates), len(alerts),
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(run_scrape())
