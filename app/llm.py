"""Groq helpers. Two jobs only:

1. `extract_offers` — fallback parser when a scraper's CSS selectors fail.
2. `is_same_cruise` — fuzzy dedup when route_hash doesn't match but two
   offers look like the same sailing on different portals.

Deal detection never touches this module.
"""

import json
import logging

from groq import Groq
from selectolax.parser import HTMLParser

from app.config import settings
from app.scrapers.base import CruiseOffer

log = logging.getLogger(__name__)

MAX_HTML_CHARS = 30_000

_client: Groq | None = None


def _get_client() -> Groq:
    global _client
    if _client is None:
        if not settings.groq_api_key:
            raise RuntimeError("GROQ_API_KEY is not set — LLM fallback unavailable")
        _client = Groq(api_key=settings.groq_api_key)
    return _client


def _strip_html(html: str) -> str:
    """Drop scripts/styles and truncate so the page fits in the prompt."""
    tree = HTMLParser(html)
    for node in tree.css("script, style, noscript, svg"):
        node.decompose()
    body = tree.body
    text = body.html if body is not None else html
    return text[:MAX_HTML_CHARS]


def extract_offers(html: str, source: str) -> list[CruiseOffer]:
    """Fallback parser: ask the LLM to pull structured offers out of raw HTML.
    Returns only offers that validate as CruiseOffer; invalid ones are dropped."""
    prompt = (
        "Extract all cruise offers from this German cruise portal HTML. "
        "Return a JSON object {\"offers\": [...]} where each offer has keys: "
        "cruise_line, ship, title, departure_port, departure_date (YYYY-MM-DD), "
        "nights (int), url, cabin_type (inside|outside|balcony|suite), "
        "price_eur (number, total price in EUR). "
        "Only include offers where you are confident about the price and date.\n\n"
        + _strip_html(html)
    )
    resp = _get_client().chat.completions.create(
        model=settings.groq_model,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0,
    )
    raw = json.loads(resp.choices[0].message.content or "{}").get("offers", [])
    offers = []
    for item in raw:
        try:
            offers.append(CruiseOffer(source=source, **item))
        except Exception as exc:
            log.warning("LLM offer failed validation, dropping: %s (%s)", item, exc)
    return offers


INFER_COUNTRIES_CHUNK = 25


def infer_countries(cruises: list[dict]) -> list[list[str]]:
    """Infer countries visited per cruise from title/ship/departure port.
    Input dicts need keys: title, ship, departure_port. Returns one list of
    ISO 3166-1 alpha-2 codes per input (aligned by index); [] where the model
    is unsure. Used by app.visa for the visa-free filter."""
    results: list[list[str]] = []
    for start in range(0, len(cruises), INFER_COUNTRIES_CHUNK):
        chunk = cruises[start : start + INFER_COUNTRIES_CHUNK]
        listing = "\n".join(
            f'{i}. title="{c["title"]}", ship="{c["ship"]}", departure="{c["departure_port"]}"'
            for i, c in enumerate(chunk)
        )
        prompt = (
            "For each cruise below, infer which countries the itinerary most "
            "likely visits (including the departure country). Respond as JSON: "
            '{"results": [["TR","GR"], ...]} with one array of ISO 3166-1 '
            "alpha-2 country codes per cruise, in input order. Use [] if you "
            "cannot tell with reasonable confidence.\n\n" + listing
        )
        resp = _get_client().chat.completions.create(
            model=settings.groq_model,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0,
        )
        raw = json.loads(resp.choices[0].message.content or "{}").get("results", [])
        for i in range(len(chunk)):
            entry = raw[i] if i < len(raw) and isinstance(raw[i], list) else []
            results.append(
                [c.upper() for c in entry if isinstance(c, str) and len(c) == 2 and c.isalpha()]
            )
    return results


def is_same_cruise(a: CruiseOffer, b: CruiseOffer) -> bool:
    """Fuzzy cross-portal dedup for cases route_hash can't catch (e.g. ship
    name spelled differently on two portals)."""
    resp = _get_client().chat.completions.create(
        model=settings.groq_model,
        messages=[
            {
                "role": "user",
                "content": (
                    "Are these two listings the same physical cruise sailing? "
                    'Answer as JSON: {"same": true/false}.\n'
                    f"A: {a.model_dump_json(exclude={'source', 'url', 'price_eur', 'cabin_type'})}\n"
                    f"B: {b.model_dump_json(exclude={'source', 'url', 'price_eur', 'cabin_type'})}"
                ),
            }
        ],
        response_format={"type": "json_object"},
        temperature=0,
    )
    return bool(json.loads(resp.choices[0].message.content or "{}").get("same"))
