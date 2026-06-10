"""
Central registry of available scrapers.

To add a new website:
  1. Create backend/scrapers/<site>.py implementing BaseScraper.
  2. Import and register it here.
"""

from scrapers.zoopla           import ZooplaScraper
from scrapers.allsop           import AllsopScraper
from scrapers.savills_auctions import SavillsAuctionsScraper
from scrapers.cliveemson       import CliveEmsonScraper
from scrapers.auctionhouse     import AuctionHouseScraper
from scrapers.strettons        import StrrettonsScraper

SCRAPER_REGISTRY = {
    "zoopla":           ZooplaScraper(),
    "allsop":           AllsopScraper(),
    "savills_auctions": SavillsAuctionsScraper(),
    "cliveemson":       CliveEmsonScraper(),
    "auctionhouse":     AuctionHouseScraper(),
    "strettons":        StrrettonsScraper(),
}
