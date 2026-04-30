"""
Background scheduler — runs the Zoopla scraper automatically every 12 hours.
Uses APScheduler's BackgroundScheduler so it never blocks the Flask process.
"""

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

logger = logging.getLogger(__name__)
_scheduler = BackgroundScheduler(daemon=True)


def _scheduled_scrape() -> None:
    from services.scraper_service import run_scrape
    logger.info("[scheduler] Triggered automatic Zoopla scrape")
    try:
        run_scrape(source="zoopla", cities=["london", "manchester", "birmingham"])
    except Exception:
        logger.exception("[scheduler] Automatic scrape failed")


def start_scheduler() -> None:
    if _scheduler.running:
        return
    _scheduler.add_job(
        func=_scheduled_scrape,
        # trigger=IntervalTrigger(minutes=5),
        trigger=IntervalTrigger(hours=240),
        id="zoopla_auto_scrape",
        replace_existing=True,
    )
    _scheduler.start()
    logger.info("[scheduler] Started — Zoopla scraper scheduled every 12 hours")


def stop_scheduler() -> None:
    if _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("[scheduler] Stopped")
