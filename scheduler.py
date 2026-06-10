"""
Background scheduler:
  • Zoopla    — runs every 24 hours (property listings refresh daily)
  • Auctions  — runs every 72 hours (new lots published weekly/bi-monthly)

Uses APScheduler's BackgroundScheduler so it never blocks the Flask process.
"""

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)
_scheduler = BackgroundScheduler(daemon=True)

_ZOOPLA_CITIES = [
    "London", "Manchester", "Birmingham", "Glasgow",
    "Liverpool", "Sheffield", "Southampton", "Nottingham",
    "Bristol", "Leicester", "Edinburgh", "Cardiff",
    "Coventry", "Hull", "Stoke-on-Trent", "Reading",

    # Additional test cities
    "Portsmouth",
    "Milton Keynes",
    "Derby",
    "Wolverhampton",
    "Northampton",
    "Luton",
    "Swansea",
    "Plymouth",
    "Preston",
    "York",
]
# "Belfast","Bradford", "Newcastle","Leeds", dont want since didnt get any leads from

def _scheduled_zoopla_scrape() -> None:
    from services.scraper_service import run_scrape
    logger.info("[scheduler] Triggered automatic Zoopla scrape")
    try:
        run_scrape(source="zoopla", cities=_ZOOPLA_CITIES)
    except Exception:
        logger.exception("[scheduler] Zoopla scrape failed")


def _scheduled_auction_scrape() -> None:
    from services.scraper_service import run_auction_scrapers, AUCTION_DEFAULT_MAX_PER_SCRAPER
    logger.info("[scheduler] Triggered automatic auction scrape")
    try:
        run_auction_scrapers(max_per_scraper=AUCTION_DEFAULT_MAX_PER_SCRAPER)
    except Exception:
        logger.exception("[scheduler] Auction scrape failed")


def start_scheduler() -> None:
    if _scheduler.running:
        return

    _scheduler.add_job(
        func=_scheduled_zoopla_scrape,
        trigger=IntervalTrigger(hours=24),
        id="zoopla_auto_scrape",
        replace_existing=True,
    )

    _scheduler.add_job(
        func=_scheduled_auction_scrape,
        trigger=IntervalTrigger(hours=72),   # every 3 days — auctions update weekly
        id="auction_auto_scrape",
        replace_existing=True,
    )

    _scheduler.start()
    logger.info("[scheduler] Started — Zoopla every 24h, Auctions every 72h")


def stop_scheduler() -> None:
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("[scheduler] Stopped")
