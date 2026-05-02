"""
Orchestrates a full scraping run per city with smart stop conditions.

Per-city stop rules (applied in real time during scraping):
  • 5 consecutive listings that are already in the DB  → stop this city
  • 100 listings checked (new + duplicate) for this city → stop this city
  After either trigger the scraper moves on to the next city.

Deduplication is enforced at the DB level (listing_url UNIQUE constraint)
and also checked live via the on_listing callback so the scraper can stop
early instead of wasting requests on pages full of known listings.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # store as naive UTC

from sqlalchemy.exc import IntegrityError
from database import SessionLocal
from models.property_listing import PropertyListing, ScraperRun
from scrapers.registry import SCRAPER_REGISTRY
from geoutils import check_point

logger = logging.getLogger(__name__)

DEFAULT_CITIES        = ["london"]
MAX_LISTINGS_PER_CITY = 100   # stop after checking this many listings
MAX_CONSECUTIVE_DUPES = 5     # stop after this many back-to-back duplicates


class _CityGuard:
    """
    Stateful callback passed to the scraper for one city.

    Called once per normalised listing (before detail fetching).
    Returns:
      "stop" — stop scraping this city now
      "skip" — listing already in DB; exclude it but keep going
      None   — new listing; include it
    """

    def __init__(self, session, city: str, source: str):
        self._session          = session
        self._city             = city
        self._source           = source
        self.total_checked     = 0
        self.consecutive_dupes = 0
        self.new_count         = 0
        self.stop_reason: Optional[str] = None

    def __call__(self, listing: Dict[str, Any]) -> Optional[str]:
        self.total_checked += 1

        if self.total_checked > MAX_LISTINGS_PER_CITY:
            logger.info(
                "[guard/%s] Hit %d-listing cap — stopping city",
                self._city, MAX_LISTINGS_PER_CITY,
            )
            self.stop_reason = f"Reached {MAX_LISTINGS_PER_CITY}-listing cap"
            return "stop"

        url = listing.get("listing_url")
        if not url:
            return "skip"

        exists = (
            self._session.query(PropertyListing.id)
            .filter_by(listing_url=url)
            .first()
        )

        if exists:
            self.consecutive_dupes += 1
            logger.debug(
                "[guard/%s] Duplicate %d/%d consecutive: %s",
                self._city, self.consecutive_dupes, MAX_CONSECUTIVE_DUPES, url,
            )
            if self.consecutive_dupes >= MAX_CONSECUTIVE_DUPES:
                logger.info(
                    "[guard/%s] %d consecutive duplicates — stopping city",
                    self._city, MAX_CONSECUTIVE_DUPES,
                )
                self.stop_reason = f"{MAX_CONSECUTIVE_DUPES} consecutive duplicates"
                return "stop"
            return "skip"

        # New listing
        self.consecutive_dupes = 0
        self.new_count += 1
        return None


def run_scrape(source: str = "zoopla", cities: Optional[List[str]] = None) -> dict:
    if cities is None:
        cities = DEFAULT_CITIES

    scraper = SCRAPER_REGISTRY.get(source)
    if scraper is None:
        raise ValueError(
            f"Unknown scraper source: '{source}'. Available: {list(SCRAPER_REGISTRY)}"
        )

    session = SessionLocal()
    started_at = _now()
    run = ScraperRun(
        source=source,
        cities=cities,
        status="running",
        started_at=started_at,
    )
    session.add(run)
    session.commit()
    run_id = str(run.id)
    logger.info("[service] Run %s started — source=%s cities=%s", run_id, source, cities)

    total_added    = 0
    total_seen     = 0
    city_stats:    List[Dict[str, Any]] = []
    new_properties: List[Dict[str, Any]] = []
    run_error:     Optional[str] = None

    try:
        for city in cities:
            logger.info("[service] ── Scraping city: %s ──", city)

            guard = _CityGuard(session, city, source)

            # fetch_listings calls guard() per listing; skipped/stopped listings
            # are excluded from the returned list automatically.
            city_listings = scraper.fetch_listings(
                city,
                fetch_details=True,
                on_listing=guard,
            )

            logger.info(
                "[service] City %s: checked=%d  new=%d  consec_dupes_at_stop=%d",
                city, guard.total_checked, guard.new_count, guard.consecutive_dupes,
            )

            city_added = 0
            for data in city_listings:
                url = data.get("listing_url")
                if not url:
                    continue

                # Compute Article 4 status from the PostGIS polygons table.
                lat = data.get("lat")
                lng = data.get("lng")
                if lat is not None and lng is not None:
                    try:
                        data["article4"] = bool(check_point(lat, lng))
                    except Exception:
                        logger.warning("[service] check_point failed for %s", url, exc_info=True)
                        data["article4"] = None
                else:
                    data["article4"] = None

                # Use a savepoint so a duplicate URL on any single row skips
                # silently rather than rolling back the entire city's batch.
                try:
                    with session.begin_nested():
                        session.add(PropertyListing(**data))
                        session.flush()
                except IntegrityError:
                    logger.debug("[service] Duplicate skipped (race): %s", url)
                    continue
                new_properties.append(data)
                city_added  += 1
                total_added += 1

            session.commit()
            total_seen += guard.total_checked

            city_stats.append({
                "city":        city,
                "added":       city_added,
                "checked":     guard.total_checked,
                "stop_reason": guard.stop_reason or "All pages exhausted",
            })

        completed_at       = _now()
        run.status         = "completed"
        run.listings_added = total_added
        run.listings_seen  = total_seen
        run.completed_at   = completed_at
        session.commit()

        logger.info(
            "[service] Run %s complete — %d added / %d seen across %d cities",
            run_id, total_added, total_seen, len(cities),
        )

    except Exception as exc:
        session.rollback()  # clear any broken transaction before writing run status
        logger.exception("[service] Run %s failed", run_id)
        completed_at     = _now()
        run_error        = str(exc)
        run.status       = "failed"
        run.error        = run_error
        run.completed_at = completed_at
        session.commit()

    finally:
        session.close()

    # Send email report regardless of success/failure
    try:
        from services.email_service import send_scrape_report
        send_scrape_report(
            source=source,
            started_at=started_at,
            completed_at=completed_at,
            city_stats=city_stats,
            total_added=total_added,
            total_seen=total_seen,
            new_properties=new_properties,
            error=run_error,
        )
    except Exception:
        logger.warning("[service] Email report dispatch failed", exc_info=True)

    if run_error:
        raise RuntimeError(run_error)

    return {
        "run_id": run_id,
        "added":  total_added,
        "seen":   total_seen,
        "status": "completed",
    }
