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

import logging
import math
import os
import re
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
TIMEOUT = 8

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
        print(f"  [postcodes.io] status={r.status_code}")
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
    except Exception as e:
        print(f"  [postcodes.io] EXCEPTION: {e}")
    return None


def _epc_match_score(known_address: str, record: dict) -> int:
    """
    Score 0–100 for how well an EPC record matches the known address.
    Returns 0 for definite mismatches (different building numbers).
    Used to rank multiple records in the same postcode.
    """
    def _tokens(s: str) -> list:
        return re.sub(r"[^a-z0-9\s]", "", s.lower()).split()

    def _building_num(tokens: list):
        """
        Return the likely building number as int, or None.
        Uses max() so "Flat 3, 145 Lampton Rd" → 145 not 3.
        Handles alphanumeric house numbers ("143a", "12b") but skips postcode
        inward codes which share the same pattern: exactly 1 digit + 2 letters
        (e.g. "4fa" from "TW3 4FA", "3ea" from "SW1 3EA").
        """
        nums = []
        for t in tokens[:8]:
            if re.match(r"^\d{1,5}$", t):
                nums.append(int(t))
            else:
                m = re.match(r"^(\d{1,5})[a-z]{1,2}$", t)
                if m:
                    digits_part = m.group(1)
                    suffix_part = t[len(digits_part):]
                    # Skip postcode inward codes: exactly 1 digit + 2 letters
                    if len(digits_part) == 1 and len(suffix_part) == 2:
                        continue
                    nums.append(int(digits_part))
        return max(nums) if nums else None

    known = _tokens(known_address)
    epc_raw = " ".join(
        str(record.get(k) or "")
        for k in ("addressLine1", "addressLine2", "address1", "address2")
    )
    epc = _tokens(epc_raw)
    if not known or not epc:
        return 0

    knum = _building_num(known)
    enum = _building_num(epc)

    # Hard exclusion: both sides have a building number and they differ.
    # This catches "143a vs 145" because 143 ≠ 145.
    if knum is not None and enum is not None and knum != enum:
        return 0

    # Distinctive word overlap (skip generic UK address vocabulary)
    _SKIP = {
        "flat", "floor", "ground", "first", "second", "third",
        "road", "street", "avenue", "close", "drive", "lane", "way",
        "place", "grove", "gardens", "court", "house", "and", "the",
    }
    kwords = {t for t in known if len(t) > 2 and not re.match(r"^\d", t) and t not in _SKIP}
    ewords = {t for t in epc   if len(t) > 2 and not re.match(r"^\d", t) and t not in _SKIP}
    overlap = kwords & ewords

    score = 0
    if knum is not None and enum is not None and knum == enum:
        # Building numbers match — award number match + distinctive word overlap
        score += 40
        if overlap:
            score += min(len(overlap) * 25, 60)
    elif knum is None and overlap:
        # Searched address has no house number — word overlap only (best we can do)
        score += min(len(overlap) * 25, 60)
    # When knum is known but enum is None or differs, score stays 0.
    # This prevents "LAMPTON COURT" (no number) scoring 25 against "145 Lampton Road".
    return min(score, 100) if score else 0


def _fetch_epc(postcode: str, address: str = "") -> dict:
    print(f"  [epc] fetching postcode={postcode!r}  address={address!r}")
    if not EPC_BEARER:
        return {"found": False, "error": "EPC_BEARER_TOKEN not configured in .env"}

    headers = {"Accept": "application/json", "Authorization": f"Bearer {EPC_BEARER}"}
    api_url = "https://api.get-energy-performance-data.communities.gov.uk/api/domestic/search"

    def _api_get(params: dict) -> list:
        try:
            r = requests.get(api_url, params=params, headers=headers, timeout=TIMEOUT)
            print(f"  [epc] GET {params} → {r.status_code}")
            if r.ok:
                data = (r.json() or {}).get("data", [])
                # API returns {"data": {"error": "..."}} (a dict, not a list) when no
                # certificates match.  Guard here so callers always receive a list.
                return data if isinstance(data, list) else []
            print(f"  [epc] error body={r.text[:200]!r}")
        except Exception as exc:
            print(f"  [epc] request error: {exc}")
        return []

    def _best(records: list) -> tuple:
        """Return (best_score, sorted_records)."""
        if not records or not address:
            return 0, records
        srt = sorted(records, key=lambda rec: _epc_match_score(address, rec), reverse=True)
        return _epc_match_score(address, srt[0]), srt

    try:
        # ── Stage 1: targeted address+postcode search ─────────────────────────
        # The EPC API's 'address' param does a free-text search within the postcode.
        # This is precise and unaffected by postcode size — it doesn't rely on a
        # fixed page window, so a postcode with 200 records is not a problem.
        if address:
            street_part = address.split(",")[0].strip()  # "2 Bisley Place, Hounslow…" → "2 Bisley Place"
            targeted = _api_get({"postcode": postcode, "address": street_part, "size": 5})
            if targeted:
                score, scored = _best(targeted)
                print(f"  [epc] targeted: score={score}  → {scored[0].get('addressLine1')!r}")
                if score > 0:
                    matched = [r for r in scored if _epc_match_score(address, r) > 0]
                    return {"found": True, "records": matched}
            print(f"  [epc] targeted gave no match — falling back to paginated postcode search")

        # ── Stage 2: paginated postcode search ───────────────────────────────
        # Fetches up to 100 records (4 pages of 25), stopping early once a match
        # is confirmed. Deduplication by lmkKey handles APIs that ignore 'from'.
        all_records: list = []
        seen_keys: set = set()

        for offset in range(0, 100, 25):
            page = _api_get({"postcode": postcode, "size": 25, "from": offset})
            new_count = 0
            for rec in page:
                key = rec.get("lmkKey") or rec.get("certificateNumber") or id(rec)
                if key not in seen_keys:
                    seen_keys.add(key)
                    all_records.append(rec)
                    new_count += 1

            print(f"  [epc] page from={offset}: {new_count} new (total unique={len(all_records)})")

            if len(page) < 25 or new_count == 0:
                break  # Last page, or API doesn't support 'from' (returning duplicates)

            if address:
                score, _ = _best(all_records)
                if score > 0:
                    print(f"  [epc] good match found at offset {offset} — stopping pagination")
                    break

        print(f"  [epc] postcode search done: {len(all_records)} unique records")
        if not all_records:
            return {"found": False, "reason": "no_records",
                    "error": f"No EPC records found for postcode {postcode}"}

        if address:
            score, scored = _best(all_records)
            print(f"  [epc] final: score={score}  → {scored[0].get('addressLine1')!r}")
            if score == 0:
                return {"found": False, "reason": "no_match",
                        "error": f"No EPC match in {postcode} ({len(all_records)} records searched — "
                                 f"property may use a neighbouring postcode)"}
            # Only return records from the same building (filter out score-0 neighbours)
            matched = [r for r in scored if _epc_match_score(address, r) > 0]
            return {"found": True, "records": matched, "postcode_searched": postcode}

        return {"found": True, "records": all_records, "postcode_searched": postcode}

    except Exception as e:
        print(f"  [epc] EXCEPTION: {e}")
        return {"found": False, "error": str(e)}


def _fetch_epc_detail(cert_number: str) -> dict:
    """
    Scrape the GOV.UK EPC certificate page for fields stripped from the 2025 API:
    floor area, habitable rooms, property type, built form, heating, current/potential scores.
    """
    if not _BS4_OK:
        return {"found": False, "error": "bs4 not installed"}
    try:
        url = f"https://find-energy-certificate.service.gov.uk/energy-certificate/{cert_number}"
        r = requests.get(url, timeout=12, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        })
        print(f"  [epc_detail] status={r.status_code}  cert={cert_number[:14]}")
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

        return detail
    except Exception as e:
        print(f"  [epc_detail] EXCEPTION: {e}")
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
        print(f"  [ctax] GET /search status={r0.status_code}")
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
        print(f"  [ctax] {method.upper()} results status={r1.status_code}  url={r1.url[:80]}")
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
        print(f"  [ctax] EXCEPTION: {e}")
        return {"found": False, "error": str(e)}


def _fetch_sales(postcode: str) -> dict:
    print(f"  [sales] fetching postcode={postcode!r}")
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
        print(f"  [sales] status={r.status_code}")
        if not r.ok:
            return {"found": False, "error": f"Land Registry API {r.status_code}"}

        items = (r.json() or {}).get("result", {}).get("items", [])
        print(f"  [sales] items found={len(items)}")
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
            print(f"  [planning/{key}] status={r.status_code}")
            if r.ok:
                entities = (r.json() or {}).get("entities", [])
                return key, {
                    "found": bool(entities),
                    "count": len(entities),
                    "names": [e.get("name") or e.get("reference", "") for e in entities],
                }
            print(f"  [planning/{key}] body={r.text[:120]!r}")
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
        print(f"  [flood] status={r.status_code}")
        if r.ok:
            entities = (r.json() or {}).get("entities", [])
            print(f"  [flood] entities={len(entities)}")
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
    except Exception as e:
        print(f"  [flood] EXCEPTION: {e}")
    return {"zone": 1, "label": "Low probability"}


def _fetch_crime(lat: float, lng: float) -> dict:
    print(f"  [crime] fetching lat={lat}  lng={lng}")

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
        print(f"  [crime] status={r.status_code}")
        if not r.ok:
            return {"found": False, "error": f"Police API {r.status_code}"}
        crimes = r.json() or []
        print(f"  [crime] total={len(crimes)}")
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
                    timeout=TIMEOUT,
                )
                if r2.ok:
                    crimes2 = r2.json() or []
                    yoy_total = len(crimes2)
                    yoy_month = prior_month
                    if yoy_total:
                        yoy_change = round((total - yoy_total) / yoy_total * 100)
                    print(f"  [crime] yoy={yoy_total} ({prior_month})")
            except Exception as exc:
                print(f"  [crime] yoy error: {exc}")

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
        print(f"  [crime] EXCEPTION: {e}")
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
            r = requests.get(endpoint, params={"data": query}, timeout=14)
            print(f"  [nearby] {endpoint.split('/')[2]} status={r.status_code}")
            if r.ok:
                break
        except Exception:
            pass
    try:
        if r is None or not r.ok:
            return {"found": False, "error": f"Overpass {r.status_code if r else 'no response'}"}

        elements = (r.json() or {}).get("elements", [])
        print(f"  [nearby] elements={len(elements)}")

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
        print(f"  [nearby] EXCEPTION: {e}")
        return {"found": False, "error": str(e)}


@property_data_bp.route("/api/epc-detail")
def get_epc_detail():
    cert = request.args.get("cert", "").strip()
    if not cert:
        return jsonify({"error": "cert param required"}), 400
    return jsonify(_fetch_epc_detail(cert))


@property_data_bp.route("/api/property-data")
def get_property_data():
    print("\n=== /api/property-data HIT ===")
    try:
        lat = float(request.args["lat"])
        lng = float(request.args["lng"])
    except (KeyError, ValueError):
        print("  ERROR: missing lat/lng params")
        return jsonify({"error": "lat and lng query params required"}), 400

    print(f"  lat={lat}  lng={lng}")

    # Optional address hint — used to rank EPC records so the correct property surfaces first
    address_hint = request.args.get("address", "").strip()

    loc = _postcode_from_latlng(lat, lng)
    postcode = (loc or {}).get("postcode")
    print(f"  postcode resolved: {postcode!r}  region={(loc or {}).get('region')!r}  address_hint={address_hint!r}")

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
        print(f"  address_hint postcode={addr_postcode!r} overrides centroid={postcode!r} for EPC lookup")

    def safe(future, label, timeout=20):
        try:
            result = future.result(timeout=timeout)
            print(f"  [{label}] OK — {str(result)[:120]}")
            return result
        except Exception as e:
            print(f"  [{label}] EXCEPTION: {e}")
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
            print(f"  [epc] no match with addr_postcode={addr_postcode!r} — trying centroid={postcode!r}")
            epc_result = _fetch_epc(postcode, address_hint)

        cert_num = None
        if epc_result.get("found") and epc_result.get("records"):
            cert_num = epc_result["records"][0].get("certificateNumber")
        f_epc_detail = pool.submit(_fetch_epc_detail, cert_num) if cert_num else None

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
        print(f"  returning response for postcode={postcode!r}")
        return jsonify(result)
