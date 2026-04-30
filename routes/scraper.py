"""
Scraper API endpoints.

  POST /scraper/run    — trigger a scraping run in a background thread
  GET  /scraper/status — return status of the most recent run
"""

import logging
import threading

from flask import Blueprint, jsonify, request

from database import SessionLocal
from models.property_listing import ScraperRun
from scrapers.registry import SCRAPER_REGISTRY

scraper_bp = Blueprint("scraper", __name__, url_prefix="/scraper")
logger = logging.getLogger(__name__)


def _background_run(source: str, cities: list) -> None:
    from services.scraper_service import run_scrape
    try:
        run_scrape(source=source, cities=cities)
    except Exception:
        logger.exception("[route] Background scrape failed for source=%s cities=%s", source, cities)


@scraper_bp.route("/run", methods=["POST"])
def trigger_scrape():
    body = request.get_json(silent=True) or {}
    source = body.get("website", "zoopla")
    cities = body.get("cities") or ["london"]

    if source not in SCRAPER_REGISTRY:
        return jsonify({"error": f"Unknown source '{source}'. Available: {list(SCRAPER_REGISTRY)}"}), 400

    session = SessionLocal()
    try:
        running = session.query(ScraperRun).filter_by(status="running").first()
        if running:
            return jsonify({"error": "A scraper run is already in progress"}), 409
    finally:
        session.close()

    t = threading.Thread(target=_background_run, args=(source, cities), daemon=True)
    t.start()

    return jsonify({"message": "Scraper started", "source": source, "cities": cities}), 202


@scraper_bp.route("/status", methods=["GET"])
def scraper_status():
    session = SessionLocal()
    try:
        latest: ScraperRun = (
            session.query(ScraperRun)
            .order_by(ScraperRun.started_at.desc())
            .first()
        )

        if not latest:
            return jsonify({
                "status": "idle",
                "source": None,
                "cities": [],
                "last_run": None,
                "completed_at": None,
                "listings_added": 0,
                "listings_seen": 0,
                "error": None,
            })

        return jsonify({
            "status": latest.status,
            "source": latest.source,
            "cities": latest.cities or [],
            "last_run": latest.started_at.isoformat() if latest.started_at else None,
            "completed_at": latest.completed_at.isoformat() if latest.completed_at else None,
            "listings_added": latest.listings_added or 0,
            "listings_seen": latest.listings_seen or 0,
            "error": latest.error,
        })
    finally:
        session.close()
