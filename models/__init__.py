from sqlalchemy.orm import declarative_base

Base = declarative_base()

from models.property_listing import PropertyListing, ScraperRun  # noqa: F401, E402
from models.tier import Tier, TierFeature, AdminPermission  # noqa: F401, E402
from models.user import User, LoginEvent  # noqa: F401, E402
from models.feature_usage import FeatureUsage  # noqa: F401, E402
from models.notes import UserNote, UserFavorite  # noqa: F401, E402
from models.saved_search import UserSavedSearch  # noqa: F401, E402
from models.site_settings import SiteSetting  # noqa: F401, E402
from models.preferences import UserPropertyPrefs  # noqa: F401, E402
from models.dismissed import UserDismissedProperty  # noqa: F401, E402
from models.mortgage_rate import MortgageRate  # noqa: F401, E402
