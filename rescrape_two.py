"""Re-scrape allsop and strettons, 10 listings each."""
import sys, os, logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(name)s %(message)s", stream=sys.stdout)
logger = logging.getLogger("rescrape")

sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv; load_dotenv()

from database import SessionLocal
from models.property_listing import PropertyListing
from scrapers.registry import SCRAPER_REGISTRY
from sqlalchemy.exc import IntegrityError

MAX = 10
SOURCES = [
    ("allsop",    ["residential"]),
    ("strettons", ["all"]),
]


class Guard:
    def __init__(self, session):
        self._session = session
        self.new_count = 0
        self.dupes = 0

    def __call__(self, listing):
        if self.new_count >= MAX:
            return "stop"
        url = listing.get("listing_url")
        if not url:
            return "skip"
        exists = self._session.query(PropertyListing.id).filter_by(listing_url=url).first()
        if exists:
            self.dupes += 1
            if self.dupes >= 5:
                return "stop"
            return "skip"
        self.dupes = 0
        self.new_count += 1
        return None


def run_one(source, cities):
    scraper = SCRAPER_REGISTRY.get(source)
    if not scraper:
        logger.error("Unknown source: %s", source)
        return 0

    session = SessionLocal()
    added = 0
    try:
        for city in cities:
            guard = Guard(session)
            listings = scraper.fetch_listings(city, fetch_details=True, on_listing=guard)
            logger.info("[%s/%s] guard: new=%d  listings returned=%d", source, city, guard.new_count, len(listings))

            for data in listings:
                url = data.get("listing_url")
                if not url:
                    continue
                try:
                    with session.begin_nested():
                        session.add(PropertyListing(**data))
                        session.flush()
                    added += 1
                    logger.info("  ADDED lot=%-5s auction=%s  url=...%s",
                                data.get("lot_number", "?"),
                                str(data.get("auction_date", ""))[:10],
                                str(url)[-50:])
                except IntegrityError:
                    pass

            session.commit()
            if added >= MAX:
                break
    except Exception:
        session.rollback()
        logger.exception("[%s] scrape failed", source)
    finally:
        session.close()

    return added


print("=" * 60)
for src, cities in SOURCES:
    logger.info("Starting %s ...", src)
    n = run_one(src, cities)
    logger.info("Finished %s: %d added", src, n)
    print("-" * 60)

print("Done.")
