"""Deal-hunting profiles: independent configurations sharing one codebase,
database, and bot. Selected via the PROFILE env var ("default", "visa_ru",
or "all" to run every profile in one process).

Each profile decides (a) which scrapers run and (b) which hot deals it alerts
on. Alert dedup is per profile (alerts_sent.profile), so profiles never
suppress each other's alerts. Under "all", each deal is still sent as ONE
Telegram message — visa_ru's alert set is a subset of default's, so passing
deals just get a visa-free badge instead of a duplicate message.
"""

from dataclasses import dataclass

from app.config import settings
from app.visa import VISA_FREE_RU


@dataclass(frozen=True)
class Profile:
    name: str
    # scraper source slugs this profile wants; None = all registered scrapers
    sources: tuple[str, ...] | None = None
    # a deal must visit ONLY these countries to alert; None = no visa filter
    visa_filter: frozenset[str] | None = None


PROFILES: dict[str, Profile] = {
    # current behavior: every scraper, no filtering
    "default": Profile(name="default"),
    # Russian-passport mode: visa-free itineraries only. Currently the same
    # scraper list as default (seascanner is the only live source); narrow it
    # once portals exist that are irrelevant for RU citizens.
    "visa_ru": Profile(name="visa_ru", sources=("seascanner",), visa_filter=VISA_FREE_RU),
}


def active_profiles() -> list[Profile]:
    value = settings.profile
    if value == "all":
        return list(PROFILES.values())
    if value in PROFILES:
        return [PROFILES[value]]
    raise ValueError(
        f"unknown PROFILE {value!r} — expected 'all' or one of {sorted(PROFILES)}"
    )


def wanted_sources(profiles: list[Profile]) -> set[str] | None:
    """Union of the profiles' scraper lists; None = run all registered."""
    if any(p.sources is None for p in profiles):
        return None
    return {slug for p in profiles for slug in (p.sources or ())}
