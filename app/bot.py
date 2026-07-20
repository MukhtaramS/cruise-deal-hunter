"""Telegram bot service entrypoint. Run with: python -m app.bot

Commands:
  /start    — 5-step inline-keyboard onboarding (region, passport, budget,
              trip length, departure regions). Region + passport required,
              the rest skippable. Ends with the user's best current matching
              deal as a sample.
  /settings — re-open any single onboarding step to change a preference
  /status   — last scrape time, cruises tracked, users, deals this week
  /top      — 5 cheapest per-night offers right now
  /visafree — 10 cheapest per-night fully visa-free (RU passport) offers

Alerts themselves are sent by the scraper service (app/jobs.py:route_deals)
per user; this process only answers commands and runs onboarding.
"""

import logging
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
)

from app.alerts import format_alert
from app.config import settings
from app.db import session_scope
from app.deals import median_price_last_30d
from app.detector import HotDeal, latest_snapshots
from app.matching import deal_matches_user
from app.models import AlertSent, Cruise, PriceSnapshot, User
from app.onboarding import (
    BUDGET,
    BUDGET_VALUES,
    DEPART,
    LENGTH,
    MENU,
    PASSPORT,
    PROMPTS,
    REGION,
    budget_keyboard,
    departure_keyboard,
    length_keyboard,
    passport_keyboard,
    region_keyboard,
    settings_menu_keyboard,
)
from app.visa import VISA_FREE_RU, all_visa_free, get_cached_countries

log = logging.getLogger(__name__)


# ---------------------------------------------------------------- sample deal

def best_current_match(session, user) -> HotDeal | None:
    """The user's best (cheapest per-night) currently-tracked offer that
    passes their preferences — sent right after onboarding as proof of value."""
    today = date.today()
    snaps = [
        s for s in latest_snapshots(session) if s.cruise.departure_date >= today
    ]
    needs_visa = (getattr(user, "passport_country", None) or "").upper() == "RU"
    countries = (
        get_cached_countries(session, {s.cruise.route_hash for s in snaps})
        if needs_visa
        else {}
    )
    best = None
    for snap in snaps:
        cruise = snap.cruise
        ppn = snap.price_eur / cruise.nights
        if not deal_matches_user(
            user,
            nights=cruise.nights,
            price_per_night=ppn,
            departure_port=cruise.departure_port,
            countries=countries.get(cruise.route_hash, set()),
        ):
            continue
        if best is None or ppn < best[0]:
            best = (ppn, snap)
    if best is None:
        return None
    snap = best[1]
    cruise = snap.cruise
    return HotDeal(
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
        median_30d=median_price_last_30d(session, cruise.id, snap.cabin_type),
        scraped_at=snap.scraped_at,
    )


async def _send_sample_deal(chat, user) -> None:
    with session_scope() as session:
        deal = best_current_match(session, user)
        visa_free = False
        if deal is not None and (user.passport_country or "").upper() == "RU":
            cached = get_cached_countries(session, {deal.route_hash})
            countries = cached.get(deal.route_hash, set())
            visa_free = bool(countries) and all_visa_free(countries, VISA_FREE_RU)
        text = format_alert(deal, visa_free=visa_free) if deal is not None else None
    if text:
        await chat.send_message("🎯 Your best current match:")
        await chat.send_message(text, disable_web_page_preview=True)
    else:
        await chat.send_message(
            "No offer matches your preferences right now — you'll get an "
            "alert the moment one appears. Widen filters anytime with /settings."
        )


# ------------------------------------------------------------- onboarding flow

def _prefs(ctx: ContextTypes.DEFAULT_TYPE) -> dict:
    return ctx.user_data.setdefault("prefs", {"dep": set()})


def _in_settings(ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    return ctx.user_data.get("mode") == "settings"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat = update.effective_chat
    assert chat is not None
    with session_scope() as session:
        user = session.get(User, chat.id)
        onboarded = user is not None and user.onboarded_at is not None
    if onboarded:
        await chat.send_message(
            "🚢 You're already set up — deals matching your preferences land "
            "here automatically.\nCommands: /settings, /status, /top, /visafree, /reset"
        )
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data["mode"] = "onboarding"
    _prefs(context)
    name = update.effective_user.first_name if update.effective_user else None
    hello = f"Welcome{', ' + name if name else ''}! Five quick questions and " \
            "I'll start hunting cruise deals for you.\n\n"
    await chat.send_message(hello + PROMPTS[REGION], reply_markup=region_keyboard())
    return REGION


async def on_region(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _prefs(context)["home_region"] = query.data.rsplit(":", 1)[-1]
    if _in_settings(context):
        return await _save_single(update, context, "home_region")
    await query.edit_message_text(PROMPTS[PASSPORT], reply_markup=passport_keyboard())
    return PASSPORT


async def on_passport(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _prefs(context)["passport_country"] = query.data.rsplit(":", 1)[-1]
    if _in_settings(context):
        return await _save_single(update, context, "passport_country")
    await query.edit_message_text(PROMPTS[BUDGET], reply_markup=budget_keyboard())
    return BUDGET


async def on_budget(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    _prefs(context)["budget_per_night_max"] = BUDGET_VALUES[query.data.rsplit(":", 1)[-1]]
    if _in_settings(context):
        return await _save_single(update, context, "budget_per_night_max")
    await query.edit_message_text(PROMPTS[LENGTH], reply_markup=length_keyboard())
    return LENGTH


async def on_length(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    value = query.data.rsplit(":", 1)[-1]
    _prefs(context)["trip_length_pref"] = None if value == "any" else value
    if _in_settings(context):
        return await _save_single(update, context, "trip_length_pref")
    await query.edit_message_text(
        PROMPTS[DEPART], reply_markup=departure_keyboard(_prefs(context)["dep"])
    )
    return DEPART


async def on_depart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    value = query.data.rsplit(":", 1)[-1]
    prefs = _prefs(context)
    if value == "any":
        prefs["dep"] = set()
    elif value != "done":
        prefs["dep"] ^= {value}  # toggle
        await query.edit_message_reply_markup(departure_keyboard(prefs["dep"]))
        return DEPART
    prefs["departure_prefs"] = ",".join(sorted(prefs["dep"])) or None
    if _in_settings(context):
        return await _save_single(update, context, "departure_prefs")
    return await _finish_onboarding(update, context)


async def _finish_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat = update.effective_chat
    prefs = _prefs(context)
    with session_scope() as session:
        user = session.get(User, chat.id)
        if user is None:
            user = User(chat_id=chat.id)
            session.add(user)
        user.first_name = (
            update.effective_user.first_name if update.effective_user else None
        )
        user.home_region = prefs.get("home_region")
        user.passport_country = prefs.get("passport_country")
        user.budget_per_night_max = prefs.get("budget_per_night_max")
        user.trip_length_pref = prefs.get("trip_length_pref")
        user.departure_prefs = prefs.get("departure_prefs")
        user.onboarded_at = datetime.now(timezone.utc)
        session.flush()
        session.expunge(user)
    await update.callback_query.edit_message_text(
        "✅ You're all set! I check prices every 4 hours and only message "
        "you when a deal matches your preferences.\n"
        "Change them anytime with /settings."
    )
    await _send_sample_deal(chat, user)
    context.user_data.clear()
    return ConversationHandler.END


# ------------------------------------------------------------------- /settings

FIELD_LABELS = {
    "home_region": "booking region",
    "passport_country": "passport",
    "budget_per_night_max": "budget",
    "trip_length_pref": "trip length",
    "departure_prefs": "departure regions",
}


async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat = update.effective_chat
    assert chat is not None
    with session_scope() as session:
        user = session.get(User, chat.id)
        onboarded = user is not None and user.onboarded_at is not None
        current_deps = set((user.departure_prefs or "").split(",")) - {""} if user else set()
    if not onboarded:
        await chat.send_message("Run /start first to set up your preferences.")
        return ConversationHandler.END
    context.user_data.clear()
    context.user_data["mode"] = "settings"
    context.user_data["prefs"] = {"dep": current_deps}
    await chat.send_message(
        "What would you like to change?", reply_markup=settings_menu_keyboard()
    )
    return MENU


async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    step = query.data.rsplit(":", 1)[-1]
    if step == "region":
        await query.edit_message_text(PROMPTS[REGION], reply_markup=region_keyboard())
        return REGION
    if step == "pass":
        await query.edit_message_text(PROMPTS[PASSPORT], reply_markup=passport_keyboard())
        return PASSPORT
    if step == "budget":
        await query.edit_message_text(PROMPTS[BUDGET], reply_markup=budget_keyboard())
        return BUDGET
    if step == "len":
        await query.edit_message_text(PROMPTS[LENGTH], reply_markup=length_keyboard())
        return LENGTH
    await query.edit_message_text(
        PROMPTS[DEPART], reply_markup=departure_keyboard(_prefs(context)["dep"])
    )
    return DEPART


async def _save_single(update: Update, context: ContextTypes.DEFAULT_TYPE, field: str) -> int:
    chat = update.effective_chat
    value = _prefs(context).get(field)
    with session_scope() as session:
        user = session.get(User, chat.id)
        if user is not None:
            setattr(user, field, value)
    await update.callback_query.edit_message_text(
        f"✅ Updated your {FIELD_LABELS[field]}."
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    if update.effective_chat:
        await update.effective_chat.send_message("Cancelled — nothing was changed.")
    return ConversationHandler.END

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    assert chat is not None

    with session_scope() as session:
        user = session.get(User, chat.id)
        if user is not None:
            user.home_region = None
            user.passport_country = None
            user.budget_per_night_max = None
            user.trip_length_pref = None
            user.departure_prefs = None
            user.onboarded_at = None

    context.user_data.clear()

    await chat.send_message(
        "✅ Onboarding reset.\n"
        "Send /start to begin again."
    )

# ------------------------------------------------------------- info commands

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    assert chat is not None
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    with session_scope() as session:
        last_scrape = session.execute(
            select(func.max(PriceSnapshot.scraped_at))
        ).scalar()
        cruises = session.execute(select(func.count(Cruise.id))).scalar_one()
        users = session.execute(
            select(func.count()).select_from(User).where(User.onboarded_at.is_not(None))
        ).scalar_one()
        deals_week = session.execute(
            select(func.count())
            .select_from(AlertSent)
            .where(AlertSent.sent_at >= week_ago)
        ).scalar_one()
    last = f"{last_scrape:%d.%m.%Y %H:%M} UTC" if last_scrape else "never"
    await chat.send_message(
        f"Last scrape: {last}\n"
        f"Cruises tracked: {cruises}\n"
        f"Users onboarded: {users}\n"
        f"Alerts sent this week: {deals_week}"
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
            "No visa-free cruises known yet. The country cache fills once an "
            "RU-passport user is onboarded and a scrape has run."
        ]
    await chat.send_message("\n".join(lines), disable_web_page_preview=True)


# ------------------------------------------------------------------- wiring

def build_application() -> Application:
    app = Application.builder().token(settings.telegram_bot_token).build()
    app.add_handler(CommandHandler("reset", reset), group=-1) 
    conversation = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("settings", settings_cmd),
        ],
        states={
            MENU: [CallbackQueryHandler(on_menu, pattern=r"^menu:")],
            REGION: [CallbackQueryHandler(on_region, pattern=r"^ob:region:")],
            PASSPORT: [CallbackQueryHandler(on_passport, pattern=r"^ob:pass:")],
            BUDGET: [CallbackQueryHandler(on_budget, pattern=r"^ob:budget:")],
            LENGTH: [CallbackQueryHandler(on_length, pattern=r"^ob:len:")],
            DEPART: [CallbackQueryHandler(on_depart, pattern=r"^ob:dep:")],
        },
        fallbacks=[CommandHandler("cancel", cancel),             
                   CommandHandler("start", start), ],
    )
    app.add_handler(conversation)
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("top", top))
    app.add_handler(CommandHandler("visafree", visafree))
    return app


def main() -> None:
    if not settings.telegram_bot_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set — see .env.example")
    app = build_application()
    log.info("Bot polling started")
    app.run_polling()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    main()
