"""
Central registry of available scrapers.

To add a new website:
  1. Create backend/scrapers/<site>.py implementing BaseScraper.
  2. Import and register it here.
"""

from scrapers.zoopla import ZooplaScraper

SCRAPER_REGISTRY = {
    "zoopla": ZooplaScraper(),
    # "rightmove": RightmoveScraper(),
    # "onthemarket": OnTheMarketScraper(),
}
