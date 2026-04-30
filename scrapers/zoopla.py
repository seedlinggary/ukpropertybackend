"""
Zoopla property-for-sale scraper — two-tier Cloudflare bypass.

Tier 1 — curl_cffi (fast, no browser):
  Makes HTTP requests that impersonate Chrome's TLS/HTTP2 fingerprint.
  The JSON-LD schema block (<script id="lsrp-schema">) is server-rendered
  inside the raw HTML, so no JavaScript execution is needed.

Tier 2 — undetected-chromedriver (full browser fallback):
  A patched ChromeDriver that strips automation signals.
  Clicks the real pagination "Next" button rather than just changing the URL.

Sort order:  newest_listings (most recent first) — set via URL param.

Stop conditions are enforced by the caller via the on_listing callback:
  return "skip" → exclude this listing from results (duplicate)
  return "stop" → stop the current city entirely
  return None   → include the listing normally

Floor sizes arrive in square feet (unitCode="FTK"); converted to m².
"""

import json
import logging
import os
import random
import re
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# On a headless server (Railway, Render, etc.) set UC_HEADLESS=1 (default).
# Set UC_HEADLESS=0 to open a visible browser window (local testing).
_UC_HEADLESS = os.getenv("UC_HEADLESS", "1") == "1"

BASE_URL     = "https://www.zoopla.co.uk/for-sale/property/{city}/"
SORT_PARAM   = "newest_listings"
MAX_PAGES    = 40           # hard ceiling; stop conditions usually fire first
SQFT_TO_SQM  = 0.092903

_CF_SIGNALS = (
    "just a moment",
    "security verification",
    "please wait",
    "cf-browser-verification",
)

_CURL_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "accept-language": "en-GB,en;q=0.9",
    "cache-control": "no-cache",
    "pragma": "no-cache",
    "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
}

OnListingFn = Callable[[Dict[str, Any]], Optional[str]]  # returns "stop"|"skip"|None


def _search_url(city_slug: str, page: int) -> str:
    return f"{BASE_URL.format(city=city_slug)}?sort={SORT_PARAM}&pn={page}"


# ─────────────────────────────────────────────────────────────
# Shared HTTP helper
# ─────────────────────────────────────────────────────────────

def _curl_get(url: str) -> Optional[str]:
    try:
        from curl_cffi import requests as cffi_req
        resp = cffi_req.get(
            url,
            headers=_CURL_HEADERS,
            impersonate="chrome124",
            timeout=30,
            allow_redirects=True,
        )
        if resp.status_code != 200:
            logger.warning("[zoopla/curl] HTTP %d for %s", resp.status_code, url)
            return None
        html = resp.text
        if any(s in html.lower() for s in _CF_SIGNALS):
            logger.info("[zoopla/curl] Cloudflare challenge — will use browser")
            return None
        return html
    except Exception:
        logger.debug("[zoopla/curl] request failed", exc_info=True)
        return None


# ─────────────────────────────────────────────────────────────
# Tier 1: JSON-LD extraction from raw HTML
# ─────────────────────────────────────────────────────────────

def _schema_items_from_html(html: str) -> List[dict]:
    """
    Extract listing items from the lsrp-schema __next_s push embedded in HTML.

    Zoopla writes:
      <script>(self.__next_s||[]).push([0, {
        "type": "application/ld+json",
        "children": "{ escaped Schema.org JSON }",
        "id": "lsrp-schema"
      }])</script>
    """
    try:
        marker_idx = html.find('"id":"lsrp-schema"')
        if marker_idx == -1:
            return []
        children_key = '"children":"'
        key_idx = html.rfind(children_key, max(0, marker_idx - 200_000), marker_idx)
        if key_idx == -1:
            return []
        i = key_idx + len(children_key)
        chars: List[str] = []
        while i < len(html):
            ch = html[i]
            if ch == "\\":
                chars.append(ch); chars.append(html[i + 1]); i += 2
            elif ch == '"':
                break
            else:
                chars.append(ch); i += 1
        children_str = json.loads('"' + "".join(chars) + '"')
        data = json.loads(children_str)
        for node in data.get("@graph", []):
            if node.get("@type") == "SearchResultsPage":
                elements = node.get("mainEntity", {}).get("itemListElement", [])
                return [el["item"] for el in elements if el.get("item")]
    except Exception:
        logger.debug("[zoopla] schema extraction failed", exc_info=True)
    return []


def _html_has_next_page(html: str, page: int) -> bool:
    return f"?pn={page + 1}" in html or 'rel="next"' in html


# ─────────────────────────────────────────────────────────────
# Tier 2: undetected-chromedriver browser
# ─────────────────────────────────────────────────────────────

def _make_uc_driver(headless: bool = True):
    import undetected_chromedriver as uc
    opts = uc.ChromeOptions()
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    # Only set binary_location when the env var points to a file that exists.
    # Setting it to a non-existent path triggers a TypeError inside UC driver.
    chrome_path = os.getenv("CHROME_EXECUTABLE_PATH", "")
    if chrome_path and os.path.isfile(chrome_path):
        opts.binary_location = chrome_path
    return uc.Chrome(options=opts, headless=headless, use_subprocess=True)


def _wait_for_cloudflare(driver, timeout: int = 30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not any(s in driver.title.lower() for s in _CF_SIGNALS):
            return True
        logger.info("[zoopla/browser] Cloudflare challenge active — waiting…")
        time.sleep(2)
    logger.warning("[zoopla/browser] CF challenge not resolved in %ds", timeout)
    return False


def _dom_schema_items(driver) -> List[dict]:
    try:
        raw = driver.execute_script(
            "var el=document.getElementById('lsrp-schema');"
            "return el ? el.textContent : null;"
        )
        if not raw:
            return []
        for node in json.loads(raw).get("@graph", []):
            if node.get("@type") == "SearchResultsPage":
                elements = node.get("mainEntity", {}).get("itemListElement", [])
                return [el["item"] for el in elements if el.get("item")]
    except Exception:
        logger.debug("[zoopla/browser] DOM extraction failed", exc_info=True)
    return []


def _browser_click_next(driver) -> bool:
    """Click the pagination Next button. Returns True if clicked successfully."""
    for selector in (
        '[data-testid="pagination-next"]',
        'a[rel="next"]',
        'a[aria-label="Next page"]',
    ):
        try:
            btn = driver.find_element("css selector", selector)
            driver.execute_script("arguments[0].scrollIntoView(true);", btn)
            time.sleep(0.3)
            btn.click()
            logger.info("[zoopla/browser] Clicked next-page button")
            return True
        except Exception:
            pass
    return False


# ─────────────────────────────────────────────────────────────
# Detail page: full "About this property" description
# ─────────────────────────────────────────────────────────────

def _full_description_from_html(html: str) -> Optional[str]:
    best: Optional[str] = None

    # Strategy 1: any ld+json script tag
    for m in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL,
    ):
        try:
            desc = _deepest_description(json.loads(m.group(1)))
            if desc and len(desc) > len(best or ""):
                best = desc
        except Exception:
            pass

    if best and len(best) >= 200:
        return best.strip()

    # Strategy 2: lsrp-schema __next_s push
    try:
        marker_idx = html.find('"id":"lsrp-schema"')
        if marker_idx != -1:
            children_key = '"children":"'
            key_idx = html.rfind(children_key, max(0, marker_idx - 200_000), marker_idx)
            if key_idx != -1:
                i = key_idx + len(children_key)
                chars: List[str] = []
                while i < len(html):
                    ch = html[i]
                    if ch == "\\":
                        chars.append(ch); chars.append(html[i + 1]); i += 2
                    elif ch == '"':
                        break
                    else:
                        chars.append(ch); i += 1
                children_str = json.loads('"' + "".join(chars) + '"')
                desc = _deepest_description(json.loads(children_str))
                if desc and len(desc) > len(best or ""):
                    best = desc
    except Exception:
        pass

    if best and len(best) >= 200:
        return best.strip()

    # Strategy 3: "About this property" heading in raw HTML
    for pattern in (
        r'[Aa]bout this property\s*</[^>]+>\s*<[^>]+>(.*?)(?=<h[1-6]|</section|</article)',
        r'data-testid=["\']listing-description["\'][^>]*>(.*?)</(?:div|p|section)',
        r'data-testid=["\']truncated-text["\'][^>]*>(.*?)</(?:div|p|section)',
    ):
        m = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
        if m:
            text = re.sub(r'<[^>]+>', ' ', m.group(1))
            text = re.sub(r'\s+', ' ', text).strip()
            if len(text) >= 100:
                return text

    return best.strip() if best else None


def _deepest_description(obj: Any, _d: int = 0) -> Optional[str]:
    if _d > 8:
        return None
    best: Optional[str] = None
    if isinstance(obj, dict):
        d = obj.get("description")
        if isinstance(d, str) and len(d) > len(best or ""):
            best = d
        for v in obj.values():
            r = _deepest_description(v, _d + 1)
            if r and len(r) > len(best or ""):
                best = r
    elif isinstance(obj, list):
        for item in obj:
            r = _deepest_description(item, _d + 1)
            if r and len(r) > len(best or ""):
                best = r
    return best


def _fetch_full_description(listing_url: str) -> Optional[str]:
    html = _curl_get(listing_url)
    if not html:
        return None
    return _full_description_from_html(html)


# ─────────────────────────────────────────────────────────────
# Normalisation
# ─────────────────────────────────────────────────────────────

def _to_int(val: Any) -> Optional[int]:
    try:
        return int(float(re.sub(r"[^\d.]", "", str(val))))
    except (TypeError, ValueError):
        return None


def _sqft_to_m2(val: Any) -> Optional[float]:
    try:
        return round(float(val) * SQFT_TO_SQM, 1)
    except (TypeError, ValueError):
        return None


def _prop_type(schema_type: str, name: str) -> Optional[str]:
    st, n = schema_type.lower(), name.lower()
    if "apartment" in st or "flat" in n:
        return "flat"
    if "house" in st or "house" in n:
        return "house"
    return schema_type.lower() or None


def _normalize(item: dict, city: str) -> Optional[Dict[str, Any]]:
    try:
        url = item.get("url", "")
        if not url:
            return None
        offers  = item.get("offers") or {}
        related = item.get("isRelatedTo") or {}
        geo     = related.get("geo") or {}
        floor   = related.get("floorSize") or {}
        size_m2: Optional[float] = None
        if floor.get("value"):
            unit = (floor.get("unitCode") or "").upper()
            if unit == "FTK":
                size_m2 = _sqft_to_m2(floor["value"])
            elif unit in ("MTK", "M2", "SQM"):
                size_m2 = round(float(floor["value"]), 1)
        return {
            "source":        "zoopla",
            "listing_url":   url,
            "city":          city,
            "address":       related.get("address") or item.get("name"),
            "price":         _to_int(offers.get("price")),
            "bedrooms":      related.get("numberOfBedrooms"),
            "bathrooms":     related.get("numberOfBathroomsTotal"),
            "size_m2":       size_m2,
            "property_type": _prop_type(related.get("@type", ""), item.get("name", "")),
            "description":   item.get("description"),
            "agent_name":    None,
            "agent_phone":   None,
            "image_url":     item.get("image"),
            "lat":           float(geo["latitude"])  if geo.get("latitude")  else None,
            "lng":           float(geo["longitude"]) if geo.get("longitude") else None,
        }
    except Exception:
        logger.warning("[zoopla] normalise failed", exc_info=True)
        return None


# ─────────────────────────────────────────────────────────────
# Scraper
# ─────────────────────────────────────────────────────────────

from scrapers.base import BaseScraper


class ZooplaScraper(BaseScraper):
    source = "zoopla"

    def fetch_listings(
        self,
        city: str,
        fetch_details: bool = True,
        on_listing: Optional[OnListingFn] = None,
    ) -> List[Dict[str, Any]]:
        """
        Scrape Zoopla for the given city, sorted by most recent.

        Args:
            city:          City name, e.g. "london".
            fetch_details: Visit each listing URL to get the full description.
            on_listing:    Optional callback called for every normalised listing
                           BEFORE detail fetching.  Return value controls flow:
                             "stop" — stop scraping this city immediately
                             "skip" — exclude listing from results (duplicate)
                             None   — include listing normally
        """
        city_slug  = city.lower().replace(" ", "-")
        results: List[Dict[str, Any]] = []
        uc_driver  = None
        stop_city  = False

        try:
            page = 1
            while page <= MAX_PAGES and not stop_city:
                url = _search_url(city_slug, page)
                logger.info("[zoopla] Page %d → %s", page, url)

                raw_items: List[dict] = []
                has_next  = False
                used_browser = False

                # ── Tier 1: curl ──────────────────────────────────
                html = _curl_get(url)
                if html:
                    raw_items = _schema_items_from_html(html)
                    if raw_items:
                        logger.info("[zoopla/curl] %d items on page %d", len(raw_items), page)
                        has_next = _html_has_next_page(html, page)

                # ── Tier 2: UC browser ────────────────────────────
                if not raw_items:
                    logger.info("[zoopla/browser] curl empty — launching UC browser")
                    used_browser = True
                    if uc_driver is None:
                        uc_driver = _make_uc_driver(headless=_UC_HEADLESS)
                        uc_driver.get(url)
                        time.sleep(random.uniform(3.0, 5.0))
                        if not _wait_for_cloudflare(uc_driver):
                            logger.warning("[zoopla/browser] CF not resolved — stopping city")
                            break
                    else:
                        # Already on previous page — click Next
                        if not _browser_click_next(uc_driver):
                            logger.info("[zoopla/browser] No next button — stopping city")
                            break
                        time.sleep(random.uniform(2.5, 4.0))
                        if not _wait_for_cloudflare(uc_driver):
                            break

                    raw_items = _dom_schema_items(uc_driver)
                    # Check if next button exists for pagination signal
                    try:
                        uc_driver.find_element(
                            "css selector",
                            '[data-testid="pagination-next"], a[rel="next"]',
                        )
                        has_next = True
                    except Exception:
                        has_next = False
                    logger.info("[zoopla/browser] %d items on page %d", len(raw_items), page)

                if not raw_items:
                    logger.info("[zoopla] No items on page %d — stopping city", page)
                    break

                # ── Apply on_listing callback & filter ────────────
                page_keep: List[Dict[str, Any]] = []
                for raw in raw_items:
                    listing = _normalize(raw, city)
                    if not listing:
                        continue
                    if on_listing is not None:
                        action = on_listing(listing)
                        if action == "stop":
                            logger.info("[zoopla] on_listing returned stop — ending city")
                            stop_city = True
                            break
                        if action == "skip":
                            continue  # duplicate — don't include
                    page_keep.append(listing)

                # ── Fetch full descriptions (only for kept listings) ─
                if fetch_details and page_keep:
                    logger.info(
                        "[zoopla/detail] Fetching descriptions for %d listings…",
                        len(page_keep),
                    )
                    for listing in page_keep:
                        full_desc = _fetch_full_description(listing["listing_url"])
                        if full_desc:
                            listing["description"] = full_desc
                        time.sleep(random.uniform(0.5, 1.5))

                results.extend(page_keep)
                logger.info(
                    "[zoopla] Page %d: %d kept → %d city total",
                    page, len(page_keep), len(results),
                )

                if stop_city or not has_next:
                    break

                # For curl path, next page is a URL increment; browser already
                # has the driver positioned — we click next at the top of the loop.
                if not used_browser:
                    time.sleep(random.uniform(1.5, 3.0))

                page += 1

        except Exception:
            logger.exception("[zoopla] Unexpected error scraping city=%s", city)
        finally:
            if uc_driver:
                try:
                    uc_driver.quit()
                except Exception:
                    pass

        return results
