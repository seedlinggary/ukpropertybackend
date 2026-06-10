"""
Shared HTTP tier helpers for auction/property scrapers.

Provides the 4-tier fallback chain (curl → browser → ScraperAPI DC → ScraperAPI residential)
and a TierTracker class for per-scraper usage stats.

All auction scrapers import from here; the Zoopla scraper keeps its own copy for
backward compatibility.
"""

import logging
import os
import random
import re
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_UC_HEADLESS = os.getenv("UC_HEADLESS", "1") == "1"

CF_SIGNALS = (
    "just a moment",
    "security verification",
    "please wait",
    "cf-browser-verification",
)

CURL_HEADERS = {
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


# ── Tier stats ────────────────────────────────────────────────────────────────

class TierTracker:
    """Per-scraper request tier statistics, reset once per city."""

    _TIERS = ("curl", "browser", "scraperapi_dc", "scraperapi_gb")
    _CONTEXTS = ("search", "detail")

    def __init__(self) -> None:
        self._stats = self._blank()

    def _blank(self) -> Dict:
        return {
            ctx: {t: {"attempts": 0, "successes": 0} for t in self._TIERS}
            for ctx in self._CONTEXTS
        }

    def reset(self) -> None:
        self._stats = self._blank()

    def track(self, context: str, tier: str, success: bool) -> None:
        self._stats[context][tier]["attempts"] += 1
        if success:
            self._stats[context][tier]["successes"] += 1

    def get(self) -> Dict:
        import copy
        return copy.deepcopy(self._stats)


# ── Chrome browser utilities ──────────────────────────────────────────────────

_CHROME_CANDIDATES = [
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/google-chrome",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

_CHROMEDRIVER_CANDIDATES = [
    "/usr/bin/chromedriver",
    "/usr/local/bin/chromedriver",
]


def find_chrome() -> Optional[str]:
    explicit = os.getenv("CHROME_EXECUTABLE_PATH", "")
    if explicit:
        return explicit
    for p in _CHROME_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


def find_chromedriver() -> Optional[str]:
    explicit = os.getenv("CHROMEDRIVER_PATH", "")
    if explicit and os.path.isfile(explicit):
        return explicit
    for p in _CHROMEDRIVER_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


def chrome_major_version() -> Optional[int]:
    env_ver = os.getenv("CHROME_VERSION", "")
    if env_ver:
        try:
            return int(env_ver)
        except ValueError:
            pass

    # Windows: query registry (chrome.exe --version hangs on Windows)
    if os.name == "nt":
        try:
            import winreg
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                for sub in (
                    r"SOFTWARE\WOW6432Node\Google\Update\Clients\{8A69D345-D564-463c-AFF1-A69D9E530F96}",
                    r"SOFTWARE\Google\Update\Clients\{8A69D345-D564-463c-AFF1-A69D9E530F96}",
                ):
                    try:
                        with winreg.OpenKey(hive, sub) as k:
                            ver_str = winreg.QueryValueEx(k, "pv")[0]
                            m = re.match(r"(\d+)", str(ver_str))
                            if m:
                                return int(m.group(1))
                    except OSError:
                        continue
        except Exception:
            pass

    import subprocess
    chrome_path = find_chrome()
    if not chrome_path:
        return None
    try:
        out = subprocess.run(
            [chrome_path, "--version"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        m = re.search(r"(\d+)\.", out)
        return int(m.group(1)) if m else None
    except Exception:
        return None


def make_uc_driver(headless: bool = True):
    """Construct an undetected ChromeDriver instance."""
    import undetected_chromedriver as uc
    opts = uc.ChromeOptions()
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--lang=en-GB")

    chrome_path = find_chrome()
    if chrome_path:
        opts.binary_location = chrome_path
        logger.debug("[browser] Chrome binary: %s", chrome_path)

    driver_exe = find_chromedriver()
    version_main = chrome_major_version()

    return uc.Chrome(
        options=opts,
        headless=headless,
        use_subprocess=True,
        driver_executable_path=driver_exe,
        version_main=version_main,
    )


def wait_for_cf_clear(driver, timeout: int = 5) -> bool:
    """Poll until Cloudflare clears. Returns False if still blocked."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not any(s in driver.title.lower() for s in CF_SIGNALS):
            return True
        time.sleep(1)
    return False


# ── Per-tier request functions ────────────────────────────────────────────────

def direct_curl(url: str, source: str = "scraper") -> Optional[str]:
    try:
        from curl_cffi import requests as cffi_req
        resp = cffi_req.get(
            url, headers=CURL_HEADERS, impersonate="chrome124",
            timeout=30, allow_redirects=True,
        )
        if resp.status_code != 200:
            logger.warning("[%s/curl] HTTP %d for %s", source, resp.status_code, url)
            return None
        html = resp.text
        if any(s in html.lower() for s in CF_SIGNALS):
            logger.info("[%s/curl] Cloudflare challenge detected for %s", source, url)
            return None
        return html
    except Exception:
        logger.debug("[%s/curl] request failed for %s", source, url, exc_info=True)
        return None


def browser_get_html(
    driver, url: str, source: str = "scraper", initial_wait: float = 2.5,
) -> Optional[str]:
    try:
        driver.get(url)
        time.sleep(random.uniform(initial_wait, initial_wait + 1.5))
        if not wait_for_cf_clear(driver):
            logger.warning("[%s/browser] CF not resolved for %s", source, url)
            return None
        return driver.page_source
    except Exception:
        logger.debug("[%s/browser] fetch failed for %s", source, url, exc_info=True)
        return None


def scraperapi_dc(url: str, api_key: str, source: str = "scraper") -> Optional[str]:
    """ScraperAPI datacenter proxy — 1 credit per request."""
    try:
        import requests as req
        resp = req.get(
            "https://api.scraperapi.com/",
            params={"api_key": api_key, "url": url},
            timeout=60,
        )
        if resp.status_code != 200:
            logger.warning("[%s/scraperapi-dc] HTTP %d for %s", source, resp.status_code, url)
            return None
        html = resp.text
        if any(s in html.lower() for s in CF_SIGNALS):
            logger.info("[%s/scraperapi-dc] CF still present for %s", source, url)
            return None
        return html
    except Exception:
        logger.warning("[%s/scraperapi-dc] request failed", source, exc_info=True)
        return None


def scraperapi_render(url: str, api_key: str, source: str = "scraper") -> Optional[str]:
    """ScraperAPI with JavaScript rendering — 5 credits per request."""
    try:
        import requests as req
        resp = req.get(
            "https://api.scraperapi.com/",
            params={"api_key": api_key, "url": url, "render": "true"},
            timeout=90,
        )
        if resp.status_code != 200:
            logger.warning("[%s/scraperapi-render] HTTP %d for %s", source, resp.status_code, url)
            return None
        html = resp.text
        if any(s in html.lower() for s in CF_SIGNALS):
            logger.info("[%s/scraperapi-render] CF still present for %s", source, url)
            return None
        return html
    except Exception:
        logger.warning("[%s/scraperapi-render] request failed", source, exc_info=True)
        return None


def scraperapi_gb(url: str, api_key: str, source: str = "scraper") -> Optional[str]:
    """ScraperAPI residential UK proxy — 5 credits per request."""
    try:
        import requests as req
        resp = req.get(
            "https://api.scraperapi.com/",
            params={"api_key": api_key, "url": url, "country_code": "gb"},
            timeout=60,
        )
        if resp.status_code != 200:
            logger.warning("[%s/scraperapi-gb] HTTP %d for %s", source, resp.status_code, url)
            return None
        html = resp.text
        if any(s in html.lower() for s in CF_SIGNALS):
            logger.info("[%s/scraperapi-gb] CF still present for %s", source, url)
            return None
        return html
    except Exception:
        logger.warning("[%s/scraperapi-gb] request failed", source, exc_info=True)
        return None


def fetch_html(
    url: str,
    source: str,
    tracker: TierTracker,
    context: str = "detail",
    uc_driver=None,
    allow_residential: bool = False,
    render_js: bool = False,
) -> Optional[str]:
    """
    Full 4-tier fallback chain with tracking.

    render_js=True causes tier-3 to use ScraperAPI with JS rendering (5 credits)
    instead of the default datacenter proxy (1 credit).  Use it when the page
    is known to be JavaScript-rendered and curl/browser have already failed.
    """
    api_key = os.getenv("SCRAPERAPI_KEY", "")

    # Tier 1: direct curl
    html = direct_curl(url, source)
    tracker.track(context, "curl", bool(html))
    if html:
        return html

    # Tier 2: undetected browser
    if uc_driver is not None:
        html = browser_get_html(uc_driver, url, source)
        tracker.track(context, "browser", bool(html))
        if html:
            return html

    if api_key:
        # Tier 3: ScraperAPI (datacenter or render)
        html = scraperapi_render(url, api_key, source) if render_js else scraperapi_dc(url, api_key, source)
        tracker.track(context, "scraperapi_dc", bool(html))
        if html:
            return html

        # Tier 4: ScraperAPI residential UK
        if allow_residential:
            html = scraperapi_gb(url, api_key, source)
            tracker.track(context, "scraperapi_gb", bool(html))
            if html:
                return html

    return None


# ── Common parsing helpers ─────────────────────────────────────────────────────

def parse_guide_price(text: str) -> Optional[int]:
    """
    Parse an auction guide price string to an integer (pence-free £ value).

    Handles:  "£375,000+"  "£55,000-£65,000"  "£7.5M+"  "TBA"  "POA"
    Returns the lower bound (first price found), or None if unparseable.
    """
    text = str(text or "").strip()
    if not text or text.upper() in ("TBA", "N/A", "POA", "-", ""):
        return None
    # Millions suffix: £7.5M+
    m = re.search(r"£([\d,\.]+)\s*[Mm]", text)
    if m:
        try:
            return int(float(m.group(1).replace(",", "")) * 1_000_000)
        except ValueError:
            pass
    # Standard: £375,000
    m = re.search(r"£([\d,]+)", text)
    if m:
        try:
            return int(m.group(1).replace(",", ""))
        except ValueError:
            pass
    return None


def normalize_property_type(text: str) -> Optional[str]:
    """Map a free-text property type description to a short canonical string."""
    if not text:
        return None
    t = text.lower()
    if any(w in t for w in ("flat", "apartment", "studio flat")):
        return "flat"
    if any(w in t for w in ("terraced", "semi-detached", "detached", "bungalow", "cottage", "house")):
        return "house"
    if "land" in t:
        return "land"
    if any(w in t for w in ("commercial", "office", "retail", "shop", "warehouse", "industrial")):
        return "commercial"
    if "hmo" in t:
        return "house"
    return text[:100]


def city_from_address(address: str) -> Optional[str]:
    """
    Best-effort city extraction from a UK property address.

    Splits on commas; returns the part immediately before a postcode-like
    token (or the last non-postcode part).
    """
    if not address:
        return None
    parts = [p.strip() for p in address.split(",") if p.strip()]
    if not parts:
        return None
    # Walk from the end, skip postcode tokens
    for part in reversed(parts):
        if re.match(r"^[A-Z]{1,2}\d", part):
            continue
        # Skip pure country names
        if part.lower() in ("england", "wales", "scotland", "uk", "united kingdom"):
            continue
        return part.lower()
    return parts[0].lower()


def parse_size_m2(text: str) -> Optional[float]:
    """
    Parse a floor area from free text.  Returns m² as a float.
    Handles: "76 sq m", "12,680 sq ft", "76m²", "1,178 sqm", "76.5 m2"
    Converts sq ft to m² (× 0.0929).  Returns the first match found.
    """
    import re as _re
    if not text:
        return None
    # sq m variants first (higher precision)
    m = _re.search(r"([\d,]+(?:\.\d+)?)\s*(?:sq\.?\s*m|sqm|m2|m²)", text, _re.IGNORECASE)
    if m:
        try:
            return round(float(m.group(1).replace(",", "")), 1)
        except ValueError:
            pass
    # sq ft → m²
    m = _re.search(r"([\d,]+(?:\.\d+)?)\s*sq\.?\s*(?:ft|feet)", text, _re.IGNORECASE)
    if m:
        try:
            return round(float(m.group(1).replace(",", "")) * 0.0929, 1)
        except ValueError:
            pass
    return None


_UK_POSTCODE_RE = re.compile(
    r"\b([A-Z]{1,2}\d{1,2}[A-Z]?\s*\d[A-Z]{2})\b", re.IGNORECASE
)

_geocode_cache: Dict[str, tuple] = {}


def geocode_address(address: str) -> tuple:
    """
    Return (lat, lng) for a UK address string, or (None, None) if lookup fails.

    Strategy:
      1. Extract UK postcode → call postcodes.io (fast, free, accurate centroid).
      2. Fall back to Nominatim OSM with the full address string.

    Results are cached in-process to avoid duplicate requests.
    """
    if not address:
        return None, None

    key = address.strip().upper()
    if key in _geocode_cache:
        return _geocode_cache[key]

    try:
        import requests as _req

        # ── Tier 1: postcodes.io ──────────────────────────────────────────────
        pc_m = _UK_POSTCODE_RE.search(address)
        if pc_m:
            pc = pc_m.group(1).replace(" ", "").upper()
            try:
                r = _req.get(
                    f"https://api.postcodes.io/postcodes/{pc}",
                    timeout=8,
                )
                if r.status_code == 200:
                    res = r.json().get("result", {})
                    lat = res.get("latitude")
                    lng = res.get("longitude")
                    if lat is not None and lng is not None:
                        result = (float(lat), float(lng))
                        _geocode_cache[key] = result
                        logger.debug("[geocode] postcodes.io %s -> %s", pc, result)
                        return result
            except Exception:
                pass

        # ── Tier 2: Nominatim ─────────────────────────────────────────────────
        try:
            time.sleep(1.1)  # Nominatim ToS: max 1 req/s
            r = _req.get(
                "https://nominatim.openstreetmap.org/search",
                params={"q": address, "format": "json", "countrycodes": "gb", "limit": 1},
                headers={"User-Agent": "ukproperty-scraper/1.0"},
                timeout=10,
            )
            if r.status_code == 200:
                hits = r.json()
                if hits:
                    lat = float(hits[0]["lat"])
                    lng = float(hits[0]["lon"])
                    if 49.0 < lat < 61.0 and -8.0 < lng < 2.0:
                        result = (lat, lng)
                        _geocode_cache[key] = result
                        logger.debug("[geocode] nominatim %r -> %s", address[:60], result)
                        return result
        except Exception:
            pass

        # ── Tier 2b: Nominatim with locality only (last 2 comma parts) ─────────
        parts = [p.strip() for p in address.split(",") if p.strip()]
        if len(parts) >= 3:
            locality = ", ".join(parts[-2:])
            if locality.upper() != key:
                try:
                    time.sleep(1.1)
                    r = _req.get(
                        "https://nominatim.openstreetmap.org/search",
                        params={"q": locality, "format": "json", "countrycodes": "gb", "limit": 1},
                        headers={"User-Agent": "ukproperty-scraper/1.0"},
                        timeout=10,
                    )
                    if r.status_code == 200:
                        hits = r.json()
                        if hits:
                            lat = float(hits[0]["lat"])
                            lng = float(hits[0]["lon"])
                            if 49.0 < lat < 61.0 and -8.0 < lng < 2.0:
                                result = (lat, lng)
                                _geocode_cache[key] = result
                                logger.debug("[geocode] nominatim-locality %r -> %s", locality, result)
                                return result
                except Exception:
                    pass

        # ── Tier 3: postcodes.io partial postcode (district-level) ────────────
        # Useful when address has only an outward code like "SW1V" with no inward.
        partial_m = re.search(r"\b([A-Z]{1,2}\d{1,2}[A-Z]?)\b", address, re.IGNORECASE)
        if partial_m:
            try:
                pc_out = partial_m.group(1).upper()
                r = _req.get(
                    f"https://api.postcodes.io/outcodes/{pc_out}",
                    timeout=8,
                )
                if r.status_code == 200:
                    res = r.json().get("result", {})
                    lat = res.get("latitude")
                    lng = res.get("longitude")
                    if lat is not None and lng is not None:
                        result = (float(lat), float(lng))
                        _geocode_cache[key] = result
                        logger.debug("[geocode] outcode %s -> %s", pc_out, result)
                        return result
            except Exception:
                pass

    except Exception:
        pass

    _geocode_cache[key] = (None, None)
    return None, None


def clean_text(text: Optional[str]) -> Optional[str]:
    """Strip HTML tags, decode entities, collapse whitespace."""
    if not text:
        return None
    import html as html_mod
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None
