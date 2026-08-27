"""
GET /api/property-data?lat=XX&lng=XX

Aggregates free UK property data in parallel:
  - postcodes.io (postcode, region, country, rural/urban, ward, LSOA)
  - EPC Register (energy rating) — needs EPC_BEARER_TOKEN in .env
  - Land Registry Price Paid (sales history + transaction category)
  - planning.data.gov.uk (12 planning/environmental datasets)
  - Environment Agency / planning.data.gov.uk (flood zones)
  - data.police.uk (nearby crime statistics)
  - OpenStreetMap Overpass (nearby schools, transport, shops, GPs, parks)
"""

import hashlib
import logging
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from flask import Blueprint, jsonify, request
import requests

try:
    from bs4 import BeautifulSoup as _BS
    _BS4_OK = True
except ImportError:
    _BS = None  # type: ignore[assignment,misc]
    _BS4_OK = False

logger = logging.getLogger(__name__)
property_data_bp = Blueprint("property_data", __name__)

EPC_BEARER = os.getenv("EPC_BEARER_TOKEN", "")
TIMEOUT = 5  # gunicorn worker timeout is 120s; keep individual calls short so stalls don't pile up

# ---------------------------------------------------------------------------
# In-memory response cache: keyed by (lat 3dp, lng 3dp) → property data dict
# 3 decimal places ≈ 110m precision, which is fine for postcode-level lookups.
# Capped at 200 entries; evicts the 20 oldest when full.
# ---------------------------------------------------------------------------
_PROP_DATA_CACHE: dict[str, tuple] = {}  # key → (result_dict, timestamp)
_PROP_DATA_TTL = 600  # 10 minutes

# ---------------------------------------------------------------------------
# Land Registry helpers
# Handles both old {_value: "F"} and new linked-data {prefLabel:[{_value:"..."}]} formats
# ---------------------------------------------------------------------------

def _lr_val(obj):
    if obj is None:
        return None
    if not isinstance(obj, dict):
        return obj
    if "_value" in obj:
        return obj["_value"]
    for key in ("prefLabel", "label"):
        lst = obj.get(key)
        if isinstance(lst, list) and lst:
            first = lst[0]
            if isinstance(first, dict) and "_value" in first:
                return first["_value"]
    about = obj.get("_about", "")
    if "/" in about:
        return about.split("/")[-1]
    return None

_PROP_TYPE = {
    "D": "Detached", "S": "Semi-Detached", "T": "Terraced", "F": "Flat", "O": "Other",
    "detached": "Detached", "semi-detached": "Semi-Detached",
    "terraced": "Terraced", "flat-maisonette": "Flat", "other": "Other",
}
_TENURE = {
    "F": "Freehold", "L": "Leasehold",
    "freehold": "Freehold", "leasehold": "Leasehold",
}

def _parse_lr_date(raw: str) -> str:
    if not raw:
        return ""
    raw = raw.strip()
    if len(raw) >= 10 and raw[4:5] == "-":
        return raw[:10]
    for fmt in ("%a, %d %b %Y", "%d %b %Y", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw

# ---------------------------------------------------------------------------
# Crime category labels
# ---------------------------------------------------------------------------

_CRIME_LABELS = {
    "anti-social-behaviour":  "Anti-social behaviour",
    "bicycle-theft":          "Bicycle theft",
    "burglary":               "Burglary",
    "criminal-damage-arson":  "Criminal damage & arson",
    "drugs":                  "Drugs",
    "other-crime":            "Other crime",
    "other-theft":            "Other theft",
    "possession-of-weapons":  "Weapons possession",
    "public-order":           "Public order",
    "robbery":                "Robbery",
    "shoplifting":            "Shoplifting",
    "theft-from-the-person":  "Theft from person",
    "vehicle-crime":          "Vehicle crime",
    "violent-crime":          "Violence & sexual offences",
    "computer-misuse":        "Computer misuse",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

# ---------------------------------------------------------------------------
# Data fetchers
# ---------------------------------------------------------------------------

def _postcode_from_latlng(lat: float, lng: float):
    """Returns a dict of postcode + area metadata, or None."""
    try:
        r = requests.get(
            "https://api.postcodes.io/postcodes",
            params={"lon": lng, "lat": lat, "limit": 1},
            timeout=5,
        )
        if r.ok:
            results = (r.json() or {}).get("result") or []
            if results:
                res = results[0]
                codes = res.get("codes") or {}
                return {
                    "postcode":    res.get("postcode"),
                    "region":      res.get("region"),
                    "country":     res.get("country"),
                    "rural_urban": res.get("rural_urban"),
                    "ward":        res.get("admin_ward"),
                    "district":    res.get("admin_district"),
                    "lsoa":        codes.get("lsoa"),
                    "national_park": res.get("nuts") if res.get("nuts") and "park" in str(res.get("nuts", "")).lower() else None,
                }
    except Exception:
        pass
    return None


def _epc_match_score(known_address: str, record: dict,
                     paon=None, saon=None) -> int:
    """
    Score 0–100 for how well an EPC record matches the known address.

    When paon/saon are supplied (from LR structured fields) they are used
    directly — more reliable than parsing a joined address string because:
      - saon may be a bare unit number ("3") with no "flat" keyword, which the
        string parser would mis-identify as the building number
      - we know definitively whether the LR record is a flat (saon non-empty)
        or a house (saon empty), enabling flat-presence hard exclusions

    When paon/saon are None the function falls back to parsing known_address
    (used by the per-property EPC lookup which doesn't have structured fields).
    """
    def _tokens(s: str) -> list:
        return re.sub(r"[^a-z0-9\s]", "", s.lower()).split()

    _FLAT_WORDS = {"flat", "apartment", "apt", "unit", "suite"}

    # ── EPC side: always parsed from the address text fields ─────────────────
    epc_raw = " ".join(
        str(record.get(k) or "")
        for k in ("addressLine1", "addressLine2", "addressLine3", "address1", "address2")
    )
    epc = _tokens(epc_raw)
    if not epc:
        return 0

    epc_has_flat_word = any(t in _FLAT_WORDS for t in epc)

    def _parse_identifiers(toks: list):
        """Parse (building_base, building_suffix, unit_label) from a token list."""
        unit_label = None
        excluded: set = set()
        for i, t in enumerate(toks[:7]):
            if t in _FLAT_WORDS and i + 1 < len(toks):
                unit_label = toks[i + 1].lower()
                excluded.update({i, i + 1})
                break
        base, suf = None, ""
        for i, t in enumerate(toks[:10]):
            if i in excluded:
                continue
            if re.match(r"^\d{1,5}$", t):
                base, suf = int(t), ""
                break
            m = re.match(r"^(\d{1,5})([a-z]{1,2})$", t)
            if m:
                d, s2 = m.group(1), m.group(2)
                if len(d) == 1 and len(s2) == 2:
                    continue
                base, suf = int(d), s2
                break
        if base is None and unit_label is not None:
            unit_label = None
            for t in toks[:10]:
                if re.match(r"^\d{1,5}$", t):
                    base, suf = int(t), ""
                    break
                m = re.match(r"^(\d{1,5})([a-z]{1,2})$", t)
                if m:
                    d, s2 = m.group(1), m.group(2)
                    if not (len(d) == 1 and len(s2) == 2):
                        base, suf = int(d), s2
                        break
        return base, suf, unit_label

    ebase, esuf, eunit = _parse_identifiers(epc)

    # ── LR (known) side ───────────────────────────────────────────────────────
    structured = paon is not None  # caller provided structured LR fields
    if structured:
        # Parse building number from paon ("15" -> 15, "25A" -> 25+a,
        # "CENTRAL HOUSE" -> None).  Ignore postcode-style codes (e.g. "3QY").
        paon_toks = _tokens(paon or "")
        kbase, ksuf = None, ""
        for t in paon_toks:
            if re.match(r"^\d{1,5}$", t):
                kbase, ksuf = int(t), ""
                break
            m = re.match(r"^(\d{1,5})([a-z]{1,2})$", t)
            if m:
                d, s2 = m.group(1), m.group(2)
                if not (len(d) == 1 and len(s2) == 2):
                    kbase, ksuf = int(d), s2
                    break

        # Extract unit from saon — saon="" means house (no sub-unit).
        # saon may be: "FLAT F", "FLAT 3", "3" (bare number), "F" (bare letter),
        # "APARTMENT 4B", "GROUND FLOOR", "BASEMENT".
        saon_str = (saon or "").strip()
        saon_toks = _tokens(saon_str)
        kunit = None
        if saon_toks:
            if saon_toks[0] in _FLAT_WORDS and len(saon_toks) > 1:
                kunit = saon_toks[1].lower()          # "FLAT F" -> "f"
            else:
                kunit = " ".join(saon_toks).lower()   # "3" -> "3", "GROUND FLOOR" -> "ground floor"

        # Handle legacy paon like "FLAT 3, 15" where flat info is in paon
        if kunit is None and paon_toks:
            for i, t in enumerate(paon_toks[:5]):
                if t in _FLAT_WORDS and i + 1 < len(paon_toks):
                    kunit = paon_toks[i + 1].lower()
                    excl = {i, i + 1}
                    kbase, ksuf = None, ""
                    for j, t2 in enumerate(paon_toks):
                        if j in excl:
                            continue
                        if re.match(r"^\d{1,5}$", t2):
                            kbase, ksuf = int(t2), ""
                            break
                        m2 = re.match(r"^(\d{1,5})([a-z]{1,2})$", t2)
                        if m2:
                            d2, s3 = m2.group(1), m2.group(2)
                            if not (len(d2) == 1 and len(s3) == 2):
                                kbase, ksuf = int(d2), s3
                                break
                    break
    else:
        # Fallback: parse the pre-joined address string (used by non-street EPC lookups)
        known = _tokens(known_address)
        if not known:
            return 0
        kbase, ksuf, kunit = _parse_identifiers(known)

    # ── Hard exclusions ────────────────────────────────────────────────────────

    # 1. Building numbers differ (13 vs 15)
    if kbase is not None and ebase is not None and kbase != ebase:
        return 0

    # 2. Building suffixes differ (25A vs 25C)
    if kbase is not None and ebase is not None and kbase == ebase:
        if ksuf and esuf and ksuf != esuf:
            return 0

    # 3–5 below use flat-presence knowledge only available in structured mode
    # (in fallback mode we keep the original looser logic to avoid regressions)
    if structured:
        # Is kunit a "precise" identifier — single letter, digit, or short
        # alphanumeric like "f", "3", "4b"?  Contrasted with descriptors like
        # "ground floor" or "basement" which can't be confirmed against EPC text.
        kunit_precise = (kunit is not None and
                         bool(re.match(r"^[a-z0-9]{1,4}$", kunit)))

        if kunit_precise:
            # LR identifies a specific flat unit precisely.
            # EPC MUST also have an explicit matching unit — floor descriptors
            # ("Ground Floor Flat") and whole-building certs are rejected.
            if not epc_has_flat_word:
                return 0   # LR=flat, EPC=house/whole-building cert
            if eunit is None or eunit != kunit:
                return 0   # EPC is a flat but unit unconfirmable or different
        elif kunit is not None:
            # LR has a descriptor saon ("GROUND FLOOR", "BASEMENT").
            # EPC must at least be some kind of flat cert.
            if not epc_has_flat_word:
                return 0
        else:
            # LR has no sub-unit (saon="") → this is a house/whole-building sale.
            # Reject any EPC cert that describes a specific flat.
            if epc_has_flat_word:
                return 0
    else:
        # Fallback mode: original rule — both have labels and they differ
        if kunit is not None and eunit is not None and kunit != eunit:
            return 0

    # ── Scoring ────────────────────────────────────────────────────────────────
    _SKIP = {
        "flat", "floor", "ground", "first", "second", "third",
        "road", "street", "avenue", "close", "drive", "lane", "way",
        "place", "grove", "gardens", "court", "house", "and", "the",
        "apartment", "apt", "unit", "suite",
    }
    ref_toks = (_tokens(" ".join(filter(None, [saon or "", paon or ""])))
                if structured else _tokens(known_address))
    kwords = {t for t in ref_toks if len(t) > 2 and not re.match(r"^\d", t) and t not in _SKIP}
    ewords = {t for t in epc      if len(t) > 2 and not re.match(r"^\d", t) and t not in _SKIP}
    overlap = kwords & ewords

    score = 0
    if kbase is not None and ebase is not None and kbase == ebase:
        score += 40
        if overlap:
            score += min(len(overlap) * 25, 60)
    elif kbase is None and overlap:
        score += min(len(overlap) * 25, 60)

    return min(score, 100) if score else 0


def _fetch_epc(postcode: str, address: str = "", *,
               paon: str | None = None, saon: str | None = None) -> dict:
    if not EPC_BEARER:
        return {"found": False, "error": "EPC_BEARER_TOKEN not configured in .env"}

    headers = {"Accept": "application/json", "Authorization": f"Bearer {EPC_BEARER}"}
    api_url = "https://api.get-energy-performance-data.communities.gov.uk/api/domestic/search"

    def _api_get(params: dict) -> list:
        try:
            r = requests.get(api_url, params=params, headers=headers, timeout=TIMEOUT)
            if r.ok:
                data = (r.json() or {}).get("data", [])
                # API returns {"data": {"error": "..."}} (a dict, not a list) when no
                # certificates match.  Guard here so callers always receive a list.
                return data if isinstance(data, list) else []
        except Exception:
            pass
        return []

    def _score(rec: dict) -> int:
        return _epc_match_score(address, rec, paon=paon, saon=saon)

    def _best(records: list) -> tuple:
        """Return (best_score, sorted_records)."""
        if not records or (not address and paon is None):
            return 0, records
        srt = sorted(records, key=_score, reverse=True)
        return _score(srt[0]), srt

    try:
        # ── Stage 1: targeted address+postcode search ─────────────────────────
        # Use paon (building number) when available — more reliable than splitting
        # the joined address string, which may start with a flat/unit label.
        if address or paon:
            if paon:
                street_part = paon.strip()
            else:
                # Fallback: first comma-segment (works for "2 Bisley Place, …" but
                # not for flat addresses — paon path avoids this)
                street_part = address.split(",")[0].strip()
            targeted = _api_get({"postcode": postcode, "address": street_part, "size": 10})
            if targeted:
                score, scored = _best(targeted)
                if score > 0:
                    matched = [r for r in scored if _score(r) > 0]
                    return {"found": True, "records": matched}

        # ── Stage 2: paginated postcode search ───────────────────────────────
        # Fetches up to 100 records (4 pages × 25).  Dense postcodes (E2 8DP
        # has 60+ properties) need more pages so we don't cut off early.
        all_records: list = []
        seen_keys: set = set()

        for offset in range(0, 100, 25):  # 4 pages × 25 = 100 records max
            page = _api_get({"postcode": postcode, "size": 25, "from": offset})
            new_count = 0
            for rec in page:
                key = rec.get("lmkKey") or rec.get("certificateNumber") or id(rec)
                if key not in seen_keys:
                    seen_keys.add(key)
                    all_records.append(rec)
                    new_count += 1

            if len(page) < 25 or new_count == 0:
                break  # Last page, or API doesn't support 'from' (returning duplicates)

            if address or paon is not None:
                score, _ = _best(all_records)
                if score > 0:
                    break

        if not all_records:
            return {"found": False, "reason": "no_records",
                    "error": f"No EPC records found for postcode {postcode}"}

        if address or paon is not None:
            score, scored = _best(all_records)
            if score == 0:
                return {"found": False, "reason": "no_match",
                        "error": f"No EPC match in {postcode} ({len(all_records)} records searched — "
                                 f"property may use a neighbouring postcode)"}
            # Only return records from the same building (filter out score-0 neighbours)
            matched = [r for r in scored if _score(r) > 0]
            return {"found": True, "records": matched, "postcode_searched": postcode}

        return {"found": True, "records": all_records, "postcode_searched": postcode}

    except Exception as e:
        return {"found": False, "error": str(e)}


def _fetch_epc_detail(cert_number: str, raw_rec: dict | None = None) -> dict:
    """
    Scrape the GOV.UK EPC certificate page for fields stripped from the 2025 API:
    floor area, habitable rooms, property type, built form, heating, current/potential scores.
    Falls back to raw_rec fields (totalFloorArea etc.) when scraping misses them.
    """
    if not _BS4_OK:
        return {"found": False, "error": "bs4 not installed"}
    try:
        url = f"https://find-energy-certificate.service.gov.uk/energy-certificate/{cert_number}"
        r = requests.get(url, timeout=8, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })
        if not r.ok:
            return {"found": False}

        soup = _BS(r.content, "lxml")  # use bytes so lxml reads charset from <meta>

        # Parse all <dl> key-value pairs (building details section)
        dl_data: dict = {}
        for dl in soup.find_all("dl"):
            for dt, dd in zip(dl.find_all("dt"), dl.find_all("dd")):
                k = " ".join(dt.get_text().split()).lower()
                v = " ".join(dd.get_text().split())
                if k and v:
                    dl_data[k] = v

        detail: dict = {"found": True}

        # Floor area
        for k in ("total floor area", "floor area"):
            if k in dl_data:
                raw = dl_data[k]
                m = re.search(r"(\d+(?:\.\d+)?)", raw)
                if m:
                    detail["floor_area_m2"] = float(m.group(1))
                detail["floor_area_raw"] = raw
                break

        # Habitable rooms
        for k in ("number of habitable rooms", "habitable rooms", "number of rooms"):
            if k in dl_data:
                m = re.search(r"(\d+)", dl_data[k])
                if m:
                    detail["habitable_rooms"] = int(m.group(1))
                break

        # Property type (EPC-specific, e.g. "Top-floor flat")
        for k in ("property type", "dwelling type"):
            if k in dl_data:
                detail["property_type_epc"] = dl_data[k]
                break

        # Built form (Detached, Semi-detached, Enclosed mid-terrace, etc.)
        if "built form" in dl_data:
            detail["built_form"] = dl_data["built form"]

        # CO2 emissions from <dl>
        co2_raw = dl_data.get("this property produces")
        if co2_raw:
            m = re.search(r"(\d+(?:\.\d+)?)", co2_raw)
            if m:
                detail["co2_tonnes"] = float(m.group(1))
        co2_pot = dl_data.get("this property’s potential production") or \
                  dl_data.get("this property's potential production")
        if co2_pot:
            m = re.search(r"(\d+(?:\.\d+)?)", co2_pot)
            if m:
                detail["co2_potential_tonnes"] = float(m.group(1))

        # Improvement cost / saving from <dl>
        if "typical installation cost" in dl_data:
            detail["improvement_cost"] = dl_data["typical installation cost"]
        if "typical yearly saving" in dl_data:
            detail["improvement_saving"] = dl_data["typical yearly saving"]

        # Building features table: Feature | Description | Rating
        # Columns are th/td rows; first column names the feature.
        for table in soup.find_all("table"):
            rows = table.find_all("tr")
            if not rows:
                continue
            # Check this looks like a features table (header row with "Feature")
            hdr = [c.get_text(strip=True).lower() for c in rows[0].find_all(["th", "td"])]
            if "feature" not in hdr and "description" not in hdr:
                continue
            for row in rows[1:]:
                cells = [" ".join(c.get_text().split()) for c in row.find_all(["th", "td"])]
                if len(cells) >= 2:
                    feature = cells[0].lower()
                    description = cells[1]
                    if feature == "main heating" and description:
                        detail["heating"] = description
                    elif feature == "hot water" and description:
                        detail["hot_water"] = description
            break  # only need the first features table

        # EPC numeric scores — SVG text elements are rendered as "55 D" (current), "73 C" (potential)
        score_re = re.compile(r"^(\d{1,3})\s+([A-G])\+?$")
        found_scores: list = []
        for svg in soup.find_all("svg"):
            for text_el in svg.find_all("text"):
                txt = " ".join(text_el.get_text().split())
                m = score_re.match(txt)
                if m and 1 <= int(m.group(1)) <= 100:
                    pair = (int(m.group(1)), m.group(2).upper())
                    if pair not in found_scores:
                        found_scores.append(pair)

        if len(found_scores) >= 1:
            detail["current_score"] = found_scores[0][0]
            detail["current_band"]  = found_scores[0][1]
        if len(found_scores) >= 2:
            detail["potential_score"] = found_scores[1][0]
            detail["potential_band"]  = found_scores[1][1]

        # Fallback: fill missing fields from the raw EPC API record when scraping missed them
        if raw_rec and not detail.get("floor_area_m2"):
            raw_area = raw_rec.get("totalFloorArea") or raw_rec.get("total-floor-area")
            if raw_area:
                try:
                    fa = float(str(raw_area).replace(",", "").strip())
                    if fa > 0:
                        detail["floor_area_m2"] = fa
                        detail.setdefault("floor_area_raw", f"{fa} m²")
                except (ValueError, TypeError):
                    pass
        if raw_rec and not detail.get("habitable_rooms"):
            raw_rooms = raw_rec.get("numberHabitableRooms") or raw_rec.get("number-habitable-rooms")
            if raw_rooms:
                try:
                    detail["habitable_rooms"] = int(str(raw_rooms).strip())
                except (ValueError, TypeError):
                    pass

        return detail
    except Exception as e:
        return {"found": False, "error": str(e)}


def _fetch_council_tax(postcode: str) -> dict:
    """Scrape VOA council tax bands for properties at the given postcode."""
    if not _BS4_OK:
        return {"found": False, "error": "bs4 not installed"}
    try:
        session = requests.Session()
        base = "https://www.tax.service.gov.uk/check-council-tax-band"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }

        # Step 1: GET search page to collect session cookies / CSRF token
        r0 = session.get(f"{base}/search", timeout=8, headers=headers)
        if not r0.ok:
            return {"found": False, "error": f"HTTP {r0.status_code}"}

        soup0 = _BS(r0.text, "lxml")
        form = soup0.find("form")

        # Collect form action + any hidden fields (CSRF etc.)
        action = f"{base}/search"
        form_data: dict = {}
        if form:
            act = form.get("action", "")
            if act.startswith("http"):
                action = act
            elif act.startswith("/"):
                action = f"https://www.tax.service.gov.uk{act}"
            for inp in form.find_all("input", type="hidden"):
                nm = inp.get("name")
                if nm:
                    form_data[nm] = inp.get("value", "")
        form_data["postcode"] = postcode.strip().replace(" ", "")

        # Step 2: Submit postcode
        method = (form.get("method", "post") if form else "post").lower()
        if method == "get":
            r1 = session.get(action, params=form_data, timeout=8,
                             headers=headers, allow_redirects=True)
        else:
            r1 = session.post(action, data=form_data, timeout=8,
                              headers=headers, allow_redirects=True)
        if not r1.ok:
            return {"found": False, "error": f"HTTP {r1.status_code}"}

        soup1 = _BS(r1.text, "lxml")

        # Parse council tax bands from results table
        band_re = re.compile(r"^[A-H]$")
        bands = []
        for row in soup1.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
            if len(cells) >= 2:
                for cell in reversed(cells):
                    candidate = cell.strip().upper()
                    if band_re.match(candidate):
                        bands.append({"address": cells[0][:100], "band": candidate})
                        break

        if bands:
            return {"found": True, "properties": bands}
        return {"found": False}
    except Exception as e:
        return {"found": False, "error": str(e)}


def _fetch_sales(postcode: str) -> dict:
    try:
        r = requests.get(
            "https://landregistry.data.gov.uk/data/ppi/transaction-record.json",
            params={
                "propertyAddress.postcode": postcode,
                "_pageSize": 10,
                "_sort": "-transactionDate",
            },
            headers={"Accept": "application/json"},
            timeout=TIMEOUT,
        )
        if not r.ok:
            return {"found": False, "error": f"Land Registry API {r.status_code}"}

        items = (r.json() or {}).get("result", {}).get("items", [])
        sales = []
        for item in items:
            addr = item.get("propertyAddress") or {}

            def _s(v):
                return _lr_val(v) if isinstance(v, dict) else v

            parts = [
                str(_s(addr.get("paon")) or ""),
                str(_s(addr.get("saon")) or ""),
                str(_s(addr.get("street")) or ""),
                str(_s(addr.get("town")) or ""),
            ]
            raw_type   = str(_lr_val(item.get("propertyType")) or "")
            raw_tenure = str(_lr_val(item.get("estateType")) or "")

            # Transaction category: "Standard" vs "Additional" (non-market: auction, right-to-buy, etc.)
            raw_cat = str(_lr_val(item.get("transactionCategory")) or "")
            if "additional" in raw_cat.lower():
                category = "Additional (non-market)"
            elif "standard" in raw_cat.lower():
                category = "Standard"
            else:
                category = raw_cat or None

            sales.append({
                "price":    _s(item.get("pricePaid")),
                "date":     _parse_lr_date(str(item.get("transactionDate") or "")),
                "type":     _PROP_TYPE.get(raw_type, _PROP_TYPE.get(raw_type.lower(), raw_type.title() if raw_type else "")),
                "tenure":   _TENURE.get(raw_tenure, _TENURE.get(raw_tenure.lower(), raw_tenure.title() if raw_tenure else "")),
                "new_build": _s(item.get("newBuild")),
                "address":  ", ".join(p for p in parts if p),
                "category": category,
            })
        return {"found": bool(sales), "sales": sales}
    except Exception as e:
        return {"found": False, "error": str(e)}


def _fetch_planning(lat: float, lng: float) -> dict:
    datasets = {
        "conservation_area":     "conservation-area",
        "listed_building":       "listed-building",
        "tpo":                   "tree-preservation-zone",
        "article4":              "article-4-direction-area",
        "ancient_woodland":      "ancient-woodland",
        "aonb":                  "area-of-outstanding-natural-beauty",
        "green_belt":            "green-belt",
        "sssi":                  "site-of-special-scientific-interest",
        "scheduled_monument":    "scheduled-monument",
        "national_park":         "national-park",
        "world_heritage_site":   "world-heritage-site",
        "registered_park_garden":"park-and-garden",
    }

    def _one(item):
        key, dataset = item
        try:
            r = requests.get(
                "https://www.planning.data.gov.uk/entity.json",
                params={"latitude": lat, "longitude": lng, "dataset": dataset, "limit": 5},
                headers={"Accept": "application/json"},
                timeout=TIMEOUT,
            )
            if r.ok:
                entities = (r.json() or {}).get("entities", [])
                return key, {
                    "found": bool(entities),
                    "count": len(entities),
                    "names": [e.get("name") or e.get("reference", "") for e in entities],
                }
            return key, {"found": False, "error": f"HTTP {r.status_code}"}
        except Exception as e:
            return key, {"found": False, "error": str(e)}

    with ThreadPoolExecutor(max_workers=len(datasets)) as pool:
        return dict(pool.map(_one, datasets.items()))


def _fetch_flood(lat: float, lng: float) -> dict:
    try:
        r = requests.get(
            "https://www.planning.data.gov.uk/entity.json",
            params={"latitude": lat, "longitude": lng, "dataset": "flood-risk-zone", "limit": 5},
            headers={"Accept": "application/json"},
            timeout=TIMEOUT,
        )
        if r.ok:
            entities = (r.json() or {}).get("entities", [])
            if entities:
                max_zone = 1
                for e in entities:
                    ref = e.get("reference", "")
                    if "/" in ref:
                        try:
                            max_zone = max(max_zone, int(ref.split("/")[-1]))
                        except ValueError:
                            pass
                labels = {1: "Low probability", 2: "Medium probability", 3: "High probability"}
                return {"zone": max_zone, "label": labels[max_zone]}
    except Exception:
        pass
    return {"zone": 1, "label": "Low probability"}


def _fetch_crime(lat: float, lng: float) -> dict:

    def _parse(crimes: list) -> tuple[int, list]:
        counts: dict = {}
        for c in crimes:
            cat = c.get("category", "other-crime")
            counts[cat] = counts.get(cat, 0) + 1
        labeled = {_CRIME_LABELS.get(k, k): v for k, v in counts.items()}
        sorted_cats = sorted(labeled.items(), key=lambda x: -x[1])
        return len(crimes), [{"label": lbl, "count": cnt} for lbl, cnt in sorted_cats]

    def _level(total: int) -> str:
        if total < 40:  return "Very Low"
        if total < 80:  return "Low"
        if total < 160: return "Moderate"
        if total < 280: return "High"
        return "Very High"

    try:
        r = requests.get(
            "https://data.police.uk/api/crimes-street/all-crime",
            params={"lat": lat, "lng": lng},
            timeout=TIMEOUT,
        )
        if not r.ok:
            return {"found": False, "error": f"Police API {r.status_code}"}
        crimes = r.json() or []
        month = crimes[0].get("month") if crimes else None
        total, categories = _parse(crimes)

        # Year-over-year: same month last year
        yoy_total = None
        yoy_change = None
        yoy_month = None
        if month:
            try:
                yr, mo = int(month[:4]), int(month[5:7])
                prior_month = f"{yr - 1}-{mo:02d}"
                r2 = requests.get(
                    "https://data.police.uk/api/crimes-street/all-crime",
                    params={"lat": lat, "lng": lng, "date": prior_month},
                    timeout=4,
                )
                if r2.ok:
                    crimes2 = r2.json() or []
                    yoy_total = len(crimes2)
                    yoy_month = prior_month
                    if yoy_total:
                        yoy_change = round((total - yoy_total) / yoy_total * 100)
            except Exception:
                pass

        return {
            "found": bool(crimes),
            "total": total,
            "month": month,
            "categories": categories,
            "level": _level(total),
            "yoy_total": yoy_total,
            "yoy_month": yoy_month,
            "yoy_change": yoy_change,
        }
    except Exception as e:
        return {"found": False, "error": str(e)}


_OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]

def _fetch_nearby(lat: float, lng: float) -> dict:
    """Nearby amenities via OpenStreetMap Overpass API — free, no auth."""
    query = (
        f"[out:json][timeout:12];"
        f"("
        f'nw["amenity"="school"](around:900,{lat},{lng});'
        f'node["railway"~"station|tram_stop|subway_entrance"](around:1200,{lat},{lng});'
        f'nw["amenity"~"supermarket|convenience"](around:700,{lat},{lng});'
        f'nw["amenity"~"doctors|clinic|hospital"](around:1000,{lat},{lng});'
        f'nw["leisure"="park"](around:700,{lat},{lng});'
        f");out tags center;"
    )
    r = None
    for endpoint in _OVERPASS_ENDPOINTS:
        try:
            r = requests.get(endpoint, params={"data": query}, timeout=8)
            if r.ok:
                break
        except Exception:
            pass
    try:
        if r is None or not r.ok:
            return {"found": False, "error": f"Overpass {r.status_code if r else 'no response'}"}

        elements = (r.json() or {}).get("elements", [])

        buckets: dict = {"schools": [], "transport": [], "shops": [], "health": [], "parks": []}

        for e in elements:
            tags = e.get("tags") or {}
            name = tags.get("name") or tags.get("operator") or tags.get("brand") or ""
            if not name:
                continue

            # Coordinates — nodes have lat/lon directly; ways have center
            elat = e.get("lat") or (e.get("center") or {}).get("lat") or lat
            elng = e.get("lon") or (e.get("center") or {}).get("lon") or lng
            dist = round(_haversine_m(lat, lng, elat, elng))

            amenity = tags.get("amenity", "")
            railway = tags.get("railway", "")
            leisure = tags.get("leisure", "")

            if amenity == "school":
                buckets["schools"].append({"name": name, "dist_m": dist})
            elif railway in ("station", "tram_stop", "subway_entrance"):
                buckets["transport"].append({"name": name, "dist_m": dist})
            elif amenity in ("supermarket", "convenience"):
                buckets["shops"].append({"name": name, "dist_m": dist})
            elif amenity in ("doctors", "clinic", "hospital"):
                buckets["health"].append({"name": name, "dist_m": dist})
            elif leisure == "park":
                buckets["parks"].append({"name": name, "dist_m": dist})

        # Sort by distance, deduplicate by name, keep top 4
        for key in buckets:
            seen = set()
            deduped = []
            for item in sorted(buckets[key], key=lambda x: x["dist_m"]):
                if item["name"] not in seen:
                    seen.add(item["name"])
                    deduped.append(item)
            buckets[key] = deduped[:4]

        return {"found": True, **buckets}

    except Exception as e:
        return {"found": False, "error": str(e)}


@property_data_bp.route("/api/epc-detail")
def get_epc_detail():
    cert = request.args.get("cert", "").strip()
    if not cert:
        return jsonify({"error": "cert param required"}), 400
    return jsonify(_fetch_epc_detail(cert))


@property_data_bp.route("/api/property-data")
def get_property_data():
    try:
        lat = float(request.args["lat"])
        lng = float(request.args["lng"])
    except (KeyError, ValueError):
        return jsonify({"error": "lat and lng query params required"}), 400


    # Check in-memory cache.
    # Key = rounded coords + address hint hash so two different properties
    # that are close together (within 110 m) don't share the same EPC match.
    _addr_hint_raw = request.args.get("address", "").strip().lower()
    _addr_hash = hashlib.md5(_addr_hint_raw.encode()).hexdigest()[:8]
    _cache_key = f"{round(lat, 3)},{round(lng, 3)},{_addr_hash}"
    _now = time.time()
    _cached = _PROP_DATA_CACHE.get(_cache_key)
    if _cached and (_now - _cached[1]) < _PROP_DATA_TTL:
        return jsonify(_cached[0])

    # Optional address hint — used to rank EPC records so the correct property surfaces first
    address_hint = request.args.get("address", "").strip()

    loc = _postcode_from_latlng(lat, lng)
    postcode = (loc or {}).get("postcode")

    # Mapbox geocoded labels include the address-level postcode (e.g. "145 Lampton Road, TW3 4EB").
    # postcodes.io returns the centroid postcode of clicked coordinates which can be a different
    # unit postcode (e.g. TW3 4EA). Prefer the postcode extracted from the address hint for EPC
    # lookups since it matches what the EPC register actually indexed the property under.
    _addr_pc = re.search(r'\b([A-Z]{1,2}\d{1,2}[A-Z]?\s*\d[A-Z]{2})\b', address_hint, re.IGNORECASE)
    addr_postcode = _addr_pc.group(1).upper().replace(" ", "") if _addr_pc else None
    if addr_postcode:
        # Normalise to "SW1A 1AA" format (insert space before final 3 chars)
        addr_postcode = addr_postcode[:-3] + " " + addr_postcode[-3:]
    epc_postcode = addr_postcode if addr_postcode else postcode
    if addr_postcode and addr_postcode != postcode:
        pass

    def safe(future, label, timeout=20):
        try:
            result = future.result(timeout=timeout)
            return result
        except Exception as e:
            return {"error": str(e)}

    no_pc = {"found": False, "error": "Could not determine postcode"}

    with ThreadPoolExecutor(max_workers=9) as pool:
        f_planning    = pool.submit(_fetch_planning, lat, lng)
        f_flood       = pool.submit(_fetch_flood, lat, lng)
        f_crime       = pool.submit(_fetch_crime, lat, lng)
        f_nearby      = pool.submit(_fetch_nearby, lat, lng)
        f_epc         = pool.submit(_fetch_epc, epc_postcode, address_hint) if epc_postcode else None
        f_sales       = pool.submit(_fetch_sales, postcode)       if postcode else None
        f_council_tax = pool.submit(_fetch_council_tax, postcode) if postcode else None

        # Resolve EPC first, then kick off detail scrape in parallel with remaining tasks
        epc_result = safe(f_epc, "epc") if f_epc else no_pc

        # Centroid-postcode fallback: if the address-level postcode search found nothing
        # (no records OR no address match) and the centroid postcode differs, try it.
        # This catches properties where the EPC is indexed under an adjacent unit postcode
        # — common on street corners and postcode boundaries.
        _addr_pc_norm = (addr_postcode or "").replace(" ", "").upper()
        _cent_pc_norm  = (postcode or "").replace(" ", "").upper()
        if (not epc_result.get("found") and
                _addr_pc_norm and _cent_pc_norm and _addr_pc_norm != _cent_pc_norm):
            epc_result = _fetch_epc(postcode, address_hint)

        cert_num = None
        epc_raw_rec = None
        if epc_result.get("found") and epc_result.get("records"):
            epc_raw_rec = epc_result["records"][0]
            cert_num = epc_raw_rec.get("certificateNumber") or epc_raw_rec.get("lmkKey")
        f_epc_detail = pool.submit(_fetch_epc_detail, cert_num, epc_raw_rec) if cert_num else None

        if f_epc_detail:
            epc_result["detail"] = safe(f_epc_detail, "epc_detail", timeout=15)

        crime_result = safe(f_crime, "crime")

        # Weight crime against estimated local population so users see crimes per capita
        # rather than a raw count.  Population density is derived from the ONS rural/urban
        # classification returned by postcodes.io; 1-mile search radius ≈ 8.14 km².
        _DENSITY_PER_KM2 = {
            "Urban major conurbation":                    5000,
            "Urban minor conurbation":                    3000,
            "Urban city and town":                        1800,
            "Urban city and town in a sparse setting":     800,
            "Rural town and fringe":                       350,
            "Rural town and fringe in a sparse setting":   120,
            "Rural village and dispersed":                  60,
            "Rural village and dispersed in a sparse setting": 25,
        }
        _SEARCH_AREA_KM2 = 8.14  # π × 1.609²
        _UK_RATE = 6.1           # England & Wales: ~4.4M crimes/yr / 60M pop / 12 months × 1000

        if isinstance(crime_result, dict) and crime_result.get("found"):
            rural_urban = (loc or {}).get("rural_urban") or ""
            density = _DENSITY_PER_KM2.get(rural_urban, 1800)
            pop_estimate = int(density * _SEARCH_AREA_KM2)
            total = crime_result.get("total", 0)
            rate = round(total / pop_estimate * 1000, 1) if pop_estimate else None

            def _rate_level(r: float) -> str:
                if r < 3:   return "Very Low"
                if r < 6:   return "Low"
                if r < 12:  return "Moderate"
                if r < 20:  return "High"
                return "Very High"

            crime_result["pop_estimate"]    = pop_estimate
            crime_result["rate_per_1000"]   = rate
            crime_result["uk_rate_per_1000"] = _UK_RATE
            if rate is not None:
                crime_result["level"] = _rate_level(rate)

        result = {
            "postcode":    postcode,
            "location":    loc,
            "epc":         epc_result,
            "sales":       safe(f_sales,       "sales")    if f_sales       else no_pc,
            "planning":    safe(f_planning,    "planning"),
            "flood":       safe(f_flood,       "flood"),
            "crime":       crime_result,
            "nearby":      safe(f_nearby,      "nearby"),
            "council_tax": safe(f_council_tax, "ctax")     if f_council_tax else {"found": False},
        }

        # Store in cache; evict oldest 20 entries when cap exceeded
        if len(_PROP_DATA_CACHE) >= 200:
            oldest = sorted(_PROP_DATA_CACHE, key=lambda k: _PROP_DATA_CACHE[k][1])[:20]
            for k in oldest:
                del _PROP_DATA_CACHE[k]
        _PROP_DATA_CACHE[_cache_key] = (result, time.time())

        return jsonify(result)


# ---------------------------------------------------------------------------
# Street-level sales drill-down
# ---------------------------------------------------------------------------

def _parse_street_and_district(address: str, postcode: str) -> tuple:
    """
    Extract (street_name_upper, town_upper, district_upper) from a full
    address string and postcode.

    e.g. "7 Westcott Road, Southwark, London, SE17 3QY"
         postcode "SE17 3QY"
    →   ("WESTCOTT ROAD", "SOUTHWARK", "SE17")
    """
    # --- postcode district ---
    pc_clean = re.sub(r"\s+", "", (postcode or "")).upper()
    district = re.match(r"^([A-Z]{1,2}\d{1,2}[A-Z]?)", pc_clean)
    district = district.group(1) if district else ""

    # --- street name: strip leading house number from first comma-part ---
    first_part = (address.split(",")[0] if "," in address else address).strip()
    street = re.sub(r"^\d+[a-zA-Z]?\s+", "", first_part).strip()  # remove "7 " or "14A "
    # handle "Flat 3, 7 Kings Road" → first part is "Flat 3" — try second part
    if len(street.split()) <= 1 and "," in address:
        second_part = address.split(",")[1].strip()
        street = re.sub(r"^\d+[a-zA-Z]?\s+", "", second_part).strip()

    # --- town: second non-empty comma-part that isn't a postcode / country ---
    parts = [p.strip() for p in address.split(",")]
    town = ""
    skip = {"united kingdom", "england", "wales", "scotland"}
    pc_re = re.compile(r"^[A-Z]{1,2}\d", re.IGNORECASE)
    for p in parts[1:]:
        if not p or p.lower() in skip or pc_re.match(p):
            continue
        town = p
        break

    return street.upper(), town.upper(), district.upper()


def _fetch_street_sales(street: str, town: str = "", district: str = "") -> dict:
    """
    Fetch all Land Registry sales for a given street name, then batch-match
    EPC records (by postcode) to append floor_area_m2 and price_per_m2.
    """

    # ── Step 1: Query Land Registry ───────────────────────────────────────
    params: dict = {
        "propertyAddress.street": street,
        "_pageSize": 100,
        "_sort": "-transactionDate",
        "_page": 0,
    }
    if town:
        params["propertyAddress.town"] = town

    all_items: list = []
    for page_num in range(3):          # max 300 results
        params["_page"] = page_num
        try:
            r = requests.get(
                "https://landregistry.data.gov.uk/data/ppi/transaction-record.json",
                params=params,
                headers={"Accept": "application/json"},
                timeout=TIMEOUT,
            )
            if not r.ok:
                break
            items = (r.json() or {}).get("result", {}).get("items", [])
            all_items.extend(items)
            if len(items) < 100:
                break
        except Exception as exc:
            break

    if not all_items:
        # Try without town filter if we got nothing (LR data can have different town names)
        if town:
            params_no_town = {k: v for k, v in params.items()
                              if k != "propertyAddress.town"}
            params_no_town["_page"] = 0
            try:
                r = requests.get(
                    "https://landregistry.data.gov.uk/data/ppi/transaction-record.json",
                    params=params_no_town,
                    headers={"Accept": "application/json"},
                    timeout=TIMEOUT,
                )
                if r.ok:
                    all_items = (r.json() or {}).get("result", {}).get("items", [])
            except Exception:
                pass

    if not all_items:
        return {"found": False, "sales": [], "stats": {}, "street": street,
                "error": "No sales found for this street in Land Registry"}

    # Post-filter by postcode district to avoid results from streets with the
    # same name in other cities (e.g. there are many "Victoria Road"s in England)
    if district:
        filtered = []
        for item in all_items:
            addr = item.get("propertyAddress") or {}
            def _s(v):
                return _lr_val(v) if isinstance(v, dict) else v
            pc = str(_s(addr.get("postcode")) or "").replace(" ", "").upper()
            if pc.startswith(district.upper()):
                filtered.append(item)
        if filtered:
            all_items = filtered

    # ── Step 2: Parse into sale dicts ─────────────────────────────────────
    sales: list = []
    for item in all_items:
        addr = item.get("propertyAddress") or {}

        def _s(v):
            return _lr_val(v) if isinstance(v, dict) else v

        paon     = str(_s(addr.get("paon"))   or "")
        saon     = str(_s(addr.get("saon"))   or "")
        street_n = str(_s(addr.get("street")) or "")
        town_n   = str(_s(addr.get("town"))   or "")
        postcode = str(_s(addr.get("postcode")) or "")
        raw_type   = str(_lr_val(item.get("propertyType")) or "")
        raw_tenure = str(_lr_val(item.get("estateType"))   or "")
        raw_cat    = str(_lr_val(item.get("transactionCategory")) or "")

        # saon first so "FLAT F, 6, WESTCOTT ROAD" matches EPC format
        parts = [p for p in [saon, paon, street_n, town_n] if p]
        full_address = ", ".join(parts)

        sales.append({
            "price":         _s(item.get("pricePaid")),
            "date":          _parse_lr_date(str(item.get("transactionDate") or "")),
            "type":          _PROP_TYPE.get(raw_type, raw_type.title() if raw_type else ""),
            "tenure":        _TENURE.get(raw_tenure, raw_tenure.title() if raw_tenure else ""),
            "new_build":     _s(item.get("newBuild")),
            "category":      "Additional" if "additional" in raw_cat.lower() else "Standard",
            "address":       full_address,
            "paon":          paon,
            "saon":          saon,
            "postcode":      postcode,
            "floor_area_m2": None,
            "price_per_m2":  None,
        })

    # ── Step 3: Batch EPC lookup per unique postcode ──────────────────────
    unique_postcodes = {s["postcode"] for s in sales if s["postcode"]}

    def _epc_for_postcode(pc: str) -> tuple:
        try:
            result = _fetch_epc(pc, address="")
            return pc, result.get("records", []) if result.get("found") else []
        except Exception:
            return pc, []

    epc_by_postcode: dict = {}
    if unique_postcodes:
        with ThreadPoolExecutor(max_workers=min(8, len(unique_postcodes))) as pool:
            for pc, records in pool.map(_epc_for_postcode, unique_postcodes):
                epc_by_postcode[pc] = records

    # ── Step 4: Match each sale to its best EPC cert number ──────────────
    cert_for_sale: list = [None] * len(sales)
    epc_addr_for_sale: list = [""] * len(sales)
    raw_rec_for_cert: dict = {}   # cert → raw EPC API record (fallback for floor area)
    for idx, sale in enumerate(sales):
        pc = sale.get("postcode", "")
        records = epc_by_postcode.get(pc, [])
        if not records:
            continue
        best_score, best_cert, best_rec_obj = 0, None, None
        for rec in records:
            sc = _epc_match_score(sale["address"], rec,
                                  paon=sale["paon"], saon=sale["saon"])
            if sc > best_score:
                best_score = sc
                best_cert = rec.get("certificateNumber") or rec.get("lmkKey")
                best_rec_obj = rec
        if best_cert and best_score > 0:
            cert_for_sale[idx] = best_cert
            raw_rec_for_cert[best_cert] = best_rec_obj
            # Build readable EPC address for the hover tooltip
            epc_addr_for_sale[idx] = ", ".join(
                v for k in ("addressLine1", "addressLine2", "addressLine3", "postcode")
                if (v := str(best_rec_obj.get(k) or "").strip())
            )

    # ── Step 5: Batch-scrape EPC detail pages for unique matched certs ────
    unique_certs = {c for c in cert_for_sale if c}
    cert_detail: dict = {}
    if unique_certs:
        def _get_detail(cert):
            return cert, _fetch_epc_detail(cert, raw_rec_for_cert.get(cert))
        with ThreadPoolExecutor(max_workers=min(5, len(unique_certs))) as pool:
            for cert, detail in pool.map(_get_detail, unique_certs):
                cert_detail[cert] = detail

    # ── Step 6: Apply floor area + EPC metadata to each sale ─────────────
    for idx, sale in enumerate(sales):
        cert = cert_for_sale[idx]
        if not cert:
            continue
        detail = cert_detail.get(cert, {})
        area = detail.get("floor_area_m2")
        if area and area > 0:
            sale["floor_area_m2"] = area
            price = sale.get("price")
            if isinstance(price, (int, float)):
                sale["price_per_m2"] = round(price / area)
        if detail.get("found"):
            sale["epc_cert"]           = cert
            sale["epc_address"]        = epc_addr_for_sale[idx]
            sale["epc_floor_area_raw"] = detail.get("floor_area_raw")
            sale["epc_property_type"]  = detail.get("property_type_epc")
            sale["epc_built_form"]     = detail.get("built_form")
            sale["epc_habitable_rooms"] = detail.get("habitable_rooms")

    # ── Step 7: Compute statistics ────────────────────────────────────────
    prices     = [s["price"] for s in sales if isinstance(s.get("price"), (int, float))]
    sqm_sales  = [s for s in sales if s.get("price_per_m2")]
    sqm_values = [s["price_per_m2"] for s in sqm_sales]

    # Rolling average: chronologically last 8 sales that have area data
    recent_sqm = sorted(sqm_sales, key=lambda s: s.get("date", ""))[-8:]
    rolling_avg_sqm = (
        round(sum(s["price_per_m2"] for s in recent_sqm) / len(recent_sqm))
        if recent_sqm else None
    )

    median_price = None
    if prices:
        sp = sorted(prices)
        n = len(sp)
        median_price = sp[n // 2] if n % 2 else (sp[n // 2 - 1] + sp[n // 2]) // 2

    avg_sqm = round(sum(sqm_values) / len(sqm_values)) if sqm_values else None

    # Distinct paon+saon combos = unique properties
    unique_props = len({(s["paon"], s["saon"]) for s in sales})

    stats = {
        "total_sales":    len(sales),
        "unique_props":   unique_props,
        "median_price":   median_price,
        "avg_sqm":        avg_sqm,
        "rolling_avg_sqm": rolling_avg_sqm,
        "sqm_coverage":   len(sqm_sales),
        "sqm_missing":    len(sales) - len(sqm_sales),
    }

    return {
        "found":  True,
        "street": street,
        "stats":  stats,
        "sales":  sales,
    }


def _street_from_postcode(postcode: str) -> tuple:
    """
    Derive (street, town, district) by fetching a small LR sample for the postcode
    and picking the dominant street/town from the structured address fields.
    Falls back to ("", "", district) when the LR query returns nothing.
    """
    from collections import Counter
    pc_clean = re.sub(r"\s+", "", (postcode or "").upper())
    district_m = re.match(r"^([A-Z]{1,2}\d{1,2}[A-Z]?)", pc_clean)
    district = district_m.group(1) if district_m else ""
    try:
        r = requests.get(
            "https://landregistry.data.gov.uk/data/ppi/transaction-record.json",
            params={"propertyAddress.postcode": postcode, "_pageSize": 20, "_page": 0},
            timeout=TIMEOUT,
        )
        items = r.json().get("result", {}).get("items", [])
    except Exception:
        return "", "", district

    sc, tc = Counter(), Counter()
    for item in items:
        addr = item.get("propertyAddress", {})
        if addr.get("street"):
            sc[addr["street"].upper()] += 1
        if addr.get("town"):
            tc[addr["town"].upper()] += 1

    street = sc.most_common(1)[0][0] if sc else ""
    town   = tc.most_common(1)[0][0] if tc else ""
    return street, town, district


@property_data_bp.route("/api/street-sales")
def get_street_sales():
    """
    GET /api/street-sales?address=<full_address>&postcode=<postcode>
    Accepts address+postcode (existing flow) or postcode alone (Zoopla / no-address flow).
    """
    address  = request.args.get("address",  "").strip()
    postcode = request.args.get("postcode", "").strip()

    if address:
        street, town, district = _parse_street_and_district(address, postcode)
    elif postcode:
        street, town, district = _street_from_postcode(postcode)
    else:
        return jsonify({"error": "address or postcode param required"}), 400

    if not street:
        return jsonify({"found": False, "error": "Could not determine street from postcode"}), 200

    result = _fetch_street_sales(street, town, district)
    return jsonify(result)
