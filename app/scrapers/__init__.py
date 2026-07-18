from app.scrapers.base import BaseScraper, CruiseOffer
from app.scrapers.cruise24 import Cruise24Scraper
from app.scrapers.cruiseportal24 import Cruiseportal24Scraper
from app.scrapers.dreamlines import DreamlinesScraper
from app.scrapers.kreuzfahrt_de import KreuzfahrtDeScraper
from app.scrapers.kreuzfahrten import KreuzfahrtenScraper
from app.scrapers.seascanner import SeascannerScraper

SCRAPERS: list[type[BaseScraper]] = [
    SeascannerScraper,
    Cruiseportal24Scraper,
    KreuzfahrtenScraper,
    Cruise24Scraper,
    KreuzfahrtDeScraper,
    # DreamlinesScraper is NOT registered: dreamlines.de sits behind Cloudflare
    # (403 for plain HTTP clients). Re-add once solved — likely via Playwright.
]

__all__ = [
    "BaseScraper",
    "CruiseOffer",
    "SCRAPERS",
    "Cruise24Scraper",
    "Cruiseportal24Scraper",
    "DreamlinesScraper",
    "KreuzfahrtDeScraper",
    "KreuzfahrtenScraper",
    "SeascannerScraper",
]
