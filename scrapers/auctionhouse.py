"""
Auction House UK scraper (auctionhouse.co.uk).

Auction House UK is a regional franchise with 40+ rooms across the UK.
Each region has its own page listing all current lots:
    https://auctionhouse.co.uk/<region>/

This scraper iterates over all known regions and extracts lot URLs.
Lot detail page: https://auctionhouse.co.uk/<region>/auction/lot/<lot_id>

Pass cities=["all"]          → scrape every region
Pass cities=["london"]       → scrape the London region only
Pass cities=["london", "manchester"] → scrape those regions

Env flags:
  FETCH_DETAILS           "0" (default) | "1"
  SCRAPERAPI_RESIDENTIAL  "0" (default) | "1"
"""

import logging
import os
import re
import time
import random
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from bs4 import BeautifulSoup

from scrapers.base import BaseScraper
from scrapers._http import (
    TierTracker,
    direct_curl, browser_get_html, scraperapi_dc, scraperapi_gb,
    make_uc_driver, _UC_HEADLESS,
    parse_guide_price, normalize_property_type, city_from_address, clean_text,
    parse_size_m2, geocode_address,
)

logger = logging.getLogger(__name__)

BASE   = "https://auctionhouse.co.uk"
SOURCE = "auctionhouse"

# All known region slugs from the sitemap
ALL_REGIONS = [
    "online",
    "london",
    "manchester",
    "birmingham",
    "southwest",
    "northeast",
    "northwest",
    "westyorkshire",
    "southyorkshire",
    "eastanglia",
    "essex",
    "hertfordshireandwestessex",
    "sussexandhampshire",
    "leicestershire",
    "lincolnshire",
    "northamptonshire",
    "staffordshire",
    "hullandeastyorkshire",
    "bedsandbucks",
    "chesterfieldandnorthderbyshire",
    "cumbria",
    "northwales",
    "wales",
    "scotland",
    "commercial",
]

_SCRAPERAPI_RESIDENTIAL  = os.getenv("SCRAPERAPI_RESIDENTIAL",  "0") == "1"

OnListingFn = Callable[[Dict[str, Any]], Optional[str]]

# Internal lot links: /london/auction/lot/149431
_LOT_HREF_RE = re.compile(r'href="(/[a-z]+/auction/lot/(\d+))"', re.IGNORECASE)
# External EIG online auction links (online region): https://online.auctionhouse.co.uk/lot/redirect/347329
_EIG_HREF_RE = re.compile(r'href="(https://[^"]+\.(?:eigonlineauctions\.com|auctionhouse\.co\.uk)/lot/redirect/(\d+))"')


# ── HTTP helpers ─────────────────────────────────────────────────────────────

def _get_html(url: str, tracker: TierTracker, context: str, uc_driver=None) -> Optional[str]:
    api_key = os.getenv("SCRAPERAPI_KEY", "")

    html = direct_curl(url, SOURCE)
    tracker.track(context, "curl", bool(html))
    if html and _LOT_HREF_RE.search(html):
        return html

    if uc_driver is not None:
        html = browser_get_html(uc_driver, url, SOURCE)
        tracker.track(context, "browser", bool(html))
        if html:
            return html

    if api_key:
        html = scraperapi_dc(url, api_key, SOURCE)
        tracker.track(context, "scraperapi_dc", bool(html))
        if html:
            return html
        if _SCRAPERAPI_RESIDENTIAL:
            html = scraperapi_gb(url, api_key, SOURCE)
            tracker.track(context, "scraperapi_gb", bool(html))
            if html:
                return html

    return None


# ── Date helpers ─────────────────────────────────────────────────────────────

_DATE_RE = re.compile(
    r"(\d{1,2})(?:st|nd|rd|th)?\s+"
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(\d{4})",
    re.IGNORECASE,
)
_DATE_SLASH_RE = re.compile(r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})")

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


def _parse_date_text(text: str) -> Optional[datetime]:
    m = _DATE_RE.search(text)
    if m:
        try:
            return datetime(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))
        except Exception:
            pass
    m2 = _DATE_SLASH_RE.search(text)
    if m2:
        try:
            day, mon, yr = int(m2.group(1)), int(m2.group(2)), int(m2.group(3))
            if yr < 100:
                yr += 2000
            return datetime(yr, mon, day)
        except Exception:
            pass
    return None


def _get_auction_date_from_page(html: str) -> Optional[datetime]:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    # Look near "auction" keyword
    for keyword in ("next auction", "auction date", "sale date", "auction"):
        idx = text.lower().find(keyword)
        if idx != -1:
            snippet = text[max(0, idx - 10): idx + 120]
            d = _parse_date_text(snippet)
            if d:
                return d
    return _parse_date_text(text)


# ── Lot link extraction ───────────────────────────────────────────────────────

def _extract_lot_links(html: str) -> List[tuple]:
    """Return list of (full_url, lot_id, region) tuples."""
    found = []
    seen = set()
    # Internal: /london/auction/lot/149431
    for m in _LOT_HREF_RE.finditer(html):
        href = m.group(1)
        lot_id = m.group(2)
        region = href.split("/")[1]
        full_url = f"{BASE}{href}"
        if full_url not in seen:
            seen.add(full_url)
            found.append((full_url, lot_id, region))
    # External EIG links (online region): https://online.auctionhouse.co.uk/lot/redirect/347329
    for m in _EIG_HREF_RE.finditer(html):
        full_url = m.group(1)
        lot_id = m.group(2)
        if full_url not in seen:
            seen.add(full_url)
            found.append((full_url, lot_id, "online"))
    return found


# ── Card parse from region listing page ──────────────────────────────────────

_SOLD_STATUSES = re.compile(
    r'\b(?:SOLD|Sold\s+Prior|Withdrawn\s+Prior|Withdrawn|Under\s+Offer|Exchanged)\b',
    re.IGNORECASE,
)


def _is_sold_detail_html(html: str) -> bool:
    """Return True if the detail page indicates the lot is sold/withdrawn."""
    soup = BeautifulSoup(html, "html.parser")
    if soup.find(class_=re.compile(r'\b(?:sold|withdrawn)\b', re.IGNORECASE)):
        return True
    for el in soup.find_all(["span", "div", "p", "strong", "em", "small", "label", "h1", "h2", "h3"]):
        t = el.get_text(" ", strip=True)
        classes = " ".join(el.get("class", [])).lower()
        if len(t) < 50 and _SOLD_STATUSES.search(t):
            return True
        if any(w in classes for w in ("status", "badge", "banner", "overlay", "label", "tag")):
            if _SOLD_STATUSES.search(t):
                return True
    text = soup.get_text(" ", strip=True)
    return bool(
        re.search(r'\bSOLD\b', text) or
        re.search(r'\bSold\s+Prior\b', text, re.IGNORECASE) or
        re.search(r'\bWithdrawn(?:\s+Prior)?\b', text, re.IGNORECASE) or
        re.search(r'\bUnder\s+Offer\b', text, re.IGNORECASE)
    )


def _parse_card(html: str, lot_url: str) -> Optional[Dict[str, Any]]:
    """Extract basic card data from a region listing page.

    Auction House card structure:
      <div class="lot-search-wrapper ...">
        <img class="lot-image" src="/lot-image/NNNNN" alt="... - Full Address, City, Postcode"/>
        <div class="summary-info-wrapper">
          <p class="fw-bold blue-text">3 Bed Terraced House</p>
          <p class="fw-medium blue-text grid-address">82 Harley Road, ...</p>
        </div>
      </div>
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
        path = lot_url.replace(BASE, "")

        anchor = None
        for a in soup.find_all("a", href=True):
            if a["href"].rstrip("/") == path.rstrip("/"):
                anchor = a
                break

        if not anchor:
            return None

        card = anchor
        for _ in range(5):
            if card.parent:
                card = card.parent
            if len(card.get_text(" ", strip=True)) > 40:
                break

        card_text = card.get_text(" ", strip=True)

        # Skip sold / withdrawn / under-offer lots
        card_classes = " ".join(card.get("class", [])).lower()
        if re.search(r'\b(?:sold|withdrawn)\b', card_classes):
            return None
        if card.find(class_=re.compile(r'\b(?:sold|withdrawn)\b', re.IGNORECASE)):
            return None
        for el in card.find_all(["span", "div", "p", "strong", "em", "small"]):
            t = el.get_text(" ", strip=True)
            if len(t) < 50 and _SOLD_STATUSES.search(t):
                return None
        if _SOLD_STATUSES.search(card_text[:200]):
            return None

        # ── Address ───────────────────────────────────────────────────────────
        # Prefer the explicit grid-address paragraph; fall back to img alt text
        address = None
        addr_p = card.select_one("p.grid-address, .fw-medium.blue-text")
        if addr_p:
            address = clean_text(addr_p.get_text(" ", strip=True))
        if not address:
            img_tag = card.find("img", class_="lot-image")
            if img_tag and img_tag.get("alt"):
                # alt format: "Property for Auction in X - Full Address, City"
                alt = img_tag["alt"]
                m = re.search(r" - (.+)$", alt)
                address = clean_text(m.group(1) if m else alt)

        # ── One-liner description (highlighted bold property type line) ────────
        description = None
        bold_p = card.select_one("p.fw-bold")
        if bold_p:
            description = clean_text(bold_p.get_text(" ", strip=True))

        # ── Guide price ───────────────────────────────────────────────────────
        price = parse_guide_price(card_text)

        # ── Bedrooms ──────────────────────────────────────────────────────────
        bedrooms = None
        bed_m = re.search(r"(\d+)\s*[Bb]ed(?:room)?", card_text)
        if bed_m:
            bedrooms = int(bed_m.group(1))

        # ── Property type ──────────────────────────────────────────────────────
        prop_type = normalize_property_type(description or card_text)

        # ── Lot number ────────────────────────────────────────────────────────
        lot_m = re.search(r"[Ll]ot\s*[-:]?\s*(\d+)", card_text)
        lot_number = int(lot_m.group(1)) if lot_m else None

        # ── Image ─────────────────────────────────────────────────────────────
        # Listing page uses relative /lot-image/NNNNN src — make absolute
        image_url = None
        img = card.find("img", class_="lot-image")
        if img and img.get("src"):
            src = img["src"]
            image_url = src if src.startswith("http") else f"{BASE}{src}"
        if not image_url:
            img = card.find("img")
            if img and img.get("src"):
                src = img["src"]
                if src.startswith("http") and "eigproperty" in src.lower():
                    image_url = src

        return {
            "address":       address,
            "price":         price,
            "bedrooms":      bedrooms,
            "property_type": prop_type,
            "lot_number":    lot_number,
            "image_url":     image_url,
            "description":   description,
        }
    except Exception:
        logger.debug("[%s] card parse error for %s", SOURCE, lot_url, exc_info=True)
        return None


# ── Detail page parser ────────────────────────────────────────────────────────

def _parse_detail_page(html: str, auction_date_hint: Optional[datetime]) -> Dict[str, Any]:
    """Extract all available fields from an Auction House lot detail page."""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    # Address — Auctionhouse uses <title>: "Property for Auction in X - <Address>"
    address = None
    title_tag = soup.find("title")
    if title_tag:
        title_text = title_tag.get_text(" ", strip=True)
        # "Property for Auction in London - 82 Harley Road, ..."
        m = re.search(r"[-–]\s+(.+)$", title_text)
        if m:
            address = clean_text(m.group(1))
    if not address:
        h1 = soup.find("h1")
        if h1:
            address = clean_text(h1.get_text(" ", strip=True))

    # Guide price
    price = None
    gp_m = re.search(r"[Gg]uide\s+[Pp]rice[:\s]*([£\d,\.\+\-\sMm]+)", text)
    if gp_m:
        price = parse_guide_price(gp_m.group(1))
    if price is None:
        price = parse_guide_price(text)

    # Bedrooms / bathrooms
    bedrooms = bathrooms = None
    bed_m = re.search(r"(\d+)\s*[Bb]ed(?:room)?", text)
    if bed_m:
        bedrooms = int(bed_m.group(1))
    bath_m = re.search(r"(\d+)\s*[Bb]ath(?:room)?", text)
    if bath_m:
        bathrooms = int(bath_m.group(1))

    # Floor area
    size_m2 = parse_size_m2(text)

    # Property type
    prop_type = None
    for pattern in (
        r"(\d+\s*[Bb]ed(?:room)?\s+[\w\s]+)",
        r"(flat|apartment|house|bungalow|terraced|semi|detached|land|commercial)",
    ):
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            prop_type = normalize_property_type(m.group(1))
            break

    # Description — prefer the lot-details div (full property write-up);
    # fall back to the longest non-boilerplate paragraph.
    _BOILERPLATE = (
        "guides are provided", "administration charge", "disbursements",
        "important notice", "we draw your attention", "special conditions",
        "buyer's premium", "disclaimer", "administration fee",
        "map preview", "working with together",
    )
    description = None
    lot_details_div = soup.find("div", class_="lot-details")
    if lot_details_div:
        # Collect property paragraphs inside lot-details, skip boilerplate
        parts = []
        for p in lot_details_div.find_all("p"):
            t = clean_text(p.get_text(" ", strip=True))
            if t and len(t) > 20 and not any(b in t.lower() for b in _BOILERPLATE):
                parts.append(t)
        if parts:
            description = " ".join(parts)
    if not description:
        best_len = 0
        for p in soup.find_all("p"):
            t = clean_text(p.get_text(" ", strip=True))
            if not t or len(t) < 30:
                continue
            if any(b in t.lower() for b in _BOILERPLATE):
                continue
            if len(t) > best_len:
                best_len = len(t)
                description = t

    # Images are lazy-loaded (JS carousel) — prefer /lot-image/ src or eigproperty CDN
    image_url = None
    skip_terms = ("logo", "icon", "facebook", "twitter", "linkedin", "youtube", "instagram",
                  "noscript", "tr?id=", "arrow", ".svg")
    # First try lot-image relative URL (on detail page it may appear as full URL)
    for img in soup.find_all("img"):
        src = img.get("src", "")
        alt = (img.get("alt", "") or "").lower()
        if not src:
            continue
        if any(t in src.lower() or t in alt for t in skip_terms):
            continue
        if "/lot-image/" in src:
            image_url = src if src.startswith("http") else f"{BASE}{src}"
            break
        if src.startswith("http") and "eigproperty" in src.lower():
            image_url = src
            break

    # Agent contact
    agent_phone = None
    phone_m = re.search(r"(\d[\d\s\-]{8,14}\d)", text)
    if phone_m:
        agent_phone = phone_m.group(1).strip()

    # Auction date — page format: "09:30, 24 June 2026 | Lot N <address>"
    auction_date = None
    # Try broad date search first (most reliable for this site)
    auction_date = _parse_date_text(text)
    if auction_date is None:
        for kw in ("for sale by auction", "auction date", "sale date", "lot date"):
            idx = text.lower().find(kw)
            if idx != -1:
                snippet = text[max(0, idx - 10): idx + 150]
                auction_date = _parse_date_text(snippet)
                if auction_date:
                    break
    if auction_date is None:
        auction_date = auction_date_hint

    # Lat / lng
    lat = lng = None
    lat_m = re.search(r'"lat(?:itude)?"\s*:\s*([-\d.]+)', html)
    lng_m = re.search(r'"l(?:ng|on(?:gitude)?)"\s*:\s*([-\d.]+)', html)
    if lat_m and lng_m:
        try:
            lat, lng = float(lat_m.group(1)), float(lng_m.group(1))
            if not (49.0 < lat < 61.0 and -8.0 < lng < 2.0):
                lat = lng = None
        except ValueError:
            pass
    if lat is None and address:
        lat, lng = geocode_address(address)

    return {
        "address":       address,
        "price":         price,
        "bedrooms":      bedrooms,
        "bathrooms":     bathrooms,
        "size_m2":       size_m2,
        "property_type": prop_type,
        "description":   description,
        "image_url":     image_url,
        "agent_name":    "Auction House UK",
        "agent_phone":   agent_phone,
        "auction_date":  auction_date,
        "lat":           lat,
        "lng":           lng,
    }


# ── Main scraper class ────────────────────────────────────────────────────────

class AuctionHouseScraper(BaseScraper):
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
        Scrape Auction House lots.

        city:  a region slug (e.g. "london", "manchester") to scrape one region,
               or "all" to scrape every region.
        """
        city_lower = city.lower().strip()
        if city_lower in ("all", ""):
            regions = ALL_REGIONS
        elif city_lower in ALL_REGIONS:
            regions = [city_lower]
        else:
            # Accept partial match
            regions = [r for r in ALL_REGIONS if city_lower in r] or ALL_REGIONS

        results: List[Dict[str, Any]] = []
        uc_driver = None
        stop_all = False

        try:
            for region in regions:
                if stop_all:
                    break

                region_url = f"{BASE}/{region}/"
                logger.info("[%s] Fetching region: %s", SOURCE, region_url)

                html = _get_html(region_url, self._tracker, "search", uc_driver)
                if not html:
                    logger.warning("[%s] No HTML for region: %s", SOURCE, region)
                    continue

                lot_links = _extract_lot_links(html)
                if not lot_links:
                    logger.info("[%s] No lots found in region: %s", SOURCE, region)
                    continue

                auction_date = _get_auction_date_from_page(html)
                logger.info(
                    "[%s] Region %s: %d lots, auction_date=%s",
                    SOURCE, region, len(lot_links), auction_date,
                )

                for lot_url, lot_id, lot_region in lot_links:
                    if stop_all:
                        break

                    card = _parse_card(html, lot_url)
                    if card is None:
                        continue

                    card_lot_num = card.get("lot_number")
                    partial = {
                        "source":        SOURCE,
                        "listing_url":   lot_url,
                        "city":          city_from_address(card.get("address")) or lot_region,
                        "address":       card.get("address"),
                        "price":         card.get("price"),
                        "bedrooms":      card.get("bedrooms"),
                        "bathrooms":     None,
                        "size_m2":       None,
                        "property_type": card.get("property_type"),
                        "description":   card.get("description"),
                        "agent_name":    "Auction House UK",
                        "agent_phone":   None,
                        "image_url":     card.get("image_url"),
                        "lat":           None,
                        "lng":           None,
                        "auction_date":  auction_date,
                        "lot_number":    str(card_lot_num) if card_lot_num else None,
                    }

                    if on_listing is not None:
                        action = on_listing(partial)
                        if action == "stop":
                            logger.info("[%s] on_listing=stop at region %s", SOURCE, region)
                            stop_all = True
                            break
                        if action == "skip":
                            continue

                    if fetch_details:
                        time.sleep(random.uniform(0.4, 1.0))
                        detail_html = _get_html(lot_url, self._tracker, "detail", uc_driver)
                        if detail_html:
                            if _is_sold_detail_html(detail_html):
                                logger.debug("[%s] Skipping sold/withdrawn lot (detail page): %s", SOURCE, lot_url)
                                continue
                            detail = _parse_detail_page(detail_html, auction_date)
                            for k, v in detail.items():
                                if v is not None:
                                    partial[k] = v
                            if partial.get("city") is None and partial.get("address"):
                                partial["city"] = city_from_address(partial["address"])

                    results.append(partial)

                time.sleep(random.uniform(1.0, 2.0))

        except Exception:
            logger.exception("[%s] Unexpected error", SOURCE)
        finally:
            if uc_driver:
                try:
                    uc_driver.quit()
                except Exception:
                    pass

        logger.info("[%s] Total lots fetched: %d", SOURCE, len(results))
        return results
