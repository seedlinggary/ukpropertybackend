"""
Strettons Chartered Surveyors auction scraper (strettons.co.uk).

Strettons is a London-based auction house. Their lot listing pages are
JavaScript-rendered (React app), so this scraper relies on the browser tier
or ScraperAPI render to get the content.

Catalogue pages checked:
  /auctions/available-lots/    — current available lots
  /auctions/current-catalogue/ — full current catalogue

The detail page for each lot is at a URL like:
  /auctions/lot/<lot-id>/  (pattern inferred from page links)

Pass cities=["all"] when calling run_scrape; city is ignored.

Env flags:
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
    direct_curl, scraperapi_render, scraperapi_gb,
    make_uc_driver, _UC_HEADLESS,
    parse_guide_price, normalize_property_type, city_from_address, clean_text,
    parse_size_m2, geocode_address,
)

logger = logging.getLogger(__name__)

BASE   = "https://www.strettons.co.uk"
SOURCE = "strettons"

CATALOGUE_URLS = [
    f"{BASE}/auctions/available-lots/",
    f"{BASE}/auctions/current-catalogue/",
]

_SCRAPERAPI_RESIDENTIAL  = os.getenv("SCRAPERAPI_RESIDENTIAL",  "0") == "1"

OnListingFn = Callable[[Dict[str, Any]], Optional[str]]

# Strettons lot pages: /auction-residential-property-for-sale/... or /auction-commercial-property-for-sale/...
_LOT_HREF_RE = re.compile(
    r'href="(/auction-(?:(?:residential|commercial)-)?property-for-sale/[^"]+/)"'
)

SITEMAP_URL = f"{BASE}/properties/details.xml"
_SITEMAP_LOT_RE = re.compile(
    r"<loc>(https://www\.strettons\.co\.uk"
    r"/auction-(?:(?:residential|commercial)-)?property-for-sale/[^<]+/)</loc>"
)


def _lot_urls_from_sitemap() -> List[str]:
    """Extract auction lot URLs from the sitemap XML.

    The sitemap is server-rendered XML — no JS rendering needed.
    Used as a fallback when catalogue pages yield zero lot links.
    """
    try:
        html = direct_curl(SITEMAP_URL, SOURCE)
        if not html:
            logger.warning("[%s] sitemap returned nothing", SOURCE)
            return []
        urls = _SITEMAP_LOT_RE.findall(html)
        logger.info("[%s] sitemap: %d auction lot URLs", SOURCE, len(urls))
        return urls
    except Exception:
        logger.warning("[%s] sitemap fetch failed", SOURCE, exc_info=True)
        return []


def _is_content_rich(html: str) -> bool:
    """Check if HTML has actual lot listings (not just nav/marketing skeleton)."""
    low = html.lower()
    # "guide price" in visible text (not just CSS class names like "icon-property-price")
    # and/or "lot N" as actual text content
    return "guide price" in low or bool(re.search(r"\blot\s+\d+\b", low))


# ── HTTP helpers with JS-render fallback ─────────────────────────────────────

def _get_html(
    url: str,
    tracker: TierTracker,
    context: str,
    uc_driver=None,
    require_content: bool = True,
) -> Optional[str]:
    """
    Full 4-tier chain.  For Strettons' JS-rendered pages we upgrade tier-3
    from standard datacenter to ScraperAPI render (5cr) when content check fails.
    """
    api_key = os.getenv("SCRAPERAPI_KEY", "")

    # Tier 1: curl
    html = direct_curl(url, SOURCE)
    tracker.track(context, "curl", bool(html))
    if html and (not require_content or _is_content_rich(html)):
        return html

    # Tier 2: browser (handles React/JS rendering)
    if uc_driver is not None:
        html = _browser_get_with_wait(uc_driver, url)
        tracker.track(context, "browser", bool(html))
        if html and (not require_content or _is_content_rich(html)):
            return html

    if api_key:
        # Tier 3: ScraperAPI with JS rendering (5 credits — needed for React)
        html = scraperapi_render(url, api_key, SOURCE)
        tracker.track(context, "scraperapi_dc", bool(html))
        if html and (not require_content or _is_content_rich(html)):
            return html

        # Tier 4: ScraperAPI residential (5 credits)
        if _SCRAPERAPI_RESIDENTIAL:
            html = scraperapi_gb(url, api_key, SOURCE)
            tracker.track(context, "scraperapi_gb", bool(html))
            if html:
                return html

    return None


def _browser_get_with_wait(driver, url: str) -> Optional[str]:
    """Navigate in browser and wait for React content to render."""
    try:
        driver.get(url)
        time.sleep(random.uniform(10.0, 12.0))
        # Scroll to trigger lazy-loaded lot cards
        for _ in range(8):
            driver.execute_script("window.scrollBy(0, 800);")
            time.sleep(0.7)
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(1.5)
        return driver.page_source
    except Exception:
        logger.debug("[%s/browser] failed for %s", SOURCE, url, exc_info=True)
        return None


# ── Date helpers ─────────────────────────────────────────────────────────────

_DATE_RE = re.compile(
    r"(\d{1,2})(?:st|nd|rd|th)?\s+"
    r"(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
    r"\s+(\d{2,4})",
    re.IGNORECASE,
)

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6,
    "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "september": 9,
    "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}


def _parse_date(text: str) -> Optional[datetime]:
    m = _DATE_RE.search(text)
    if not m:
        return None
    try:
        year = int(m.group(3))
        if year < 100:
            year += 2000  # "26" → 2026
        month_key = m.group(2).lower()[:3]
        return datetime(year, _MONTHS[month_key], int(m.group(1)))
    except Exception:
        return None


def _get_auction_date_from_page(html: str) -> Optional[datetime]:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)
    for keyword in ("next auction", "auction date", "auction"):
        idx = text.lower().find(keyword)
        if idx != -1:
            snippet = text[max(0, idx - 10): idx + 120]
            d = _parse_date(snippet)
            if d:
                return d
    return _parse_date(text)


# ── Lot link extraction ───────────────────────────────────────────────────────

def _extract_lot_links(html: str) -> List[str]:
    """Return deduplicated lot page URLs."""
    import html as _html_mod
    seen, found = set(), []
    for m in _LOT_HREF_RE.finditer(html):
        path = _html_mod.unescape(m.group(1))  # decode &amp; → &
        full = f"{BASE}{path}"
        if full not in seen:
            seen.add(full)
            found.append(full)
    return found


# ── Card parse from catalogue page ───────────────────────────────────────────

def _is_sold_lot(element) -> bool:
    """Return True if any sold/under-offer indicator is found in the card element."""
    if element is None:
        return False
    # CSS class on element itself or any descendant
    own_classes = " ".join(element.get("class", [])).lower()
    if "sold" in own_classes or "withdrawn" in own_classes:
        return True
    if element.find(class_=re.compile(r'\b(?:sold|withdrawn)\b', re.IGNORECASE)):
        return True
    # Text badge check — look for short standalone status elements first
    for el in element.find_all(["span", "div", "p", "strong", "em", "small", "label"]):
        t = el.get_text(" ", strip=True)
        if len(t) < 40 and re.search(r'\bsold\b', t, re.IGNORECASE):
            return True
    # Full card text — uppercase SOLD (banner/overlay) or Under Offer / Withdrawn
    text = element.get_text(" ", strip=True)
    if re.search(r'\bSOLD\b', text):
        return True
    if re.search(r'\bUnder\s+Offer\b', text, re.IGNORECASE):
        return True
    if re.search(r'\bWithdrawn\b', text, re.IGNORECASE):
        return True
    return False


def _is_sold_detail_html(html: str) -> bool:
    """Return True if the detail page itself indicates the lot is sold/withdrawn."""
    soup = BeautifulSoup(html, "html.parser")
    # CSS class anywhere on the page
    if soup.find(class_=re.compile(r'\b(?:sold|withdrawn)\b', re.IGNORECASE)):
        return True
    # Short badge/status elements
    for el in soup.find_all(["span", "div", "p", "strong", "em", "small", "label", "h1", "h2"]):
        t = el.get_text(" ", strip=True)
        classes = " ".join(el.get("class", [])).lower()
        if len(t) < 40 and re.search(r'\bsold\b', t, re.IGNORECASE):
            return True
        if any(w in classes for w in ("status", "badge", "banner", "overlay", "label", "tag")):
            if re.search(r'\bsold\b', t, re.IGNORECASE):
                return True
    # Uppercase SOLD banner anywhere in page text
    text = soup.get_text(" ", strip=True)
    if re.search(r'\bSOLD\b', text):
        return True
    if re.search(r'\bUnder\s+Offer\b', text, re.IGNORECASE):
        return True
    if re.search(r'\bWithdrawn\b', text, re.IGNORECASE):
        return True
    return False


def _parse_card_from_page(html: str, lot_url: str) -> Optional[Dict[str, Any]]:
    """Extract basic card data from the catalogue page for a given lot URL."""
    try:
        soup = BeautifulSoup(html, "html.parser")
        path = lot_url.replace(BASE, "")

        # Find the "link-title" anchor (contains address + lot date spans).
        # Multiple anchors share the same href; we want the one with heading_ttl.
        # BS4 decodes &amp; -> & in href so unescape our path for comparison.
        import html as _html_mod
        path_decoded = _html_mod.unescape(path)
        anchor = None
        for a in soup.find_all("a", href=True):
            if a["href"].rstrip("/") == path_decoded.rstrip("/"):
                if a.find("span", class_=re.compile("heading_ttl")):
                    anchor = a
                    break
        if not anchor:
            return None

        # Address: <span class="heading_ttl auction_ttl"> inside the anchor
        ttl_span = anchor.find("span", class_=re.compile("heading_ttl|auction_ttl"))
        address = clean_text(ttl_span.get_text(" ", strip=True)) if ttl_span else None

        # Auction date + lot number: <address class="lot_number">04 Jun 26 - Lot 5</address>
        auction_date = None
        lot_number = None
        lot_addr = anchor.find("address", class_=re.compile("lot_number"))
        if lot_addr:
            lot_addr_text = lot_addr.get_text(" ", strip=True)
            auction_date = _parse_date(lot_addr_text)
            lot_m = re.search(r"[Ll]ot\s*(\d+)", lot_addr_text)
            if lot_m:
                lot_number = lot_m.group(1)

        # Walk up to the per-lot card container (h2 parent = individual card div).
        # Stop at the first block-level div that is a direct parent of the h2.
        card = anchor
        for _ in range(4):
            if card.parent:
                card = card.parent
            if card.name in ("div", "article", "li", "section"):
                break

        # Price: prefer the .office-info div (contains "Guide Price £X") within the card.
        # Fall back to full card text search.
        price = None
        info_div = card.find(class_=re.compile(r"\boffice-info\b"))
        if info_div:
            price = parse_guide_price(info_div.get_text(" ", strip=True))
        if price is None:
            price = parse_guide_price(card.get_text(" ", strip=True))

        # Bedrooms from URL slug (e.g. "1-bedroom-house")
        bedrooms = None
        bed_m = re.search(r"(\d+)-bedroom", path)
        if bed_m:
            bedrooms = int(bed_m.group(1))
        else:
            bed_m = re.search(r"(\d+)\s*[Bb]ed(?:room)?", card.get_text(" ", strip=True))
            if bed_m:
                bedrooms = int(bed_m.group(1))

        # Image — main property images use /i/property/NNNNN/... path.
        # Agent/staff photos use short /i/<name> paths — skip those.
        # The lot's hex ID appears inside api_sources/{hex}/ in the image path.
        image_url = None
        lot_id_hash = path.rstrip("/").split("-")[-1]  # hex suffix e.g. 6a0735030a...
        for img in soup.find_all("img"):
            src = img.get("src", "")
            if "/i/property/" in src and (not lot_id_hash or lot_id_hash in src):
                image_url = src
                break
        if not image_url:
            # Fallback: any /i/property/ image on the page
            for img in soup.find_all("img"):
                src = img.get("src", "")
                if "/i/property/" in src:
                    image_url = src
                    break

        if _is_sold_lot(card):
            logger.debug("[%s] Sold lot (card): %s", SOURCE, lot_url)
            return {"_sold": True}

        return {
            "address":      address,
            "price":        price,
            "bedrooms":     bedrooms,
            "auction_date": auction_date,
            "lot_number":   lot_number,
            "image_url":    image_url,
        }
    except Exception:
        logger.debug("[%s] card parse error for %s", SOURCE, lot_url, exc_info=True)
        return None  # parse failure — caller should still attempt detail page


# ── Detail page parser ────────────────────────────────────────────────────────

def _parse_detail_page(html: str, auction_date_hint: Optional[datetime]) -> Dict[str, Any]:
    """Extract all available fields from a Strettons lot detail page."""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    # Address
    address = None
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
        r"(\d+\s*[Bb]ed(?:room)?\s+[\w\s-]+)",
        r"(flat|apartment|house|bungalow|terraced|semi|detached|land|commercial|hmo)",
    ):
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            prop_type = normalize_property_type(m.group(1))
            break

    # Description
    description = None
    best_len = 0
    for p in soup.find_all("p"):
        t = clean_text(p.get_text(" ", strip=True))
        if t and len(t) > best_len:
            best_len = len(t)
            description = t

    # Image — main property images use /i/property/NNNNN/... path.
    # Agent/staff photos use short /i/<name> paths — skip those.
    image_url = None
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if src and "/i/property/" in src:
            image_url = src
            break

    # Agent
    agent_name = "Strettons"
    agent_phone = None
    phone_m = re.search(r"(\d[\d\s\-]{8,14}\d)", text)
    if phone_m:
        agent_phone = phone_m.group(1).strip()

    # Auction date
    auction_date = None
    for kw in ("auction date", "sale date", "lot date", "auction"):
        idx = text.lower().find(kw)
        if idx != -1:
            snippet = text[max(0, idx - 10): idx + 120]
            auction_date = _parse_date(snippet)
            if auction_date:
                break
    if auction_date is None:
        auction_date = _parse_date(text)
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
        "agent_name":    agent_name,
        "agent_phone":   agent_phone,
        "auction_date":  auction_date,
        "lat":           lat,
        "lng":           lng,
    }


# ── Main scraper class ────────────────────────────────────────────────────────

class StrrettonsScraper(BaseScraper):
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
        Scrape all available Strettons auction lots.

        The `city` parameter is ignored.
        Browser tier or ScraperAPI render required (JS-rendered pages).
        """
        results: List[Dict[str, Any]] = []
        stop_all = False
        seen_urls: set = set()
        found_lot_urls: set = set()  # tracks all lot URLs seen from catalogues

        # Strettons is a React app — browser tier required for JS rendering.
        uc_driver = None
        try:
            uc_driver = make_uc_driver(_UC_HEADLESS)
            logger.info("[%s] Browser launched", SOURCE)
        except Exception:
            logger.warning("[%s] Browser unavailable — ScraperAPI render only", SOURCE, exc_info=True)

        try:
            for catalogue_url in CATALOGUE_URLS:
                if stop_all:
                    break

                logger.info("[%s] Fetching catalogue: %s", SOURCE, catalogue_url)
                html = _get_html(catalogue_url, self._tracker, "search", uc_driver)

                if not html:
                    logger.warning("[%s] All tiers failed for %s", SOURCE, catalogue_url)
                    continue

                if not _is_content_rich(html):
                    logger.warning("[%s] Page has no lot content: %s", SOURCE, catalogue_url)
                    continue

                auction_date = _get_auction_date_from_page(html)
                lot_urls = _extract_lot_links(html)

                # Deduplicate across catalogue pages
                lot_urls = [u for u in lot_urls if u not in seen_urls]
                seen_urls.update(lot_urls)
                found_lot_urls.update(lot_urls)

                logger.info(
                    "[%s] %s: %d lots, auction_date=%s",
                    SOURCE, catalogue_url, len(lot_urls), auction_date,
                )

                for lot_url in lot_urls:
                    if stop_all:
                        break

                    card = _parse_card_from_page(html, lot_url)

                    # _sold=True → confirmed sold on the catalogue card; skip cheaply.
                    # None → CSS parse failure; still attempt the detail page.
                    if card is not None and card.get("_sold"):
                        continue

                    card_date = card.get("auction_date") if card else None

                    partial = {
                        "source":        SOURCE,
                        "listing_url":   lot_url,
                        "city":          city_from_address(card.get("address") if card else None),
                        "address":       card.get("address") if card else None,
                        "price":         card.get("price") if card else None,
                        "bedrooms":      card.get("bedrooms") if card else None,
                        "bathrooms":     None,
                        "size_m2":       None,
                        "property_type": None,
                        "description":   None,
                        "agent_name":    "Strettons",
                        "agent_phone":   None,
                        "image_url":     card.get("image_url") if card else None,
                        "lat":           None,
                        "lng":           None,
                        "auction_date":  card_date or auction_date,
                        "lot_number":    card.get("lot_number") if card else None,
                    }

                    if on_listing is not None:
                        action = on_listing(partial)
                        if action == "stop":
                            logger.info("[%s] on_listing=stop", SOURCE)
                            stop_all = True
                            break
                        if action == "skip":
                            continue

                    if fetch_details:
                        time.sleep(random.uniform(0.5, 1.2))
                        detail_html = _get_html(lot_url, self._tracker, "detail", uc_driver)
                        if detail_html:
                            if _is_sold_detail_html(detail_html):
                                logger.debug("[%s] Skipping sold lot (detail page): %s", SOURCE, lot_url)
                                continue
                            detail = _parse_detail_page(detail_html, auction_date)
                            for k, v in detail.items():
                                if v is not None:
                                    # The card-level date from <address class="lot_number">
                                    # is lot-specific and more reliable than any date found
                                    # by text-searching the detail page. Preserve it.
                                    if k == "auction_date" and card_date is not None:
                                        continue
                                    partial[k] = v
                            if partial.get("city") is None and partial.get("address"):
                                partial["city"] = city_from_address(partial["address"])

                    results.append(partial)

            # ── Sitemap fallback ────────────────────────────────────────────────
            # When catalogue pages return zero lot links (JS rendering unavailable
            # or Strettons changed their React component structure), fall back to
            # the sitemap XML which lists all lot URLs as plain server-rendered XML.
            if not found_lot_urls and not stop_all:
                logger.info(
                    "[%s] No lot URLs from catalogue pages — trying sitemap fallback",
                    SOURCE,
                )
                sitemap_urls = _lot_urls_from_sitemap()
                new_urls = [u for u in sitemap_urls if u not in seen_urls]
                seen_urls.update(new_urls)
                logger.info(
                    "[%s] sitemap: %d new lot URLs to process", SOURCE, len(new_urls)
                )

                for lot_url in new_urls:
                    if stop_all:
                        break

                    partial = {
                        "source":        SOURCE,
                        "listing_url":   lot_url,
                        "city":          None,
                        "address":       None,
                        "price":         None,
                        "bedrooms":      None,
                        "bathrooms":     None,
                        "size_m2":       None,
                        "property_type": None,
                        "description":   None,
                        "agent_name":    "Strettons",
                        "agent_phone":   None,
                        "image_url":     None,
                        "lat":           None,
                        "lng":           None,
                        "auction_date":  None,
                        "lot_number":    None,
                    }

                    if on_listing is not None:
                        action = on_listing(partial)
                        if action == "stop":
                            logger.info("[%s] on_listing=stop (sitemap)", SOURCE)
                            stop_all = True
                            break
                        if action == "skip":
                            continue

                    if fetch_details:
                        time.sleep(random.uniform(0.5, 1.2))
                        detail_html = _get_html(lot_url, self._tracker, "detail", uc_driver)
                        if detail_html:
                            if _is_sold_detail_html(detail_html):
                                logger.debug(
                                    "[%s] Skipping sold lot (sitemap/detail): %s",
                                    SOURCE, lot_url,
                                )
                                continue
                            detail = _parse_detail_page(detail_html, None)
                            for k, v in detail.items():
                                if v is not None:
                                    partial[k] = v
                            if partial.get("city") is None and partial.get("address"):
                                partial["city"] = city_from_address(partial["address"])

                    results.append(partial)

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
