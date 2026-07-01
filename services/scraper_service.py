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

DEFAULT_CITIES                  = ["london"]
MAX_LISTINGS_PER_CITY           = 500   # Zoopla default: stop after checking this many per city
MAX_CONSECUTIVE_DUPES           = 5     # stop after this many back-to-back duplicates
AUCTION_DEFAULT_MAX_PER_SCRAPER = 1000   # default cap for each auction source

# (source, city/category, fetch_details) triples for the 5 auction scrapers
# Ordered fastest→slowest: allsop (JSON API) → cliveemson/auctionhouse (curl) → savills (browser, no detail) → strettons (browser)
# fetch_details=False skips individual lot page fetches (faster, uses catalog card data only)
AUCTION_SCRAPER_CONFIGS: List[tuple] = [
    ("allsop",           "residential", True),
    ("cliveemson",       "all",         True),
    ("auctionhouse",     "london",      True),
    ("savills_auctions", "all",         False),  # Cloudflare on every detail page; catalog cards have enough data
    ("strettons",        "all",         True),
]


class _CityGuard:
    """
    Stateful callback passed to the scraper for one city/source.

    Called once per normalised listing (before detail fetching).
    Returns:
      "stop" — stop scraping this city now
      "skip" — listing already in DB; exclude it but keep going
      None   — new listing; include it
    """

    def __init__(self, session, city: str, source: str, max_per_city: int = MAX_LISTINGS_PER_CITY):
        self._session          = session
        self._city             = city
        self._source           = source
        self._max              = max_per_city
        self.total_checked     = 0
        self.consecutive_dupes = 0
        self.new_count         = 0
        self.stop_reason: Optional[str] = None

    def __call__(self, listing: Dict[str, Any]) -> Optional[str]:
        self.total_checked += 1

        if self.total_checked > self._max:
            logger.info(
                "[guard/%s] Hit %d-listing cap — stopping",
                self._city, self._max,
            )
            self.stop_reason = f"Reached {self._max}-listing cap"
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

            scraper.reset_tier_stats()

            guard = _CityGuard(session, city, source)

            # fetch_listings calls guard() per listing; skipped/stopped listings
            # are excluded from the returned list automatically.
            city_listings = scraper.fetch_listings(
                city,
                fetch_details=True,
                on_listing=guard,
            )

            tier_stats = scraper.get_tier_stats()

            logger.info(
                "[service] City %s: checked=%d  new=%d  consec_dupes_at_stop=%d  tier_stats=%s",
                city, guard.total_checked, guard.new_count, guard.consecutive_dupes, tier_stats,
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
                "tier_stats":  tier_stats,
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


def run_auction_scrapers(max_per_scraper: int = AUCTION_DEFAULT_MAX_PER_SCRAPER) -> dict:
    """
    Run all 5 auction-house scrapers and send ONE consolidated email report.

    max_per_scraper: maximum new listings to collect per source (default 100).
    Each source stops when it hits this cap OR 5 consecutive duplicates.
    """
    session    = SessionLocal()
    started_at = _now()
    run = ScraperRun(
        source="auction_houses",
        cities=[s for s, _, _ in AUCTION_SCRAPER_CONFIGS],
        status="running",
        started_at=started_at,
    )
    session.add(run)
    session.commit()
    run_id = str(run.id)
    logger.info("[auction] Run %s started — max_per_scraper=%d", run_id, max_per_scraper)

    total_added     = 0
    total_seen      = 0
    source_stats:   List[Dict[str, Any]] = []
    new_properties: List[Dict[str, Any]] = []
    errors:         List[str] = []

    for source, city, fetch_details in AUCTION_SCRAPER_CONFIGS:
        scraper = SCRAPER_REGISTRY.get(source)
        if scraper is None:
            logger.error("[auction] Unknown scraper: %s", source)
            errors.append(f"{source}: not in registry")
            source_stats.append({
                "city":        source,
                "added":       0,
                "checked":     0,
                "stop_reason": "not in registry",
                "tier_stats":  {},
            })
            continue

        logger.info("[auction] ── Scraping %s (city=%s) ──", source, city)
        scraper.reset_tier_stats()
        guard = _CityGuard(session, city, source, max_per_city=max_per_scraper)
        source_error: Optional[str] = None

        try:
            city_listings = scraper.fetch_listings(city, fetch_details=fetch_details, on_listing=guard)
            tier_stats    = scraper.get_tier_stats()

            logger.info(
                "[auction] %s: checked=%d  new=%d  tier_stats=%s",
                source, guard.total_checked, guard.new_count, tier_stats,
            )

            source_added = 0
            for data in city_listings:
                url = data.get("listing_url")
                if not url:
                    continue

                lat, lng = data.get("lat"), data.get("lng")
                if lat is not None and lng is not None:
                    try:
                        data["article4"] = bool(check_point(lat, lng))
                    except Exception:
                        data["article4"] = None
                else:
                    data["article4"] = None

                try:
                    with session.begin_nested():
                        session.add(PropertyListing(**data))
                        session.flush()
                except IntegrityError:
                    logger.debug("[auction] Duplicate skipped: %s", url)
                    continue

                new_properties.append(data)
                source_added += 1
                total_added  += 1

            session.commit()
            total_seen += guard.total_checked

        except Exception as exc:
            session.rollback()
            source_error = str(exc)
            errors.append(f"{source}: {source_error}")
            tier_stats   = {}
            source_added = 0
            logger.exception("[auction] %s scrape failed", source)

        source_stats.append({
            "city":        source,       # "city" key reused as source label in email
            "added":       source_added,
            "checked":     guard.total_checked,
            "stop_reason": guard.stop_reason or source_error or "All lots exhausted",
            "tier_stats":  tier_stats,
        })

    completed_at   = _now()
    combined_error = "; ".join(errors) if errors else None
    run.status         = "failed" if len(errors) == len(AUCTION_SCRAPER_CONFIGS) else "completed"
    run.listings_added = total_added
    run.listings_seen  = total_seen
    run.error          = combined_error
    run.completed_at   = completed_at
    session.commit()
    run_status = run.status  # capture before session closes
    session.close()

    logger.info(
        "[auction] Run %s complete — %d added / %d seen across %d sources",
        run_id, total_added, total_seen, len(AUCTION_SCRAPER_CONFIGS),
    )

    try:
        from services.email_service import send_scrape_report
        send_scrape_report(
            source="Auction Houses",
            started_at=started_at,
            completed_at=completed_at,
            city_stats=source_stats,
            total_added=total_added,
            total_seen=total_seen,
            new_properties=new_properties,
            error=combined_error,
            section_label="By Source",
        )
    except Exception:
        logger.warning("[auction] Email report dispatch failed", exc_info=True)

    return {
        "run_id":  run_id,
        "added":   total_added,
        "seen":    total_seen,
        "sources": len(AUCTION_SCRAPER_CONFIGS),
        "errors":  errors,
        "status":  run_status,
    }
