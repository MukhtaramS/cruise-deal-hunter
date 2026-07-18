"""Scraper/scheduler service entrypoint: runs a scrape cycle immediately on
start, then every SCRAPE_INTERVAL_HOURS (default 4)."""

import asyncio
import logging
from datetime import datetime, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import settings
from app.jobs import run_scrape

log = logging.getLogger(__name__)


async def main() -> None:
    scheduler = AsyncIOScheduler(timezone="Europe/Berlin")
    scheduler.add_job(
        run_scrape,
        "interval",
        hours=settings.scrape_interval_hours,
        next_run_time=datetime.now(timezone.utc),  # fire once at startup
        coalesce=True,
        max_instances=1,
    )
    scheduler.start()
    log.info("Scheduler started — scraping every %dh", settings.scrape_interval_hours)
    await asyncio.Event().wait()  # run forever


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.run(main())
