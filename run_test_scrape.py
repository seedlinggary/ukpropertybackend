"""
One-shot test script:
  1. Adds auction_date column to property_listings if missing.
  2. Runs each auction scraper, capping at MAX_PER_SCRAPER listings each.
  3. Prints a summary table.

Usage (from backend/):
    python run_test_scrape.py
"""

import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("test_scrape")

# ── 0. Bootstrap path & env ──────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv()

# ── 1. DB migration: add columns if absent ───────────────────────────────────
from database import engine, SessionLocal
from sqlalchemy import text

with engine.connect() as conn:
    for col, typedef in [
        ("auction_date", "TIMESTAMP"),
        ("lot_number",   "VARCHAR(50)"),
    ]:
        result = conn.execute(text(
            f"SELECT column_name FROM information_schema.columns "
            f"WHERE table_name='property_listings' AND column_name='{col}'"
        ))
        if result.fetchone() is None:
            logger.info("Adding %s column …", col)
            conn.execute(text(f"ALTER TABLE property_listings ADD COLUMN {col} {typedef}"))
            conn.commit()
            logger.info("Column %s added.", col)
        else:
            logger.info("%s column already present.", col)

# ── 2. Import models & scraper machinery ─────────────────────────────────────
from models.property_listing import PropertyListing, ScraperRun
from models import Base
from sqlalchemy.exc import IntegrityError

try:
    from geoutils import check_point
    _GEO = True
except Exception:
    _GEO = False
    logger.warning("geoutils not available — article4 will be None")

MAX_PER_SCRAPER = 10

SCRAPERS_TO_RUN = [
    ("cliveemson",       ["all"]),
    ("auctionhouse",     ["london"]),
    ("allsop",           ["residential"]),
    ("savills_auctions", ["all"]),
    ("strettons",        ["all"]),
]


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).replace(tzinfo=None)


def scrape_one(source: str, cities: list) -> dict:
    """Run a single scraper, capped at MAX_PER_SCRAPER listings total."""
    from scrapers.registry import SCRAPER_REGISTRY

    scraper = SCRAPER_REGISTRY.get(source)
    if scraper is None:
        return {"source": source, "error": "not in registry", "added": 0, "seen": 0}

    session = SessionLocal()
    total_added = 0
    total_seen  = 0
    run_error   = None

    class _LimitGuard:
        def __init__(self):
            self.total_checked     = 0
            self.consecutive_dupes = 0
            self.new_count         = 0
            self.stop_reason       = None

        def __call__(self, listing):
            self.total_checked += 1
            if self.new_count >= MAX_PER_SCRAPER:
                self.stop_reason = f"Reached {MAX_PER_SCRAPER}-listing cap"
                return "stop"
            url = listing.get("listing_url")
            if not url:
                return "skip"
            exists = (
                session.query(PropertyListing.id)
                .filter_by(listing_url=url)
                .first()
            )
            if exists:
                self.consecutive_dupes += 1
                if self.consecutive_dupes >= 5:
                    self.stop_reason = "5 consecutive duplicates"
                    return "stop"
                return "skip"
            self.consecutive_dupes = 0
            self.new_count += 1
            return None

    try:
        for city in cities:
            scraper.reset_tier_stats()
            guard = _LimitGuard()
            city_listings = scraper.fetch_listings(
                city, fetch_details=True, on_listing=guard
            )
            logger.info(
                "[%s/%s] guard: checked=%d new=%d — %d listings returned",
                source, city, guard.total_checked, guard.new_count, len(city_listings),
            )
            for data in city_listings:
                url = data.get("listing_url")
                if not url:
                    continue
                lat, lng = data.get("lat"), data.get("lng")
                if _GEO and lat is not None and lng is not None:
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
                    total_added += 1
                except IntegrityError:
                    logger.debug("Duplicate skipped: %s", url)
            session.commit()
            total_seen += guard.total_checked

            if total_added >= MAX_PER_SCRAPER:
                logger.info("[%s] Reached %d added — stopping.", source, MAX_PER_SCRAPER)
                break

    except Exception as exc:
        session.rollback()
        run_error = str(exc)
        logger.exception("[%s] Scrape failed", source)
    finally:
        session.close()

    return {
        "source":    source,
        "added":     total_added,
        "seen":      total_seen,
        "error":     run_error,
    }


# ── 3. Delete old records for these sources ───────────────────────────────────
session = SessionLocal()
sources = [s for s, _ in SCRAPERS_TO_RUN]
deleted = session.query(PropertyListing).filter(PropertyListing.source.in_(sources)).delete(synchronize_session=False)
session.commit()
session.close()
logger.info("Deleted %d old auction records", deleted)

# ── 4. Run scrapers ───────────────────────────────────────────────────────────
results = []
for src, cities in SCRAPERS_TO_RUN:
    logger.info("=" * 60)
    logger.info("Starting scraper: %s  cities=%s", src, cities)
    logger.info("=" * 60)
    r = scrape_one(src, cities)
    results.append(r)
    logger.info("Finished %s: added=%d seen=%d error=%s", src, r["added"], r["seen"], r["error"])

# ── 5. Summary ────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print(f"{'SCRAPER':<25} {'ADDED':>6} {'SEEN':>6}  ERROR")
print("-" * 60)
for r in results:
    print(f"{r['source']:<25} {r['added']:>6} {r['seen']:>6}  {r['error'] or ''}")
print("=" * 60)
