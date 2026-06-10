import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


class BaseScraper(ABC):
    source: str = ""

    @abstractmethod
    def fetch_listings(self, city: str) -> List[Dict[str, Any]]:
        """Return a list of normalised property dicts for the given city."""

    def scrape_cities(self, cities: List[str]) -> List[Dict[str, Any]]:
        all_listings: List[Dict[str, Any]] = []
        for city in cities:
            try:
                logger.info("[%s] Scraping city: %s", self.source, city)
                listings = self.fetch_listings(city)
                logger.info("[%s] %d listings fetched from %s", self.source, len(listings), city)
                all_listings.extend(listings)
            except Exception:
                logger.exception("[%s] Failed to scrape %s", self.source, city)
        return all_listings

    # ── Optional tier tracking (implemented by scrapers that support it) ──────

    def reset_tier_stats(self) -> None:
        """Reset per-tier request counters.  No-op if not overridden."""

    def get_tier_stats(self) -> Dict[str, Any]:
        """Return a snapshot of per-tier request counters."""
        return {}
