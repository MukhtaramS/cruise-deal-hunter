"""Run a single scraper by name.

Examples:
  python -m app.scrape --source dreamlines --dry-run
  python -m app.scrape --source dreamlines --dry-run --file tests/fixtures/dreamlines.html
  python -m app.scrape --source dreamlines          # store + detect + alert

--dry-run prints parsed offers without touching the DB or Telegram.
--file parses a local HTML save instead of fetching the live site.
"""

import argparse
import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from app.scrapers import SCRAPERS, BaseScraper, CruiseOffer

log = logging.getLogger(__name__)


def find_scraper(source: str) -> BaseScraper:
    for cls in SCRAPERS:
        if cls.source == source:
            return cls()
    known = ", ".join(cls.source for cls in SCRAPERS) or "(none registered)"
    raise SystemExit(f"unknown source {source!r} — known sources: {known}")


def print_offers(offers: list[CruiseOffer]) -> None:
    for offer in offers:
        print(
            f"{offer.ship} — {offer.title}\n"
            f"  {offer.cruise_line} | {offer.nights} nights | "
            f"ab {offer.departure_port} | {offer.departure_date:%d.%m.%Y}\n"
            f"  {offer.price_eur}€ ({offer.cabin_type}, "
            f"{offer.price_per_night:.0f}€/night)\n"
            f"  {offer.url}"
        )
    print(f"\n{len(offers)} offer(s) parsed")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run one scraper")
    parser.add_argument("--source", required=True, help="scraper slug, e.g. dreamlines")
    parser.add_argument(
        "--dry-run", action="store_true", help="print offers, no DB writes, no alerts"
    )
    parser.add_argument(
        "--file", type=Path, help="parse a local HTML file instead of fetching"
    )
    args = parser.parse_args()

    scraper = find_scraper(args.source)
    if args.file:
        offers = scraper.parse_with_fallback(args.file.read_text())
    else:
        offers = await scraper.fetch()

    if args.dry_run:
        print_offers(offers)
        print("dry run — nothing written")
        return

    # real run: same per-user pipeline as the scheduler, this source only
    from app.alerts import send_alerts
    from app.db import session_scope
    from app.detector import find_hot_deals
    from app.jobs import (
        infer_fresh_route_countries,
        load_recipients,
        needs_visa_data,
        route_deals,
        store_snapshots,
    )

    run_started = datetime.now(timezone.utc)
    with session_scope() as session:
        store_snapshots(session, offers, scraped_at=run_started)
        recipients = load_recipients(session)
        if needs_visa_data(recipients):
            infer_fresh_route_countries(session, since=run_started)
        candidates = find_hot_deals(session, since=run_started)
        deliveries = route_deals(session, candidates, recipients)
    await send_alerts(deliveries)
    log.info(
        "%s: %d offers stored, %d alert(s) sent",
        args.source, len(offers), sum(len(a) for a in deliveries.values()),
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(main())
