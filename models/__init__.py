from sqlalchemy.orm import declarative_base

Base = declarative_base()

from models.property_listing import PropertyListing, ScraperRun  # noqa: F401, E402
