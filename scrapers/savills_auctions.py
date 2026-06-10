"""
Savills Auctions scraper (auctions.savills.co.uk).

Flow:
  1. Fetch /upcoming-auctions to discover current catalog slugs
     (e.g. "9-june-2026-224", "23-june-2026-225").
  2. For each catalog, paginate through /auctions/[slug]/page-[n]
     extracting all property lot URLs.
  3. From each catalog page, parse basic card data (lot, address, price, type).
  4. Optionally visit each property detail page for bedrooms/description/image.

Pass cities=["all"] when calling run_scrape; the city argument is ignored —
all upcoming auctions are scraped regardless.

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

BASE            = "https://auctions.savills.co.uk"
UPCOMING_URL    = f"{BASE}/upcoming-auctions"
MAX_PAGES       = 30

_SCRAPERAPI_RESIDENTIAL  = os.getenv("SCRAPERAPI_RESIDENTIAL",  "0") == "1"

SOURCE = "savills_auctions"

OnListingFn = Callable[[Dict[str, Any]], Optional[str]]


# ── HTTP helpers (thin wrappers that also track stats) ───────────────────────

def _browser_get_catalog(driver, url: str) -> Optional[str]:
    """
    Fetch a Savills catalog page in the browser with extended wait + scroll.
    Lot cards are loaded via AJAX after initial render — need ~12s total.
    """
    try:
        driver.get(url)
        time.sleep(random.uniform(10.0, 12.0))
        for _ in range(8):
            driver.execute_script("window.scrollBy(0, 800);")
            time.sleep(0.8)
        driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
        time.sleep(3.0)
        return driver.page_source
    except Exception:
        logger.debug("[%s/browser] catalog fetch failed for %s", SOURCE, url, exc_info=True)
        return None


def _get_html(
    url: str,
    tracker: TierTracker,
    context: str,
    uc_driver=None,
    is_catalog: bool = False,
) -> Optional[str]:
    api_key = os.getenv("SCRAPERAPI_KEY", "")

    html = direct_curl(url, SOURCE)
    tracker.track(context, "curl", bool(html))
    if html:
        return html

    if uc_driver is not None:
        # Catalog pages need extended wait for AJAX lot-card rendering
        if is_catalog:
            html = _browser_get_catalog(uc_driver, url)
        else:
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


# ── Discovery: upcoming catalog slugs ────────────────────────────────────────

def _get_catalog_urls(uc_driver=None, tracker: Optional[TierTracker] = None) -> List[str]:
    """
    Fetch /upcoming-auctions and extract catalog hrefs.
    Returns full URLs like https://auctions.savills.co.uk/auctions/9-june-2026-224
    """
    tr = tracker or TierTracker()
    html = _get_html(UPCOMING_URL, tr, "search", uc_driver)
    if not html:
        logger.warning("[%s] Could not fetch upcoming-auctions page", SOURCE)
        return []

    # Links can be absolute or relative:
    #   href="https://auctions.savills.co.uk/auctions/9-june-2026-224"
    #   href="/auctions/9-june-2026-224"
    pattern = re.compile(
        r'href=["\'](?:https?://auctions\.savills\.co\.uk)?(/auctions/[\w-]+-\d+)["\']'
    )
    seen, urls = set(), []
    for m in pattern.finditer(html):
        slug = m.group(1)
        full = f"{BASE}{slug}"
        if full not in seen:
            seen.add(full)
            urls.append(full)

    logger.info("[%s] Found %d upcoming catalog(s): %s", SOURCE, len(urls), urls)
    return urls


# ── Per-catalog page: extract property links ──────────────────────────────────

# Lot links: http://auctions.savills.co.uk/auctions/[catalog-slug]/[address-slug]-[lot-id]
# Lot IDs are 4+ digits (22912, 23197) — excludes /page-1, /page-2 pagination links.
_LOT_HREF_RE = re.compile(
    r'href=["\'](?:https?://auctions\.savills\.co\.uk)?(/auctions/[\w-]+-\d+/[\w-]*-(\d{4,}))["\']'
)


def _extract_lot_links_from_page(html: str) -> List[tuple]:
    """
    Return list of (full_url, lot_id_str) tuples found in the catalog page HTML.
    The lot_id is the trailing numeric segment of the lot URL.
    """
    found = []
    for m in _LOT_HREF_RE.finditer(html):
        path, lot_id = m.group(1), m.group(2)
        full_url = f"https://auctions.savills.co.uk{path}"
        found.append((full_url, lot_id))
    # deduplicate preserving order
    seen, out = set(), []
    for pair in found:
        if pair[0] not in seen:
            seen.add(pair[0])
            out.append(pair)
    return out


def _has_more_pages(html: str, page: int) -> bool:
    return f"page-{page + 1}" in html or f'page={page + 1}' in html


_SOLD_STATUSES = re.compile(
    r'\b(?:SOLD|Sold\s+Prior|Withdrawn\s+Prior|Withdrawn|Under\s+Offer|Exchanged)\b',
    re.IGNORECASE,
)


def _is_sold_lot(element) -> bool:
    """Return True if the lot element shows a sold/withdrawn/under-offer status."""
    if element is None:
        return False
    # CSS class on element itself or any descendant
    own_classes = " ".join(element.get("class", [])).lower()
    if re.search(r'\b(?:sold|withdrawn)\b', own_classes):
        return True
    if element.find(class_=re.compile(r'\b(?:sold|withdrawn)\b', re.IGNORECASE)):
        return True
    # Short badge/status elements (under 50 chars to avoid description false positives)
    for el in element.find_all(["span", "div", "p", "strong", "em", "small", "label"]):
        t = el.get_text(" ", strip=True)
        if len(t) < 50 and _SOLD_STATUSES.search(t):
            return True
    # Full text — uppercase SOLD / exact status phrases
    text = element.get_text(" ", strip=True)
    return bool(_SOLD_STATUSES.search(text[:200]))  # only check leading text to avoid description hits


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
    # Uppercase SOLD or exact phrase anywhere in page
    text = soup.get_text(" ", strip=True)
    return bool(re.search(r'\bSOLD\b', text) or
                re.search(r'\bSold\s+Prior\b', text, re.IGNORECASE) or
                re.search(r'\bWithdrawn(?:\s+Prior)?\b', text, re.IGNORECASE) or
                re.search(r'\bUnder\s+Offer\b', text, re.IGNORECASE))


# ── Catalog page: quick card parse (no detail fetch) ─────────────────────────

def _parse_card_from_page(html: str, lot_id: str) -> Optional[Dict[str, Any]]:
    """
    Extract listing data from a Savills catalog page card.

    Savills renders each lot as:
      <li class="lot" data-lot_id="NNNNN">
        <ul class="lot-image-list">
          <li class="lot-image slick-slide slick-current slick-active">
            <a title="Full Address, City, Postcode"><img src="https://resize.auctions.savills.co.uk/..."/></a>
          </li>
        </ul>
        <div class="lot-content">
          <p class="lot-number">Lot N</p>
          <p class="guide-price"><span class="value">£NNN,NNN</span></p>
          <a class="lot-name" title="Full Address, City, Postcode">...</a>
          <div class="lot-details"><ul><li>3 bed flat</li><li>Leasehold</li>...</ul></div>
        </div>
      </li>
    """
    try:
        soup = BeautifulSoup(html, "html.parser")

        # Locate lot container directly by data-lot_id
        lot_li = soup.find("li", attrs={"data-lot_id": lot_id})
        if not lot_li:
            return None

        if _is_sold_lot(lot_li):
            return None

        lot_content = lot_li.find("div", class_="lot-content") or lot_li

        # ── Address ───────────────────────────────────────────────────────────
        # Primary: <a class="lot-name" title="Full Address, City, Postcode">
        address = None
        lot_name_a = lot_content.find("a", class_="lot-name")
        if lot_name_a:
            address = clean_text(lot_name_a.get("title") or lot_name_a.get_text(" ", strip=True))
        # Fallback: title on any image anchor (slick slide)
        if not address:
            for a in lot_li.find_all("a", title=True):
                t = clean_text(a.get("title", ""))
                if t and len(t) > 10 and not re.match(r"^Lot\s*\d*$", t, re.IGNORECASE):
                    address = t
                    break
        if address and re.match(r"^Lot\s*\d*$", address.strip(), re.IGNORECASE):
            address = None

        # ── Image ────────────────────────────────────────────────────────────
        # Prefer the active slick slide; fall back to any resize.auctions.savills img
        image_url = None
        active_slide = lot_li.find(
            "li", class_=re.compile(r"slick-current")
        )
        if active_slide:
            img = active_slide.find("img")
            if img:
                src = img.get("src", "")
                if src and "resize.auctions.savills" in src and not src.endswith(".svg"):
                    image_url = src
        if not image_url:
            for img in lot_li.find_all("img"):
                src = img.get("src", "")
                if src and "resize.auctions.savills" in src and not src.endswith(".svg"):
                    image_url = src
                    break

        # ── Price ─────────────────────────────────────────────────────────────
        price_tag = lot_content.select_one(".guide-price .value, .price-container .value")
        price = parse_guide_price(price_tag.get_text() if price_tag else lot_content.get_text(" ", strip=True))

        # ── Lot number ────────────────────────────────────────────────────────
        lot_num_p = lot_content.find("p", class_="lot-number")
        lot_num = None
        if lot_num_p:
            m = re.search(r"(\d+)", lot_num_p.get_text())
            lot_num = int(m.group(1)) if m else None

        # ── Bullet-point description from .lot-details ────────────────────────
        description = None
        lot_details = lot_content.find("div", class_="lot-details")
        if lot_details:
            skip_terms = ("click here", "section", "offered", "lots 9")
            parts = []
            for li in lot_details.find_all("li"):
                t = li.get_text(" ", strip=True)
                if t and len(t) > 2 and not any(s in t.lower() for s in skip_terms):
                    parts.append(t)
            if parts:
                description = " | ".join(parts)

        # ── Bedrooms & property type ──────────────────────────────────────────
        _WORD_NUMS = {
            "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
            "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
        }
        search_text = description or lot_content.get_text(" ", strip=True)
        bedrooms = None
        bed_m = re.search(r"(\d+)\s*[Bb]ed", search_text)
        if bed_m:
            bedrooms = int(bed_m.group(1))
        else:
            word_m = re.search(
                r"(one|two|three|four|five|six|seven|eight|nine|ten)[- ]+bed",
                search_text, re.IGNORECASE,
            )
            if word_m:
                bedrooms = _WORD_NUMS.get(word_m.group(1).lower())

        prop_type = normalize_property_type(search_text)

        return {
            "lot_number":    lot_num,
            "price":         price,
            "address":       address,
            "property_type": prop_type,
            "bedrooms":      bedrooms,
            "image_url":     image_url,
            "description":   description,
        }
    except Exception:
        logger.debug("[%s] card parse error for lot_id=%s", SOURCE, lot_id, exc_info=True)
        return None


# ── Detail page parser ────────────────────────────────────────────────────────

_DATE_RE = re.compile(
    r"(\d{1,2})\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})",
    re.IGNORECASE,
)

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}


def _parse_date(text: str) -> Optional[datetime]:
    m = _DATE_RE.search(text)
    if not m:
        return None
    try:
        day = int(m.group(1))
        month = _MONTHS[m.group(2).lower()]
        year = int(m.group(3))
        return datetime(year, month, day)
    except Exception:
        return None


def _parse_detail_page(html: str, auction_date_hint: Optional[datetime]) -> Dict[str, Any]:
    """Extract all available fields from a Savills lot detail page."""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    # Address — typically the page <h1>
    address = None
    h1 = soup.find("h1")
    if h1:
        address = clean_text(h1.get_text(" ", strip=True))

    # Guide price
    price = None
    gp_m = re.search(r"[Gg]uide\s+[Pp]rice[:\s]*(.*?)(?:\n|<|$)", html)
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
        r"([\w\s-]+ bedroom [\w\s-]+)",
        r"(flat|apartment|house|bungalow|land|commercial|office|studio)",
    ):
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            prop_type = normalize_property_type(m.group(1))
            break

    # Description — longest <p> that isn't boilerplate/copyright/legal
    _BOILERPLATE = (
        "copyright", "all rights reserved", "savills plc", "savills.co.uk",
        "savills is a trading", "privacy policy", "cookie", "registered in england",
        "guide price", "reserve price", "buyer's fee", "special conditions",
    )
    description = None
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

    # Image — prefer resize.auctions.savills.co.uk (actual property photos); skip SVGs/logos
    image_url = None
    _SKIP_IMG = ("logo", "icon", "arrow", ".svg", "square", "home.svg", "images.svg")
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if not src:
            continue
        src_low = src.lower()
        if any(s in src_low for s in _SKIP_IMG):
            continue
        if "resize.auctions.savills" in src_low:
            image_url = src
            break
    if not image_url:
        for img in soup.find_all("img"):
            src = img.get("src", "")
            if not src or any(s in src.lower() for s in _SKIP_IMG):
                continue
            if src.startswith("http") and ("savills" in src.lower() or "cdn" in src.lower()):
                image_url = src
                break

    # Auction date
    auction_date = None
    for label in ("auction", "sale date", "lot date"):
        idx = text.lower().find(label)
        if idx != -1:
            snippet = text[idx: idx + 100]
            auction_date = _parse_date(snippet)
            if auction_date:
                break
    if auction_date is None and auction_date_hint:
        auction_date = auction_date_hint

    # Lat / lng from embedded map or script data
    lat = lng = None
    lat_m = re.search(r'"lat(?:itude)?"\s*:\s*([-\d.]+)', html)
    lng_m = re.search(r'"l(?:ng|on(?:gitude)?)"\s*:\s*([-\d.]+)', html)
    if lat_m and lng_m:
        try:
            lat = float(lat_m.group(1))
            lng = float(lng_m.group(1))
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
        "auction_date":  auction_date,
        "lat":           lat,
        "lng":           lng,
    }


# ── Main scraper class ────────────────────────────────────────────────────────

class SavillsAuctionsScraper(BaseScraper):
    source = SOURCE

    def __init__(self) -> None:
        self._tracker = TierTracker()

    def reset_tier_stats(self) -> None:
        self._tracker.reset()

    def get_tier_stats(self) -> Dict:
        return self._tracker.get()

    def fetch_listings(
        self,
        city: str,  # noqa: ARG002 — Savills scrapes all lots regardless of city
        fetch_details: bool = True,
        on_listing: Optional[OnListingFn] = None,
    ) -> List[Dict[str, Any]]:
        """
        Scrape all upcoming Savills auction catalogs.

        The `city` parameter is ignored — all upcoming lots are returned.
        Pass on_listing callback to enable early stopping / duplicate skipping.
        """
        results: List[Dict[str, Any]] = []
        stop_all = False

        # Savills catalog pages are Cloudflare-protected — browser tier required.
        uc_driver = None
        try:
            uc_driver = make_uc_driver(_UC_HEADLESS)
            logger.info("[%s] Browser launched", SOURCE)
        except Exception:
            logger.warning("[%s] Browser unavailable — curl/ScraperAPI only", SOURCE, exc_info=True)

        try:
            catalog_urls = _get_catalog_urls(uc_driver, self._tracker)
            if not catalog_urls:
                logger.warning("[%s] No catalogs found", SOURCE)
                return results

            for catalog_url in catalog_urls:
                if stop_all:
                    break

                # Infer auction date from catalog slug, e.g. "9-june-2026-224"
                catalog_slug = catalog_url.rstrip("/").split("/")[-1]
                auction_date_hint = _parse_date(catalog_slug.replace("-", " "))
                logger.info("[%s] Catalog: %s  auction_date=%s", SOURCE, catalog_url, auction_date_hint)

                for page in range(1, MAX_PAGES + 1):
                    if stop_all:
                        break

                    page_url = (
                        catalog_url
                        if page == 1
                        else f"{catalog_url}/page-{page}"
                    )
                    logger.info("[%s] Page %d -> %s", SOURCE, page, page_url)

                    html = _get_html(page_url, self._tracker, "search", uc_driver, is_catalog=True)
                    if not html:
                        logger.info("[%s] No HTML on page %d — ending catalog", SOURCE, page)
                        break

                    lot_links = _extract_lot_links_from_page(html)
                    if not lot_links:
                        logger.info("[%s] No lot links on page %d — ending catalog", SOURCE, page)
                        break

                    logger.info("[%s] Page %d: %d lot links", SOURCE, page, len(lot_links))

                    for lot_url, lot_id in lot_links:
                        # Quick card data from catalog page
                        card = _parse_card_from_page(html, lot_id)
                        if card is None:
                            continue

                        partial = {
                            "source":        SOURCE,
                            "listing_url":   lot_url,
                            "city":          city_from_address(card.get("address")),
                            "address":       card.get("address"),
                            "price":         card.get("price"),
                            "bedrooms":      card.get("bedrooms"),
                            "bathrooms":     None,
                            "size_m2":       None,
                            "property_type": card.get("property_type"),
                            "description":   card.get("description"),
                            "agent_name":    "Savills Auctions",
                            "agent_phone":   None,
                            "image_url":     card.get("image_url"),
                            "lat":           None,
                            "lng":           None,
                            "auction_date":  auction_date_hint,
                            "lot_number":    card.get("lot_number") or lot_id,
                        }

                        # on_listing callback (dedup / stop)
                        if on_listing is not None:
                            action = on_listing(partial)
                            if action == "stop":
                                logger.info("[%s] on_listing=stop — ending catalog", SOURCE)
                                stop_all = True
                                break
                            if action == "skip":
                                continue

                        # Detail page fetch — enrich but don't downgrade address/description
                        if fetch_details:
                            time.sleep(random.uniform(0.5, 1.2))
                            detail_html = _get_html(lot_url, self._tracker, "detail", uc_driver)
                            if detail_html:
                                if _is_sold_detail_html(detail_html):
                                    logger.debug("[%s] Skipping sold/withdrawn lot (detail page): %s", SOURCE, lot_url)
                                    continue
                                detail = _parse_detail_page(detail_html, auction_date_hint)
                                for k, v in detail.items():
                                    if v is None:
                                        continue
                                    # Don't overwrite address/description with shorter/worse value
                                    if k == "address" and partial.get("address") and len(partial["address"]) >= len(v):
                                        continue
                                    if k == "description" and partial.get("description") and len(partial["description"]) >= len(v):
                                        continue
                                    partial[k] = v
                                if partial.get("city") is None and partial.get("address"):
                                    partial["city"] = city_from_address(partial["address"])

                        results.append(partial)

                    if stop_all:
                        break

                    has_next = _has_more_pages(html, page)
                    if not has_next:
                        logger.info("[%s] No next page after page %d", SOURCE, page)
                        break

                    time.sleep(random.uniform(1.0, 2.5))

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
