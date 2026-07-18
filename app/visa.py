"""Visa-freedom data and country inference for cruise routes.

`VISA_FREE_RU` is the set of ISO 3166-1 alpha-2 codes visa-free for Russian
passport holders (per the user's list — not auto-updated, edit as rules
change). Countries visited by a cruise are not part of scraped data, so they
are inferred once per route via Groq (`app.llm.infer_countries`) and cached
in the `route_countries` table keyed by route_hash:

- countries string "TR,GR"  -> inferred country set
- countries string ""       -> LLM looked and couldn't tell (cached, not retried)
- no row                    -> never asked (a failed LLM call caches nothing)

The visa_ru profile only alerts when ALL inferred countries are visa-free;
routes with unknown countries never pass the filter (conservative).
"""

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import RouteCountries

log = logging.getLogger(__name__)

VISA_FREE_RU: frozenset[str] = frozenset({
    "TR",  # Turkey
    "AE",  # United Arab Emirates
    "OM",  # Oman
    "QA",  # Qatar
    "BH",  # Bahrain
    "TH",  # Thailand
    "MV",  # Maldives
    "LK",  # Sri Lanka
    "MY",  # Malaysia
    "ID",  # Indonesia
    "RS",  # Serbia
    "ME",  # Montenegro
    "CU",  # Cuba
    "MA",  # Morocco
    "TN",  # Tunisia
    "AR",  # Argentina
    "BR",  # Brazil
})

# (route_hash, title, ship, departure_port) — what the LLM gets to work with
RouteInfo = tuple[str, str, str, str]


def all_visa_free(countries: set[str], allowed: frozenset[str]) -> bool:
    """True only if the route's countries are known AND every one is allowed."""
    return bool(countries) and countries <= allowed


def get_cached_countries(session: Session, route_hashes: set[str]) -> dict[str, set[str]]:
    """Cached country sets for the given routes. Missing hashes are absent
    from the result (unknown-but-cached routes map to an empty set)."""
    if not route_hashes:
        return {}
    rows = session.execute(
        select(RouteCountries).where(RouteCountries.route_hash.in_(route_hashes))
    ).scalars()
    return {row.route_hash: _parse(row.countries) for row in rows}


def ensure_countries(session: Session, routes: list[RouteInfo]) -> dict[str, set[str]]:
    """Return countries per route_hash, inferring uncached routes via Groq in
    one batched call. A failed LLM call logs and caches nothing, so those
    routes stay eligible for retry next run."""
    unique: dict[str, RouteInfo] = {}
    for route in routes:
        unique.setdefault(route[0], route)
    cached = get_cached_countries(session, set(unique))
    missing = [unique[h] for h in unique if h not in cached]
    if not missing:
        return cached

    from app.llm import infer_countries  # lazy — keeps Groq out of test imports

    try:
        inferred = infer_countries(
            [
                {"title": title, "ship": ship, "departure_port": port}
                for (_, title, ship, port) in missing
            ]
        )
    except Exception:
        log.exception("country inference failed for %d route(s) — will retry next run", len(missing))
        return cached

    for (route_hash, *_), countries in zip(missing, inferred):
        session.add(
            RouteCountries(
                route_hash=route_hash,
                countries=",".join(sorted(countries)),
                inferred_at=datetime.now(timezone.utc),
            )
        )
        cached[route_hash] = set(countries)
    log.info("inferred countries for %d new route(s)", len(missing))
    return cached


def _parse(raw: str) -> set[str]:
    return {c for c in raw.split(",") if c}
