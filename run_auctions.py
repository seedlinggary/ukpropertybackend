"""
Run all 5 auction-house scrapers locally with an optional listing cap.

Usage (from backend/):
    python run_auctions.py              # default: 100 per source
    python run_auctions.py --limit 20   # quick test, 20 per source
    python run_auctions.py --limit 200  # deeper pull
"""

import argparse
import logging
import os
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("run_auctions")

sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv()

# ── DB migration: ensure auction columns exist ────────────────────────────────
from database import engine
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
            logger.info("Adding column %s ...", col)
            conn.execute(text(f"ALTER TABLE property_listings ADD COLUMN {col} {typedef}"))
            conn.commit()

# ── Parse args ────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="Scrape all 5 auction houses")
parser.add_argument(
    "--limit", type=int, default=100,
    help="Max new listings to collect per source (default: 100)",
)
args = parser.parse_args()

logger.info("Starting auction scrape — limit=%d per source", args.limit)

# ── Run ───────────────────────────────────────────────────────────────────────
from services.scraper_service import run_auction_scrapers, AUCTION_SCRAPER_CONFIGS

result = run_auction_scrapers(max_per_scraper=args.limit)

# ── Summary table ─────────────────────────────────────────────────────────────
print("\n" + "=" * 65)
print(f"{'SOURCE':<20} {'ADDED':>6} {'CHECKED':>8}  ERROR")
print("-" * 65)

# Pull per-source stats from the result errors list for the table
sources = [s for s, _, _ in AUCTION_SCRAPER_CONFIGS]
errors_map = {}
for e in result.get("errors", []):
    src = e.split(":")[0].strip()
    errors_map[src] = e

# Re-read from DB for accurate counts (service already committed)
from database import SessionLocal
from models.property_listing import PropertyListing
from datetime import datetime, timezone, timedelta

session = SessionLocal()
cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=30)
print(f"{'SOURCE':<20} {'IN DB':>6}  STATUS")
print("-" * 65)
for src in sources:
    count = session.query(PropertyListing).filter(
        PropertyListing.source == src
    ).count()
    err = errors_map.get(src, "")
    status = f"error: {err}" if err else "ok"
    print(f"  {src:<18} {count:>6}  {status}")
session.close()

print("=" * 65)
print(f"Total added this run: {result['added']}  |  Total checked: {result['seen']}")
if result.get("errors"):
    print(f"Errors: {'; '.join(result['errors'])}")
print()
