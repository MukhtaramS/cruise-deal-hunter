"""Telegram bot service entrypoint. Run with: python -m app.bot

Commands:
  /start    — subscribe this chat (stores chat_id in telegram_chats)
  /status   — last scrape time, cruises tracked, deals found this week
  /top      — 5 cheapest per-night offers right now (future departures only)
  /visafree — 10 cheapest per-night cruises visiting ONLY countries that are
              visa-free for Russian passports (needs the visa_ru or "all"
              profile to have populated the route_countries cache)

Alerts themselves are sent by the scraper service (app/jobs.py) to every
subscribed chat; this process only answers commands.
"""

import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from app.config import settings
from app.db import session_scope
from app.detector import latest_snapshots
from app.models import AlertSent, Cruise, PriceSnapshot, TelegramChat
from app.visa import VISA_FREE_RU, all_visa_free, get_cached_countries

log = logging.getLogger(__name__)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    assert chat is not None
    with session_scope() as session:
        if session.get(TelegramChat, chat.id) is None:
            session.add(TelegramChat(chat_id=chat.id))
    await chat.send_message(
        "🚢 Subscribed — hot-deal alerts will land here.\n"
        "Commands: /status, /top, /visafree"
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    assert chat is not None
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    with session_scope() as session:
        last_scrape = session.execute(
            select(func.max(PriceSnapshot.scraped_at))
        ).scalar()
        cruises = session.execute(select(func.count(Cruise.id))).scalar_one()
        deals_week = session.execute(
            select(func.count())
            .select_from(AlertSent)
            .where(AlertSent.sent_at >= week_ago)
        ).scalar_one()
    last = f"{last_scrape:%d.%m.%Y %H:%M} UTC" if last_scrape else "never"
    await chat.send_message(
        f"Last scrape: {last}\n"
        f"Cruises tracked: {cruises}\n"
        f"Deals found this week: {deals_week}"
    )


async def top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    assert chat is not None
    today = date.today()
    with session_scope() as session:
        current = [
            snap
            for snap in latest_snapshots(session)
            if snap.cruise.departure_date >= today
        ]
        current.sort(key=lambda s: s.price_eur / s.cruise.nights)
        lines = ["🏆 Cheapest per night right now:"]
        for rank, snap in enumerate(current[:5], start=1):
            cruise = snap.cruise
            ppn = snap.price_eur / cruise.nights
            lines.append(
                f"{rank}. {ppn:.0f}€/night — {cruise.ship}, {cruise.nights} nights, "
                f"{cruise.departure_port}, {cruise.departure_date:%d.%m} — "
                f"{snap.price_eur:.0f}€ ({snap.cabin_type})\n{cruise.url}"
            )
    if len(lines) == 1:
        lines = ["No current offers tracked yet — check back after the next scrape."]
    await chat.send_message("\n".join(lines), disable_web_page_preview=True)


async def visafree(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    assert chat is not None
    today = date.today()
    with session_scope() as session:
        current = [
            snap
            for snap in latest_snapshots(session)
            if snap.cruise.departure_date >= today
        ]
        cache = get_cached_countries(
            session, {snap.cruise.route_hash for snap in current}
        )
        visa_ok = [
            snap
            for snap in current
            if all_visa_free(cache.get(snap.cruise.route_hash, set()), VISA_FREE_RU)
        ]
        visa_ok.sort(key=lambda s: s.price_eur / s.cruise.nights)
        lines = ["🛂 Cheapest visa-free (RU passport) per night:"]
        for rank, snap in enumerate(visa_ok[:10], start=1):
            cruise = snap.cruise
            ppn = snap.price_eur / cruise.nights
            countries = ",".join(sorted(cache[cruise.route_hash]))
            lines.append(
                f"{rank}. {ppn:.0f}€/night — {cruise.ship}, {cruise.nights} nights, "
                f"{cruise.departure_port}, {cruise.departure_date:%d.%m} — "
                f"{snap.price_eur:.0f}€ ({countries})\n{cruise.url}"
            )
    if len(lines) == 1:
        lines = [
            "No visa-free cruises known yet. The country cache fills when the "
            "scraper runs with PROFILE=visa_ru or PROFILE=all."
        ]
    await chat.send_message("\n".join(lines), disable_web_page_preview=True)


def main() -> None:
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set — see .env.example")
    app = Application.builder().token(settings.telegram_bot_token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("top", top))
    app.add_handler(CommandHandler("visafree", visafree))
    log.info("Bot polling started")
    app.run_polling()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    main()
