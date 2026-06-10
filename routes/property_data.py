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


def _fetch_epc(postcode: str) -> dict:
    print(f"  [epc] fetching postcode={postcode!r}")
    if not EPC_BEARER:
        return {"found": False, "error": "EPC_BEARER_TOKEN not configured in .env"}
    try:
        r = requests.get(
            "https://api.get-energy-performance-data.communities.gov.uk/api/domestic/search",
            params={"postcode": postcode, "size": 5},
            headers={"Accept": "application/json", "Authorization": f"Bearer {EPC_BEARER}"},
            timeout=TIMEOUT,
        )
        print(f"  [epc] status={r.status_code}")
        if not r.ok:
            print(f"  [epc] error body={r.text[:200]!r}")
            return {"found": False, "error": f"EPC API {r.status_code}"}
        records = (r.json() or {}).get("data", [])
        print(f"  [epc] records={len(records)}")
        if not records:
            return {"found": False}
        return {"found": True, "records": records}
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
        counts: dict = {}
        for c in crimes:
            cat = c.get("category", "other-crime")
            counts[cat] = counts.get(cat, 0) + 1
        labeled = {_CRIME_LABELS.get(k, k): v for k, v in counts.items()}
        sorted_cats = sorted(labeled.items(), key=lambda x: -x[1])
        month = crimes[0].get("month") if crimes else None
        return {
            "found": bool(crimes),
            "total": len(crimes),
            "month": month,
            "categories": [{"label": lbl, "count": cnt} for lbl, cnt in sorted_cats],
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

    loc = _postcode_from_latlng(lat, lng)
    postcode = (loc or {}).get("postcode")
    print(f"  postcode resolved: {postcode!r}  region={( loc or {}).get('region')!r}")

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
        f_epc         = pool.submit(_fetch_epc, postcode)         if postcode else None
        f_sales       = pool.submit(_fetch_sales, postcode)       if postcode else None
        f_council_tax = pool.submit(_fetch_council_tax, postcode) if postcode else None

        # Resolve EPC first, then kick off detail scrape in parallel with remaining tasks
        epc_result = safe(f_epc, "epc") if f_epc else no_pc
        cert_num = None
        if epc_result.get("found") and epc_result.get("records"):
            cert_num = epc_result["records"][0].get("certificateNumber")
        f_epc_detail = pool.submit(_fetch_epc_detail, cert_num) if cert_num else None

        if f_epc_detail:
            epc_result["detail"] = safe(f_epc_detail, "epc_detail", timeout=15)

        result = {
            "postcode":    postcode,
            "location":    loc,
            "epc":         epc_result,
            "sales":       safe(f_sales,       "sales")    if f_sales       else no_pc,
            "planning":    safe(f_planning,    "planning"),
            "flood":       safe(f_flood,       "flood"),
            "crime":       safe(f_crime,       "crime"),
            "nearby":      safe(f_nearby,      "nearby"),
            "council_tax": safe(f_council_tax, "ctax")     if f_council_tax else {"found": False},
        }
        print(f"  returning response for postcode={postcode!r}")
        return jsonify(result)
