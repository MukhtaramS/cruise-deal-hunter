"""Per-user deal matching — pure Python, no LLM (same boundary as deals.py).

A deal reaches a user only if it passes every preference the user has set:
budget (price per night), trip length, departure region, and — for RU
passports — the visa-free filter. NULL preferences mean "no filter".

Departure regions are derived from the departure port via a curated static
map (PORT_REGIONS) of the ports actually seen across our six sources. Port
strings are normalized first ("Long Beach (Los Angeles), Kalifornien" →
"long beach"). A port we can't classify matches only users WITHOUT a
departure preference — conservative: someone who asked for Mediterranean
departures never gets a guess. Istanbul is classified Mediterranean (the
overwhelming majority of Istanbul departures on German portals are Aegean/
Med itineraries, not Black Sea rounds).
"""

from __future__ import annotations

import re
from decimal import Decimal

from app.visa import VISA_FREE_RU, all_visa_free

MEDITERRANEAN = "mediterranean"
NORTHERN_EUROPE = "northern_europe"
CARIBBEAN = "caribbean"
BLACK_SEA = "black_sea"

DEPARTURE_REGIONS = (MEDITERRANEAN, NORTHERN_EUROPE, CARIBBEAN, BLACK_SEA)

# trip_length_pref value -> inclusive nights range
TRIP_LENGTH_RANGES: dict[str, tuple[int, int]] = {
    "2-4": (2, 4),
    "5-9": (5, 9),
    "10+": (10, 10**6),
}

PORT_REGIONS: dict[str, str] = {
    # Mediterranean
    "barcelona": MEDITERRANEAN, "palma": MEDITERRANEAN, "valencia": MEDITERRANEAN,
    "malaga": MEDITERRANEAN, "alicante": MEDITERRANEAN, "genua": MEDITERRANEAN,
    "genoa": MEDITERRANEAN, "savona": MEDITERRANEAN, "marseille": MEDITERRANEAN,
    "civitavecchia": MEDITERRANEAN, "neapel": MEDITERRANEAN, "venedig": MEDITERRANEAN,
    "triest": MEDITERRANEAN, "ravenna": MEDITERRANEAN, "bari": MEDITERRANEAN,
    "brindisi": MEDITERRANEAN, "piräus": MEDITERRANEAN, "lavrion": MEDITERRANEAN,
    "heraklion": MEDITERRANEAN, "korfu": MEDITERRANEAN, "istanbul": MEDITERRANEAN,
    "antalya": MEDITERRANEAN, "izmir": MEDITERRANEAN, "valletta": MEDITERRANEAN,
    "monte carlo": MEDITERRANEAN, "dubrovnik": MEDITERRANEAN, "split": MEDITERRANEAN,
    "limassol": MEDITERRANEAN, "nizza": MEDITERRANEAN, "toulon": MEDITERRANEAN,
    # Northern Europe
    "hamburg": NORTHERN_EUROPE, "kiel": NORTHERN_EUROPE, "bremerhaven": NORTHERN_EUROPE,
    "warnemünde": NORTHERN_EUROPE, "rostock": NORTHERN_EUROPE,
    "kopenhagen": NORTHERN_EUROPE, "copenhagen": NORTHERN_EUROPE,
    "oslo": NORTHERN_EUROPE, "stockholm": NORTHERN_EUROPE, "göteborg": NORTHERN_EUROPE,
    "southampton": NORTHERN_EUROPE, "dover": NORTHERN_EUROPE, "london": NORTHERN_EUROPE,
    "amsterdam": NORTHERN_EUROPE, "rotterdam": NORTHERN_EUROPE, "ijmuiden": NORTHERN_EUROPE,
    "zeebrügge": NORTHERN_EUROPE, "bergen": NORTHERN_EUROPE, "reykjavik": NORTHERN_EUROPE,
    "helsinki": NORTHERN_EUROPE, "tallinn": NORTHERN_EUROPE, "danzig": NORTHERN_EUROPE,
    # Caribbean
    "miami": CARIBBEAN, "fort lauderdale": CARIBBEAN, "port canaveral": CARIBBEAN,
    "tampa": CARIBBEAN, "jacksonville": CARIBBEAN, "new orleans": CARIBBEAN,
    "galveston": CARIBBEAN, "san juan": CARIBBEAN, "bridgetown": CARIBBEAN,
    "nassau": CARIBBEAN, "la romana": CARIBBEAN, "montego bay": CARIBBEAN,
    "willemstad": CARIBBEAN, "pointe-à-pitre": CARIBBEAN, "fort-de-france": CARIBBEAN,
    "cartagena": CARIBBEAN, "colon": CARIBBEAN,
    # Black Sea
    "odessa": BLACK_SEA, "constanta": BLACK_SEA, "varna": BLACK_SEA,
    "burgas": BLACK_SEA, "sotschi": BLACK_SEA, "batumi": BLACK_SEA,
    "trabzon": BLACK_SEA,
}


def normalize_port(port: str) -> str:
    """'Long Beach (Los Angeles), Kalifornien' -> 'long beach';
    'Palma, Mallorca (Balearen)' -> 'palma'."""
    cut = re.split(r"[(,]", port, maxsplit=1)[0]
    return re.sub(r"\s+", " ", cut).strip().lower()


def port_region(port: str) -> str | None:
    return PORT_REGIONS.get(normalize_port(port))


def parse_departure_prefs(raw: str | None) -> set[str] | None:
    """CSV column -> set of region slugs; None/empty -> no filter."""
    if not raw:
        return None
    prefs = {p.strip() for p in raw.split(",") if p.strip()}
    return prefs or None


def deal_matches_user(
    user,
    *,
    nights: int,
    price_per_night: Decimal,
    departure_port: str,
    countries: set[str],
) -> bool:
    """Apply every preference the user has set. `user` needs the attributes
    budget_per_night_max, trip_length_pref, departure_prefs,
    passport_country (models.User or any duck-typed stand-in)."""
    budget = user.budget_per_night_max
    if budget is not None and price_per_night > Decimal(budget):
        return False

    length_range = TRIP_LENGTH_RANGES.get(user.trip_length_pref or "")
    if length_range is not None:
        low, high = length_range
        if not (low <= nights <= high):
            return False

    prefs = parse_departure_prefs(user.departure_prefs)
    if prefs is not None:
        region = port_region(departure_port)
        if region is None or region not in prefs:
            return False

    if (user.passport_country or "").upper() == "RU":
        if not all_visa_free(countries, VISA_FREE_RU):
            return False

    return True
