"""Alert formatting and per-user delivery.

`send_alerts` takes a mapping of chat_id -> alerts (built by
jobs.route_deals from each user's preferences). Every alert carries a
verification footer with the price age and the portal deep link — scraped
prices go stale, so the user is always told when we saw it."""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from telegram import Bot

from app.config import settings
from app.detector import HotDeal

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class OutgoingAlert:
    """A deal matched to a user, plus presentation flags. One OutgoingAlert
    = one Telegram message to one chat."""

    deal: HotDeal
    visa_free: bool = False  # route is fully visa-free (RU passport) -> badge


def price_age_minutes(scraped_at: datetime | None, now: datetime | None = None) -> int:
    """Whole minutes since the price was scraped; 0 when unknown. Naive
    timestamps (SQLite) are treated as UTC."""
    if scraped_at is None:
        return 0
    if scraped_at.tzinfo is None:
        scraped_at = scraped_at.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return max(0, int((now - scraped_at).total_seconds() // 60))


def format_alert(deal: HotDeal, visa_free: bool = False, now: datetime | None = None) -> str:
    """Spec format:
    🔥 -87% | AIDAnova, 7 nights, Hamburg, 12.09
    199€ (was 1499€ median)
    🕐 Price checked 12 min ago — verify on site:
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
    age = price_age_minutes(deal.scraped_at, now)
    lines.append(f"🕐 Price checked {age} min ago — verify on site:")
    lines.append(deal.url)
    return "\n".join(lines)


async def send_alerts(deliveries: dict[int, list[OutgoingAlert]]) -> None:
    """Send each user their matched alerts. deliveries: chat_id -> alerts."""
    total = sum(len(alerts) for alerts in deliveries.values())
    if not total:
        return
    if not settings.telegram_bot_token:
        log.warning(
            "Telegram not configured (token missing) — %d alert(s) not sent", total
        )
        for chat_id, alerts in deliveries.items():
            for alert in alerts:
                log.info(
                    "ALERT for %s (not sent):\n%s",
                    chat_id, format_alert(alert.deal, alert.visa_free),
                )
        return
    bot = Bot(settings.telegram_bot_token)
    sent = 0
    for chat_id, alerts in deliveries.items():
        for alert in alerts:
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=format_alert(alert.deal, alert.visa_free),
                    disable_web_page_preview=True,
                )
                sent += 1
            except Exception:
                # one blocked/dead chat must not stop everyone else's alerts
                log.exception("failed to send alert to chat %s", chat_id)
    log.info("Sent %d/%d alert(s) to %d chat(s)", sent, total, len(deliveries))
