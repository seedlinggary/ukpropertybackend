"""
Allsop LLP auction scraper (allsop.co.uk) — pure JSON API approach.

Allsop exposes an undocumented REST API used by their React front end:
  /api/search          — paginated list of auction lots (20/page)
  /api/lot/{lotid}     — full detail for a single lot

No browser or ScraperAPI credits needed; plain HTTPS requests with a
browser User-Agent work fine.

Filters applied:
  available_only=true  — skip lots already sold
  sortOrder=Date+Descending — most-recent first

listing_url is constructed as:
  https://www.allsop.co.uk/lot-overview/{ref_lower}-{lot_num}
  e.g. reference "R260625 285" → lot-overview/r260625-285

Env flags:
  FETCH_DETAILS  "0" (default) | "1"
    When "1", fetches /api/lot/{id} for each lot to obtain
    square_foot, bedrooms, bathrooms, images, description.
    When "0", uses data already present in the search result.
"""

import logging
import re
import time
import random
import urllib.parse
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

try:
    from zoneinfo import ZoneInfo as _ZoneInfo
    _UK_TZ = _ZoneInfo("Europe/London")
except (ImportError, KeyError):
    _UK_TZ = None

from scrapers.base import BaseScraper
from scrapers._http import (
    TierTracker,
    normalize_property_type, city_from_address, geocode_address,
)

logger = logging.getLogger(__name__)

BASE   = "https://www.allsop.co.uk"
SOURCE = "allsop"

_PAGE_SIZE     = 20  # API returns 20 results per page

OnListingFn = Callable[[Dict[str, Any]], Optional[str]]

_SEARCH_HEADERS = {
    "Accept":          "application/json",
    "Accept-Language": "en-GB,en;q=0.9",
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer":         "https://www.allsop.co.uk/property-search",
}

_SQFT_RE = re.compile(r"(\d[\d,\.]*)\s*sq\.?\s*(?:ft|feet)", re.IGNORECASE)
_SQM_RE  = re.compile(r"(\d[\d,\.]*)\s*(?:sq\.?\s*m|m2|m²)", re.IGNORECASE)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _api_get(path: str, params: Optional[Dict] = None) -> Optional[Dict]:
    """GET an Allsop API endpoint, return parsed JSON or None."""
    try:
        import requests as _req
        url = BASE + path
        r = _req.get(url, params=params, headers=_SEARCH_HEADERS, timeout=30)
        if r.status_code != 200:
            logger.warning("[%s] API %s -> HTTP %d", SOURCE, path, r.status_code)
            return None
        return r.json()
    except Exception:
        logger.warning("[%s] API request failed for %s", SOURCE, path, exc_info=True)
        return None


def _lot_url(lot: Dict) -> Optional[str]:
    """
    Build the public lot-overview URL replicating Allsop's JS generateLotOverviewUrl:
      /lot-overview/{byline-in-town-slug}/{name-slug}

    The byline+town segment is: (byline + " in " + town) with non-alphanumeric
    chars stripped and spaces collapsed to hyphens.
    The name segment (e.g. "R260625-285") is URL-encoded with spaces→hyphens.
    """
    byline = lot.get("allsop_propertybyline") or lot.get("main_byline") or ""
    town   = lot.get("allsop_propertytown")   or ""
    name   = lot.get("allsop_name")           or ""
    if not name:
        return None
    raw    = byline + " in " + town
    seg2   = re.sub(r'\s+', '-', re.sub(r'[^a-zA-Z0-9\s]', '', raw)).lower().strip('-')
    seg3   = urllib.parse.quote(name, safe='').replace('%20', '-').lower()
    return f"{BASE}/lot-overview/{seg2}/{seg3}"


def _parse_auction_date(value) -> Optional[datetime]:
    """
    Parse epoch-ms integer or ISO string to a naive datetime at UK local midnight.
    Epoch values from the Allsop API are UTC; converting to Europe/London before
    taking midnight avoids storing June 24 23:00 UTC instead of June 25 (BST).
    """
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            utc_dt = datetime.fromtimestamp(value / 1000, tz=timezone.utc)
            if _UK_TZ is not None:
                local_dt = utc_dt.astimezone(_UK_TZ)
            else:
                # Fallback: approximate BST (+1h) — correct for summer auctions
                local_dt = utc_dt + timedelta(hours=1)
            return local_dt.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
        s = str(value)
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(s[:26], fmt)
            except ValueError:
                pass
    except Exception:
        pass
    return None


def _size_from_features(features: List) -> Optional[float]:
    """Extract sq m from a features list like ['approx. 1,178 sq m (12,680 sq ft)', ...]."""
    for feat in features:
        text = str(feat.get("value", "") if isinstance(feat, dict) else feat)
        m = _SQM_RE.search(text)
        if m:
            try:
                return float(m.group(1).replace(",", ""))
            except ValueError:
                pass
        m = _SQFT_RE.search(text)
        if m:
            try:
                return round(float(m.group(1).replace(",", "")) * 0.0929, 1)
            except ValueError:
                pass
    return None


def _sqft_to_m2(sqft) -> Optional[float]:
    try:
        v = float(sqft)
        return round(v * 0.0929, 1) if v > 0 else None
    except (TypeError, ValueError):
        return None


def _image_url(file_id: Optional[str]) -> Optional[str]:
    if not file_id:
        return None
    return f"{BASE}/api/image/{file_id}/600/450"


def _prop_type(types: List, lot_type: str) -> Optional[str]:
    """Map Allsop property_types list + lot_type to a short canonical string."""
    joined = " ".join(str(t) for t in (types or [])).lower()
    if "flat" in joined or "apartment" in joined:
        return "flat"
    if any(w in joined for w in ("house", "terraced", "detached", "bungalow", "cottage")):
        return "house"
    if lot_type == "residential":
        return "house"
    if any(w in joined for w in ("industrial", "warehouse", "office", "retail", "shop")):
        return "commercial"
    if "development" in joined or "mixed" in joined:
        return "commercial"
    if "land" in joined:
        return "land"
    return normalize_property_type(joined) or ("house" if lot_type == "residential" else "commercial")


def _parse_search_result(lot: Dict) -> Dict[str, Any]:
    """Turn a /api/search result dict into a partial listing dict."""
    full_addr = lot.get("full_address") or lot.get("allsop_address") or ""
    price    = lot.get("guide_price_lower") or lot.get("sort_price")
    loc      = lot.get("location") or {}
    lat = lng = None
    try:
        lat_v = loc.get("lat")
        lng_v = loc.get("lon") or loc.get("lng")
        if lat_v is not None and lng_v is not None:
            lat, lng = float(lat_v), float(lng_v)
            if not (49.0 < lat < 61.0 and -8.0 < lng < 2.0):
                lat = lng = None
    except (TypeError, ValueError):
        pass
    if lat is None and full_addr:
        lat, lng = geocode_address(full_addr)

    auction_date = _parse_auction_date(lot.get("auction_date"))
    lot_type = lot.get("lot_type") or lot.get("catalogue_type") or ""
    types_list = lot.get("property_types") or lot.get("allsop_propertytype") or []

    img = _image_url(
        lot.get("featured_image_file_id") or lot.get("featured_image_path") or lot.get("image_file_id")
    )

    # lot_number_text is the human-readable catalogue number (e.g. "85", "60A")
    lot_num = lot.get("lot_number_text") or lot.get("lot_number")
    lot_number = str(lot_num).strip() if lot_num and str(lot_num).strip() not in ("", "0", "None") else None

    # Build the 3-segment public URL matching Allsop's generateLotOverviewUrl JS:
    #   /lot-overview/{byline-in-town-slug}/{name-slug}
    # The UUID is kept as _lotid solely for the /api/lot/{uuid} detail call.
    listing_url = _lot_url(lot)

    return {
        "source":        SOURCE,
        "listing_url":   listing_url,
        "address":       full_addr.strip() or None,
        "city":          city_from_address(full_addr),
        "price":         int(price) if price else None,
        "bedrooms":      None,
        "bathrooms":     None,
        "size_m2":       None,
        "property_type": _prop_type(types_list, lot_type),
        "description":   lot.get("allsop_propertybyline") or lot.get("main_byline") or None,
        "agent_name":    "Allsop LLP",
        "agent_phone":   None,
        "image_url":     img,
        "lat":           lat,
        "lng":           lng,
        "auction_date":  auction_date,
        "lot_number":    lot_number,
        "_lotid":        lot.get("allsop_lotid"),
    }


def _enrich_with_detail(partial: Dict, lotid: str) -> None:
    """Fetch /api/lot/{lotid} and fill in size_m2, bedrooms, bathrooms, images, description."""
    data = _api_get(f"/api/lot/{lotid}")
    if not data:
        return
    detail = data.get("data", {})

    # Square footage → m2
    sqft = detail.get("square_foot")
    size = _sqft_to_m2(sqft)
    if size is None:
        size = _size_from_features(detail.get("features", []))
    if size is not None:
        partial["size_m2"] = size

    # Bedrooms / bathrooms from nested property object
    prop = detail.get("property") or {}
    beds  = prop.get("bedrooms")
    baths = prop.get("bathrooms")
    if beds is not None:
        try:
            partial["bedrooms"] = int(beds)
        except (TypeError, ValueError):
            pass
    if baths is not None:
        try:
            partial["bathrooms"] = int(baths)
        except (TypeError, ValueError):
            pass

    # Fall back: parse from features text
    if partial["bedrooms"] is None:
        for feat in detail.get("features", []):
            text = str(feat.get("value", "") if isinstance(feat, dict) else feat)
            m = re.search(r"(\d+)\s+(?:bed|bedroom)", text, re.IGNORECASE)
            if m:
                partial["bedrooms"] = int(m.group(1))
                break

    # Best description paragraph
    desc_items = detail.get("description", [])
    if desc_items:
        best = max(
            (str(d.get("value", "") if isinstance(d, dict) else d) for d in desc_items),
            key=len, default=None,
        )
        if best and len(best) > 20:
            partial["description"] = best[:2000]

    # First featured image
    images = detail.get("images", [])
    if images and not partial.get("image_url"):
        fid = images[0].get("file_id")
        partial["image_url"] = _image_url(fid)

    # Auction date from detail if missing
    if partial["auction_date"] is None:
        auction = detail.get("allsop_auction") or {}
        partial["auction_date"] = _parse_auction_date(auction.get("allsop_auctiondate"))


# ── Main scraper class ────────────────────────────────────────────────────────

class AllsopScraper(BaseScraper):
    source = SOURCE

    def __init__(self) -> None:
        self._tracker = TierTracker()

    def reset_tier_stats(self) -> None:
        self._tracker.reset()

    def get_tier_stats(self) -> Dict:
        return self._tracker.get()

    def fetch_listings(
        self,
        city: str,
        fetch_details: bool = True,
        on_listing: Optional[OnListingFn] = None,
    ) -> List[Dict[str, Any]]:
        """
        Scrape Allsop auction lots via the JSON API.

        city: "residential" | "commercial" | "all" — filters lot_type
        Pagination stops once on_listing returns "stop" or we run out of pages.
        """
        city_lower = city.strip().lower()
        lot_type_filter = (
            None if city_lower in ("all", "")
            else city_lower if city_lower in ("residential", "commercial")
            else None
        )

        results: List[Dict[str, Any]] = []
        stop_all = False
        page = 1

        # Lots from auctions more than this many days ago are skipped so that
        # upcoming / recent lots always appear first when sorted Date Descending.
        _PAST_CUTOFF = datetime.now() - timedelta(days=30)

        while not stop_all:
            params: Dict[str, Any] = {
                "page":      page,
                "sortOrder": "Date Descending",
                # Note: available_only=true is intentionally omitted so that
                # upcoming (pre-auction) lots are included alongside Unsold ones.
            }
            if lot_type_filter:
                params["lot_type"] = lot_type_filter

            logger.info("[%s] Fetching search page %d (lot_type=%s)", SOURCE, page, lot_type_filter or "all")
            data = _api_get("/api/search", params=params)
            self._tracker.track("search", "curl", bool(data))

            if not data:
                logger.warning("[%s] API returned nothing for page %d", SOURCE, page)
                break

            lots = data.get("data", {}).get("results", [])
            total = data.get("data", {}).get("total", 0)
            logger.info("[%s] Page %d: %d lots (total=%d)", SOURCE, page, len(lots), total)

            if not lots:
                break

            for lot in lots:
                if stop_all:
                    break

                # Skip inactive lots.
                status = (lot.get("lotStatus") or lot.get("allsop_lotstatus") or "").lower()
                if status in ("sold", "withdrawn", "exchanged", "sold prior"):
                    logger.debug("[%s] Skipping lot %s (status=%s)", SOURCE, lot.get("allsop_name"), status)
                    continue

                # Skip lots from auctions more than 30 days ago. Because results
                # are sorted Date Descending, the first old lot signals that
                # every remaining lot on every remaining page is also old.
                auction_dt = _parse_auction_date(lot.get("auction_date"))
                if auction_dt is not None and auction_dt < _PAST_CUTOFF:
                    logger.info(
                        "[%s] Hit lot %s with auction_date=%s — stopping pagination (all remaining are older)",
                        SOURCE, lot.get("allsop_name"), auction_dt.date(),
                    )
                    stop_all = True
                    break

                partial = _parse_search_result(lot)

                if not partial.get("listing_url"):
                    logger.debug("[%s] Could not build URL for %s", SOURCE, lot.get("allsop_name"))
                    continue

                if on_listing is not None:
                    action = on_listing(partial)
                    if action == "stop":
                        logger.info("[%s] on_listing=stop", SOURCE)
                        stop_all = True
                        break
                    if action == "skip":
                        continue

                lotid = partial.pop("_lotid", None)
                if fetch_details and lotid:
                    time.sleep(random.uniform(0.3, 0.8))
                    _enrich_with_detail(partial, lotid)
                    self._tracker.track("detail", "curl", True)
                else:
                    partial.pop("_lotid", None)

                results.append(partial)

            # Stop paging if we collected enough or all pages done
            if stop_all:
                break
            if page * _PAGE_SIZE >= total:
                break
            page += 1

        logger.info("[%s] Total lots fetched: %d", SOURCE, len(results))
        return results
