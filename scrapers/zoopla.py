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

# ── Env-var feature flags ────────────────────────────────────────────────────
#
# FETCH_DETAILS           "1" (default) | "0"
#   Set to "0" to skip detail page fetching entirely (saves all detail credits).
#
# SCRAPERAPI_RESIDENTIAL  "0" (default) | "1"
#   Whether to allow the 5-credit residential UK proxy as a last-resort fallback.
#   Off by default — datacenter (1 credit) is tried first and is usually sufficient.
#
# Fallback chain for every URL (search pages and detail pages):
#   1. Direct curl          — free, fast, works on home IPs
#   2. UC browser           — free, Chrome installed on Railway via Dockerfile
#   3. ScraperAPI datacenter— 1 credit, tried if both free tiers fail
#   4. ScraperAPI residential 5 credits, only if SCRAPERAPI_RESIDENTIAL=1
# ────────────────────────────────────────────────────────────────────────────
_FETCH_DETAILS           = os.getenv("FETCH_DETAILS",           "0") == "1"
_SCRAPERAPI_RESIDENTIAL  = os.getenv("SCRAPERAPI_RESIDENTIAL",  "0") == "1"

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
    return f"{BASE_URL.format(city=city_slug)}?results_sort={SORT_PARAM}&pn={page}"


# ─────────────────────────────────────────────────────────────
# Shared HTTP helper
# ─────────────────────────────────────────────────────────────

def _fetch_html(url: str, uc_driver=None, context: str = "detail") -> Optional[str]:
    """
    Full fallback chain — used for detail pages (context='detail').
    Search pages use this same chain inline in fetch_listings so each tier
    can be tracked separately for the email report.
      1. Direct curl           free, fast
      2. UC browser            free, Chrome installed via Dockerfile
      3. ScraperAPI datacenter 1 credit
      4. ScraperAPI residential 5 credits, only if SCRAPERAPI_RESIDENTIAL=1
    """
    html = _direct_curl(url)
    _track(context, "curl", bool(html))
    if html:
        return html

    if uc_driver is not None:
        html = _browser_get_html(uc_driver, url)
        _track(context, "browser", bool(html))
        if html:
            return html

    api_key = os.getenv("SCRAPERAPI_KEY", "")
    if api_key:
        html = _scraperapi_get_dc(url, api_key)
        _track(context, "scraperapi_dc", bool(html))
        if html:
            return html

        if _SCRAPERAPI_RESIDENTIAL:
            html = _scraperapi_get(url, api_key)
            _track(context, "scraperapi_gb", bool(html))
            if html:
                return html

    return None


def _direct_curl(url: str) -> Optional[str]:
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
            logger.info("[zoopla/curl] Cloudflare challenge detected")
            return None
        return html
    except Exception:
        logger.debug("[zoopla/curl] request failed", exc_info=True)
        return None


# ─────────────────────────────────────────────────────────────
# Per-tier stats — reset per city, read by scraper_service
# ─────────────────────────────────────────────────────────────

def _empty_tier() -> Dict[str, int]:
    return {"attempts": 0, "successes": 0}

_tier_stats: Dict[str, Dict[str, Dict[str, int]]] = {
    "search": {
        "curl":          _empty_tier(),
        "browser":       _empty_tier(),
        "scraperapi_dc": _empty_tier(),
        "scraperapi_gb": _empty_tier(),
    },
    "detail": {
        "curl":          _empty_tier(),
        "browser":       _empty_tier(),
        "scraperapi_dc": _empty_tier(),
        "scraperapi_gb": _empty_tier(),
    },
}


def reset_tier_stats() -> None:
    for ctx in _tier_stats.values():
        for t in ctx.values():
            t["attempts"] = 0
            t["successes"] = 0


def get_tier_stats() -> Dict[str, Dict[str, Dict[str, int]]]:
    import copy
    return copy.deepcopy(_tier_stats)


def _track(context: str, tier: str, success: bool) -> None:
    _tier_stats[context][tier]["attempts"] += 1
    if success:
        _tier_stats[context][tier]["successes"] += 1


def _scraperapi_get(url: str, api_key: str) -> Optional[str]:
    """ScraperAPI residential UK proxy — 5 credits per request."""
    try:
        import requests as req
        resp = req.get(
            "https://api.scraperapi.com/",
            params={"api_key": api_key, "url": url, "country_code": "gb"},
            timeout=60,
        )
        if resp.status_code != 200:
            logger.warning("[zoopla/scraperapi-gb] HTTP %d for %s", resp.status_code, url)
            return None
        html = resp.text
        if any(s in html.lower() for s in _CF_SIGNALS):
            logger.info("[zoopla/scraperapi-gb] Cloudflare still present for %s", url)
            return None
        return html
    except Exception:
        logger.warning("[zoopla/scraperapi-gb] request failed", exc_info=True)
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
    # Check for pn= without requiring ? prefix — works whether pn is the first
    # or a subsequent query param (e.g. &pn=2 when results_sort comes first).
    return f"pn={page + 1}" in html or 'rel="next"' in html


# ─────────────────────────────────────────────────────────────
# Tier 2: undetected-chromedriver browser
# ─────────────────────────────────────────────────────────────

def _chrome_major_version() -> Optional[int]:
    """Read the major version from the Chrome/Chromium binary (e.g. 147)."""
    # Allow explicit override via env var (useful when os.path.isfile fails on
    # paths with spaces or permission quirks on Windows).
    env_ver = os.getenv("CHROME_VERSION", "")
    if env_ver:
        try:
            return int(env_ver)
        except ValueError:
            pass

    import subprocess
    # Try env var path first, then fall back to candidate list
    chrome_path = os.getenv("CHROME_EXECUTABLE_PATH", "")
    if not chrome_path:
        for p in [
            "/usr/bin/chromium", "/usr/bin/chromium-browser",
            "/usr/bin/google-chrome-stable", "/usr/bin/google-chrome",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ]:
            if os.path.isfile(p):
                chrome_path = p
                break
    if not chrome_path:
        return None
    try:
        out = subprocess.run(
            [chrome_path, "--version"],
            capture_output=True, text=True, timeout=5,
        ).stdout  # e.g. "Google Chrome 148.0.7778.179 ..."
        m = re.search(r"(\d+)\.", out)
        return int(m.group(1)) if m else None
    except Exception:
        return None


_CHROME_CANDIDATE_PATHS = [
    # Linux — Railway / Render / Dockerfile (chromium package)
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/google-chrome",
    # Windows — local dev
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

_CHROMEDRIVER_CANDIDATE_PATHS = [
    "/usr/bin/chromedriver",
    "/usr/local/bin/chromedriver",
]


def _find_chrome() -> Optional[str]:
    """Return the first Chrome/Chromium binary that exists on this machine."""
    explicit = os.getenv("CHROME_EXECUTABLE_PATH", "")
    if explicit:
        return explicit
    for p in _CHROME_CANDIDATE_PATHS:
        if os.path.isfile(p):
            return p
    return None


def _find_chromedriver() -> Optional[str]:
    """Return the first chromedriver binary that exists on this machine."""
    explicit = os.getenv("CHROMEDRIVER_PATH", "")
    if explicit and os.path.isfile(explicit):
        return explicit
    for p in _CHROMEDRIVER_CANDIDATE_PATHS:
        if os.path.isfile(p):
            return p
    return None


def _make_uc_driver(headless: bool = True):
    import undetected_chromedriver as uc
    opts = uc.ChromeOptions()
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--lang=en-GB")

    chrome_path = _find_chrome()
    if chrome_path:
        opts.binary_location = chrome_path
        logger.debug("[zoopla/browser] Chrome binary: %s", chrome_path)

    driver_executable = _find_chromedriver()
    if driver_executable:
        logger.debug("[zoopla/browser] ChromeDriver: %s", driver_executable)

    version_main = _chrome_major_version()

    return uc.Chrome(
        options=opts,
        headless=headless,
        use_subprocess=True,
        driver_executable_path=driver_executable,
        version_main=version_main,
    )


def _wait_for_cloudflare(driver, timeout: int = 5) -> bool:
    """
    Check whether Cloudflare has cleared after an initial page load.
    We already slept before calling this, so just poll briefly then give up —
    a headless server browser will never solve a CF challenge on its own.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        title = driver.title.lower()
        if not any(s in title for s in _CF_SIGNALS):
            return True
        time.sleep(1)
    logger.warning("[zoopla/browser] CF detected — switching to ScraperAPI")
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


def _browser_set_sort(driver, value: str = "newest_listings") -> None:
    """Select the sort dropdown on the first page load (once per city)."""
    try:
        from selenium.webdriver.support.ui import Select
        el = driver.find_element("css selector", "select[name='results_sort']")
        Select(el).select_by_value(value)
        time.sleep(2)
        logger.info("[zoopla/browser] Sort set to %s", value)
    except Exception:
        logger.debug("[zoopla/browser] Could not set sort dropdown", exc_info=True)




# ─────────────────────────────────────────────────────────────
# Detail page: full "About this property" description
# ─────────────────────────────────────────────────────────────

def _clean_text(text: Optional[str]) -> Optional[str]:
    """Strip HTML tags, decode entities, collapse whitespace."""
    if not text:
        return None
    import html as html_mod
    text = re.sub(r'<[^>]+>', ' ', text)       # remove any HTML tags
    text = html_mod.unescape(text)              # &amp; → &, &#39; → ', &nbsp; → space, etc.
    text = re.sub(r'\s+', ' ', text).strip()   # collapse whitespace
    return text or None


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
        return _clean_text(best)

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
        return _clean_text(best)

    # Strategy 3: "About this property" heading in raw HTML
    for pattern in (
        r'[Aa]bout this property\s*</[^>]+>\s*<[^>]+>(.*?)(?=<h[1-6]|</section|</article)',
        r'data-testid=["\']listing-description["\'][^>]*>(.*?)</(?:div|p|section)',
        r'data-testid=["\']truncated-text["\'][^>]*>(.*?)</(?:div|p|section)',
    ):
        m = re.search(pattern, html, re.DOTALL | re.IGNORECASE)
        if m:
            text = _clean_text(m.group(1))
            if text and len(text) >= 100:
                return text

    return _clean_text(best)


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


def _fetch_full_description(listing_url: str, uc_driver=None) -> Optional[str]:
    html = _fetch_html(listing_url, uc_driver=uc_driver)
    return _full_description_from_html(html) if html else None


def _scraperapi_get_dc(url: str, api_key: str) -> Optional[str]:
    """ScraperAPI via datacenter proxy — 1 credit per request (no country_code)."""
    try:
        import requests as req
        resp = req.get(
            "https://api.scraperapi.com/",
            params={"api_key": api_key, "url": url},
            timeout=60,
        )
        if resp.status_code != 200:
            logger.warning("[zoopla/scraperapi-dc] HTTP %d for %s", resp.status_code, url)
            return None
        html = resp.text
        if any(s in html.lower() for s in _CF_SIGNALS):
            logger.info("[zoopla/scraperapi-dc] Cloudflare still present for %s", url)
            return None
        return html
    except Exception:
        logger.warning("[zoopla/scraperapi-dc] request failed", exc_info=True)
        return None


def _browser_get_html(driver, url: str) -> Optional[str]:
    """Navigate the already-open UC browser to a URL and return page source."""
    try:
        driver.get(url)
        time.sleep(random.uniform(1.5, 2.5))
        if not _wait_for_cloudflare(driver):
            return None
        return driver.page_source
    except Exception:
        logger.debug("[zoopla/browser] detail fetch failed for %s", url, exc_info=True)
        return None


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
            "description":   _clean_text(item.get("description")),
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

    def reset_tier_stats(self) -> None:
        reset_tier_stats()

    def get_tier_stats(self) -> Dict[str, Any]:
        return get_tier_stats()

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

                # ── Tier 1: direct curl (free) ────────────────────
                # ── Tier 1: direct curl (free) ────────────────────
                html = _direct_curl(url)
                _track("search", "curl", bool(html))
                if html:
                    raw_items = _schema_items_from_html(html)
                    if raw_items:
                        logger.info("[zoopla/curl] %d items on page %d", len(raw_items), page)
                        has_next = _html_has_next_page(html, page)

                # ── Tier 2: UC browser (free) ─────────────────────
                if not raw_items:
                    used_browser = True
                    if uc_driver is None:
                        uc_driver = _make_uc_driver(headless=_UC_HEADLESS)

                    uc_driver.get(url)
                    time.sleep(random.uniform(4.0, 6.0))
                    if _wait_for_cloudflare(uc_driver):
                        if page == 1:
                            _browser_set_sort(uc_driver, SORT_PARAM)
                        raw_items = _dom_schema_items(uc_driver)
                        has_next = _html_has_next_page(uc_driver.page_source, page)
                        logger.info("[zoopla/browser] %d items on page %d", len(raw_items), page)
                    else:
                        logger.warning("[zoopla/browser] CF not resolved on page %d — trying ScraperAPI", page)
                    _track("search", "browser", bool(raw_items))

                # ── Tier 3: ScraperAPI datacenter (1 credit) ──────
                api_key = os.getenv("SCRAPERAPI_KEY", "")
                if not raw_items and api_key:
                    html = _scraperapi_get_dc(url, api_key)
                    _track("search", "scraperapi_dc", bool(html))
                    if html:
                        raw_items = _schema_items_from_html(html)
                        if raw_items:
                            has_next = _html_has_next_page(html, page)
                            logger.info("[zoopla/scraperapi-dc] %d items on page %d", len(raw_items), page)

                # ── Tier 4: ScraperAPI residential (5 credits) ────
                if not raw_items and api_key and _SCRAPERAPI_RESIDENTIAL:
                    html = _scraperapi_get(url, api_key)
                    _track("search", "scraperapi_gb", bool(html))
                    if html:
                        raw_items = _schema_items_from_html(html)
                        if raw_items:
                            has_next = _html_has_next_page(html, page)
                            logger.info("[zoopla/scraperapi-gb] %d items on page %d", len(raw_items), page)

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
                if fetch_details and _FETCH_DETAILS and page_keep:
                    logger.info("[zoopla/detail] Fetching descriptions for %d listings…", len(page_keep))
                    for listing in page_keep:
                        full_desc = _fetch_full_description(
                            listing["listing_url"], uc_driver=uc_driver,
                        )
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
