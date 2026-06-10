"""
Clive Emson Auctioneers scraper (cliveemson.co.uk).

Flow:
  1. Fetch /properties/ to get the current auction ID and all lot links.
     All ~150 lots for the current auction are listed on one page.
     URL pattern: /properties/<auction_id>/<lot_number>/
  2. Also check /properties/commerical-property-auctions/ for commercial lots.
  3. Also check /properties/unsold-lots-still-available/ for unsold lots.
  4. Optionally visit each detail page for full data.

Pass cities=["all"] when calling run_scrape; city is ignored.

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
    parse_guide_price, normalize_property_type, city_from_address, clean_text,
    parse_size_m2, geocode_address,
)

logger = logging.getLogger(__name__)

BASE   = "https://www.cliveemson.co.uk"
SOURCE = "cliveemson"

# Main listing pages to check
LISTING_PAGES = [
    "/properties/",
    "/properties/commerical-property-auctions/",
    "/properties/unsold-lots-still-available/",
]

_SCRAPERAPI_RESIDENTIAL  = os.getenv("SCRAPERAPI_RESIDENTIAL",  "0") == "1"

OnListingFn = Callable[[Dict[str, Any]], Optional[str]]


# ── HTTP helpers ─────────────────────────────────────────────────────────────

def _get_html(
    url: str,
    tracker: TierTracker,
    context: str,
    uc_driver=None,
) -> Optional[str]:
    api_key = os.getenv("SCRAPERAPI_KEY", "")

    html = direct_curl(url, SOURCE)
    tracker.track(context, "curl", bool(html))
    if html:
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


# ── Discovery: extract lot links from a listing page ─────────────────────────

_LOT_HREF_RE = re.compile(r'href="(/properties/(\d+)/(\d+)/)"')


def _extract_lot_links(html: str) -> List[tuple]:
    """
    Return list of (full_url, auction_id, lot_number) tuples.
    URL pattern: /properties/<auction_id>/<lot_number>/
    """
    found = []
    seen = set()
    for m in _LOT_HREF_RE.finditer(html):
        href = m.group(1)
        auction_id = m.group(2)
        lot_number = int(m.group(3))
        full_url = f"{BASE}{href}"
        if full_url not in seen:
            seen.add(full_url)
            found.append((full_url, auction_id, lot_number))
    return found


# ── Auction date extraction from listing page ─────────────────────────────────

_DATE_RE = re.compile(
    r"(\d{1,2})(?:st|nd|rd|th)?\s+"
    r"(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(\d{4})",
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
        return datetime(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))
    except Exception:
        return None


def _get_auction_date_from_page(html: str) -> Optional[datetime]:
    """The listing page typically shows the auction date as a header."""
    # Look for patterns like "Wednesday 17th June 2026, 11:00 AM"
    soup = BeautifulSoup(html, "html.parser")
    page_text = soup.get_text(" ", strip=True)
    # Try extracting near "auction" keyword
    for keyword in ("auction", "sale date"):
        idx = page_text.lower().find(keyword)
        if idx != -1:
            snippet = page_text[max(0, idx - 20): idx + 80]
            d = _parse_date(snippet)
            if d:
                return d
    # Fallback: find any date in the page text
    return _parse_date(page_text)


_SOLD_STATUSES = re.compile(
    r'\b(?:SOLD|Sold\s+Prior|Withdrawn\s+Prior|Withdrawn|Under\s+Offer|Exchanged|Postponed|Nil\s+Reserve)\b',
    re.IGNORECASE,
)


def _is_sold_detail_html(html: str) -> bool:
    """Return True if the detail page indicates the lot is unavailable."""
    soup = BeautifulSoup(html, "html.parser")
    if soup.find(class_=re.compile(r'\b(?:sold|withdrawn|postponed)\b', re.IGNORECASE)):
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
        re.search(r'\bPostponed\b', text, re.IGNORECASE) or
        re.search(r'\bNil\s+Reserve\b', text, re.IGNORECASE) or
        re.search(r'\bUnder\s+Offer\b', text, re.IGNORECASE)
    )


# ── Card data from listing page ───────────────────────────────────────────────

def _parse_card_from_listing_page(html: str, lot_url: str) -> Optional[Dict[str, Any]]:
    """Extract basic property data from the card in the main listing page.

    Clive Emson card structure:
      <div class="lot activeLot"
           data-auc="266"
           data-cathead="WELL PRESENTED TWO-BEDROOM FLAT..."
           data-mainpic="34404%20-%20Queens%20Road%20-%20rear.jpg"
           data-lonlat="51.358,1.4392"
           data-price="£150,000" ...>
        <a href="/properties/266/1/">
          <h3 class="LotHeading">WELL PRESENTED TWO-BEDROOM FLAT...</h3>
          <p class="LotLocation">Thanet Area - Kent Area</p>
        </a>
      </div>
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
        path = lot_url.replace(BASE, "")

        anchor = soup.find("a", href=path)
        if not anchor:
            for a in soup.find_all("a", href=True):
                if path.rstrip("/") in a["href"]:
                    anchor = a
                    break
        if not anchor:
            return None

        # Walk up to the .lot div container
        card = anchor
        for _ in range(6):
            if card.parent:
                card = card.parent
            if card.get("data-auc") or "lot" in " ".join(card.get("class", [])).lower():
                break

        # Skip sold / withdrawn / postponed / nil-reserve lots
        card_classes = " ".join(card.get("class", [])).lower()
        if re.search(r'\b(?:sold|withdrawn|postponed)\b', card_classes):
            return None
        if card.find(class_=re.compile(r'\b(?:sold|withdrawn|postponed)\b', re.IGNORECASE)):
            return None
        card_text_check = card.get_text(" ", strip=True)
        for el in card.find_all(["span", "div", "p", "strong", "em", "small"]):
            t = el.get_text(" ", strip=True)
            if len(t) < 50 and _SOLD_STATUSES.search(t):
                return None
        if _SOLD_STATUSES.search(card_text_check[:200]):
            return None

        # ── Description: heading from <h3 class="LotHeading"> or data-cathead ──
        description = None
        lot_heading = card.find("h3", class_="LotHeading") or card.find("h3")
        if lot_heading:
            description = clean_text(lot_heading.get_text(" ", strip=True))
        if not description:
            description = clean_text(card.get("data-cathead", "")) or None
        if description:
            description = description.title()  # "WELL PRESENTED..." → "Well Presented..."

        # ── Guide price: prefer data-price attr ──────────────────────────────
        price = parse_guide_price(card.get("data-price", "") or card.get_text(" ", strip=True))

        # ── Location text ──────────────────────────────────────────────────────
        address = None
        loc_p = card.find("p", class_="LotLocation")
        if loc_p:
            address = clean_text(loc_p.get_text(" ", strip=True))

        # ── Bedrooms ──────────────────────────────────────────────────────────
        bedrooms = None
        search_text = (description or "") + " " + card.get_text(" ", strip=True)
        bed_m = re.search(r"(\d+)\s*[Bb]ed(?:room)?", search_text)
        if bed_m:
            bedrooms = int(bed_m.group(1))
        else:
            word_m = re.search(
                r"(one|two|three|four|five|six|seven|eight|nine|ten)[- ]+bed",
                search_text, re.IGNORECASE,
            )
            if word_m:
                bedrooms = {"one":1,"two":2,"three":3,"four":4,"five":5,
                            "six":6,"seven":7,"eight":8,"nine":9,"ten":10}.get(word_m.group(1).lower())

        # ── Image: construct full URL from data-auc + data-mainpic ────────────
        image_url = None
        auc_id  = card.get("data-auc", "")
        mainpic = card.get("data-mainpic", "")
        if auc_id and mainpic:
            from urllib.parse import unquote
            pic_name = unquote(mainpic)
            image_url = f"{BASE}/Auc{auc_id}/pics/{pic_name}"
        if not image_url:
            img = card.find("img")
            if img and img.get("src"):
                src = img["src"]
                image_url = src if src.startswith("http") else f"{BASE}{src}"

        return {
            "property_type": normalize_property_type(description or ""),
            "price":         price,
            "address":       address,
            "bedrooms":      bedrooms,
            "image_url":     image_url,
            "description":   description,
        }
    except Exception:
        logger.debug("[%s] card parse error for %s", SOURCE, lot_url, exc_info=True)
        return None


# ── Detail page parser ────────────────────────────────────────────────────────

_WORD_NUMS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}

_UK_POSTCODE_RE = re.compile(r"[A-Z]{1,2}\d[\d\w]?\s*\d[A-Z]{2}", re.IGNORECASE)


def _parse_detail_page(html: str, auction_date_hint: Optional[datetime]) -> Dict[str, Any]:
    """Extract all available fields from a Clive Emson lot detail page."""
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text(" ", strip=True)

    # Address — Clive Emson puts it *after* "Auction Date: <date>" and before "Auction Ends:"
    # Pattern: "... Auction Date: 17th June 2026 <Full Address>, ..., CT10 1NU Auction Ends: ..."
    address = None
    adate_end = re.search(r"Auction\s+Date[:\s]+[\d\w\s,]+?\d{4}\s+", text, re.IGNORECASE)
    if adate_end:
        remainder = text[adate_end.end():]
        ae_m = re.search(r"Auction\s+Ends", remainder, re.IGNORECASE)
        candidate = remainder[:ae_m.start()].strip() if ae_m else remainder[:200].strip()
        if _UK_POSTCODE_RE.search(candidate):
            address = clean_text(candidate)
    # Fallback: extract text around UK postcode
    if not address:
        pc_m = _UK_POSTCODE_RE.search(text)
        if pc_m:
            snippet = text[max(0, pc_m.start() - 150): pc_m.end()]
            # Take the part from the last comma before 100 chars before postcode
            parts = snippet.rsplit(",", 3)
            address = clean_text(",".join(parts).strip() if len(parts) >= 2 else snippet)

    # Guide price
    price = None
    gp_m = re.search(r"GUIDE\s+PRICE[:\s]*([£\d,\.\+\-\sMm]+)", text, re.IGNORECASE)
    if gp_m:
        price = parse_guide_price(gp_m.group(1))
    if price is None:
        price = parse_guide_price(text)

    # Bedrooms / bathrooms (digit form AND word form: "Two-Bedroom")
    bedrooms = bathrooms = None
    bed_m = re.search(r"(\d+)\s*[Bb]ed(?:room)?", text)
    if bed_m:
        bedrooms = int(bed_m.group(1))
    else:
        word_bed = re.search(
            r"(one|two|three|four|five|six|seven|eight|nine|ten)[- ]+[Bb]ed(?:room)?",
            text, re.IGNORECASE,
        )
        if word_bed:
            bedrooms = _WORD_NUMS.get(word_bed.group(1).lower())

    bath_m = re.search(r"(\d+)\s*[Bb]ath(?:room)?", text)
    if bath_m:
        bathrooms = int(bath_m.group(1))

    # Floor area
    size_m2 = parse_size_m2(text)

    # Property type
    prop_type = None
    type_labels = (
        r"([\w\s-]+ bedroom [\w-]+)",
        r"(flat|apartment|house|bungalow|land|commercial|office|vacant residential|hmo)",
    )
    for pattern in type_labels:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            prop_type = normalize_property_type(m.group(1))
            break

    # Description — prefer lotPropertyDetails div (actual write-up) + key features;
    # fall back to longest non-boilerplate paragraph.
    _BOILERPLATE = (
        "all buyers are advised", "guides are provided", "administration fee",
        "all lots are sold", "common auction conditions", "reserve will be set",
        "clive emson", "registered office", "registered company",
        "map and satellite", "viewing times", "sign in to view",
        "service providers", "transportation routes",
    )
    description = None
    prop_details = soup.find("div", class_="lotPropertyDetails")
    features_div = soup.find("div", class_="lotFeatures")
    parts = []
    if features_div:
        feat_text = clean_text(features_div.get_text(" | ", strip=True))
        # Strip the "Key Features" label that comes first
        feat_text = re.sub(r"^Key Features\s*\|?\s*", "", feat_text or "", flags=re.IGNORECASE)
        if feat_text:
            parts.append(feat_text.strip(" |"))
    if prop_details:
        for p in prop_details.find_all("p"):
            t = clean_text(p.get_text(" ", strip=True))
            if t and len(t) > 20 and not any(b in t.lower() for b in _BOILERPLATE):
                parts.append(t)
    if parts:
        description = " | ".join(parts)
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

    # Address — use <h2> which Clive Emson puts the full postcode address in
    if not address:
        h2 = soup.find("h2")
        if h2:
            candidate = clean_text(h2.get_text(" ", strip=True))
            if candidate and _UK_POSTCODE_RE.search(candidate):
                address = candidate

    # Image — prefer Auc<id>/pics/ URLs; fall back to any cliveemson/upload URL
    image_url = None
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if not src:
            continue
        src_low = src.lower()
        if "auc" in src_low and "pics" in src_low:
            image_url = src if src.startswith("http") else f"{BASE}{src}"
            break
        if "cliveemson" in src_low or "upload" in src_low:
            image_url = src if src.startswith("http") else f"{BASE}{src}"
            break

    # Agent name / phone
    agent_name = "Clive Emson Auctioneers"
    agent_phone = None
    phone_m = re.search(r"(\d[\d\s\-]{8,14}\d)", text)
    if phone_m:
        agent_phone = phone_m.group(1).strip()

    # Auction date
    auction_date = None
    for kw in ("auction date", "sale date", "auction"):
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
    if not lat_m:
        lat_m = re.search(r"lat(?:itude)?[\"'\s:=]+([-\d.]+)", html, re.IGNORECASE)
    if not lng_m:
        lng_m = re.search(r"l(?:ng|on(?:gitude)?)[\"'\s:=]+([-\d.]+)", html, re.IGNORECASE)
    if lat_m and lng_m:
        try:
            lat = float(lat_m.group(1))
            lng = float(lng_m.group(1))
            # Sanity-check UK coordinates
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

class CliveEmsonScraper(BaseScraper):
    source = SOURCE

    def __init__(self) -> None:
        self._tracker = TierTracker()

    def reset_tier_stats(self) -> None:
        self._tracker.reset()

    def get_tier_stats(self) -> Dict:
        return self._tracker.get()

    def fetch_listings(
        self,
        city: str,  # noqa: ARG002 — Clive Emson scrapes all UK lots regardless of city
        fetch_details: bool = True,
        on_listing: Optional[OnListingFn] = None,
    ) -> List[Dict[str, Any]]:
        """
        Scrape all current Clive Emson auction lots.

        The `city` parameter is ignored — all lots across the UK are returned.
        """
        results: List[Dict[str, Any]] = []
        uc_driver = None
        stop_all = False

        try:
            # Collect lot links from each listing page
            all_lots: List[tuple] = []  # (full_url, auction_id, lot_number, auction_date, page_html)
            page_html_cache: Dict[str, str] = {}

            for listing_path in LISTING_PAGES:
                if stop_all:
                    break
                url = f"{BASE}{listing_path}"
                logger.info("[%s] Fetching listing page: %s", SOURCE, url)

                html = _get_html(url, self._tracker, "search", uc_driver)
                if not html:
                    logger.warning("[%s] Could not fetch %s", SOURCE, url)
                    continue

                lot_links = _extract_lot_links(html)
                if not lot_links:
                    logger.info("[%s] No lot links on %s", SOURCE, url)
                    continue

                auction_date = _get_auction_date_from_page(html)
                logger.info(
                    "[%s] %s: %d lots, auction_date=%s",
                    SOURCE, listing_path, len(lot_links), auction_date,
                )

                for lot_url, auction_id, lot_number in lot_links:
                    all_lots.append((lot_url, auction_id, lot_number, auction_date, html))
                    page_html_cache[lot_url] = html

            # Deduplicate across listing pages
            seen_urls: set = set()
            unique_lots = []
            for lot_data in all_lots:
                if lot_data[0] not in seen_urls:
                    seen_urls.add(lot_data[0])
                    unique_lots.append(lot_data)

            logger.info("[%s] Total unique lots: %d", SOURCE, len(unique_lots))

            for lot_url, auction_id, lot_number, auction_date, page_html in unique_lots:
                if stop_all:
                    break

                card = _parse_card_from_listing_page(page_html, lot_url)
                if card is None:
                    continue

                partial = {
                    "source":        SOURCE,
                    "listing_url":   lot_url,
                    "city":          city_from_address(card.get("address") if card else None),
                    "address":       card.get("address") if card else None,
                    "price":         card.get("price") if card else None,
                    "bedrooms":      card.get("bedrooms") if card else None,
                    "bathrooms":     None,
                    "size_m2":       None,
                    "property_type": card.get("property_type") if card else None,
                    "description":   card.get("description") if card else None,
                    "agent_name":    "Clive Emson Auctioneers",
                    "agent_phone":   None,
                    "image_url":     card.get("image_url") if card else None,
                    "lat":           None,
                    "lng":           None,
                    "auction_date":  auction_date,
                    "lot_number":    str(lot_number),
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
                    time.sleep(random.uniform(0.4, 1.0))
                    detail_html = _get_html(lot_url, self._tracker, "detail", uc_driver)
                    if detail_html:
                        if _is_sold_detail_html(detail_html):
                            logger.debug("[%s] Skipping unavailable lot (detail page): %s", SOURCE, lot_url)
                            continue
                        detail = _parse_detail_page(detail_html, auction_date)
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
