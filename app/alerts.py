"""Alert formatting and delivery. Alerts go to every chat subscribed via
/start (telegram_chats table), plus TELEGRAM_CHAT_ID from .env as fallback."""

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session
from telegram import Bot

from app.config import settings
from app.detector import HotDeal
from app.models import TelegramChat

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class OutgoingAlert:
    """A deal that passed at least one profile's filters, plus presentation
    flags. One OutgoingAlert = one Telegram message per chat."""

    deal: HotDeal
    visa_free: bool = False  # passed the visa_ru filter -> gets a badge line


def format_alert(deal: HotDeal, visa_free: bool = False) -> str:
    """Spec format:
    🔥 -87% | AIDAnova, 7 nights, Hamburg, 12.09
    199€ (was 1499€ median)
    <url>
    Falls back to a per-night header when the deal fired on the €/night rule
    without a meaningful drop vs the median.
    """
    where = (
        f"{deal.ship}, {deal.nights} nights, "
        f"{deal.departure_port}, {deal.departure_date:%d.%m}"
    )
    if deal.discount_pct is not None and deal.discount_pct > 0:
        head = f"🔥 -{deal.discount_pct}% | {where}"
        body = f"{deal.price_eur:.0f}€ (was {deal.median_30d:.0f}€ median)"
    else:
        head = f"🔥 {deal.price_per_night:.0f}€/night | {where}"
        body = f"{deal.price_eur:.0f}€ total ({deal.cabin_type})"
    lines = [head, body]
    if visa_free:
        lines.append("✈️ Visa-free")
    lines.append(deal.url)
    return "\n".join(lines)


def get_alert_chat_ids(session: Session) -> list[str]:
    ids = [str(cid) for cid in session.execute(select(TelegramChat.chat_id)).scalars()]
    if settings.telegram_chat_id and settings.telegram_chat_id not in ids:
        ids.append(settings.telegram_chat_id)
    return ids


async def send_alerts(alerts: list[OutgoingAlert], chat_ids: list[str]) -> None:
    if not alerts:
        return
    texts = [format_alert(a.deal, visa_free=a.visa_free) for a in alerts]
    if not settings.telegram_bot_token or not chat_ids:
        log.warning(
            "Telegram not configured (token or chat ids missing) — %d alert(s) not sent",
            len(texts),
        )
        for text in texts:
            log.info("ALERT (not sent):\n%s", text)
        return
    bot = Bot(settings.telegram_bot_token)
    for chat_id in chat_ids:
        for text in texts:
            await bot.send_message(
                chat_id=chat_id, text=text, disable_web_page_preview=True
            )
    log.info("Sent %d alert(s) to %d chat(s)", len(texts), len(chat_ids))
