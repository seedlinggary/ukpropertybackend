"""
GET /api/postcode-analysis?postcode=SW1X9LQ
  → JSON with LR data + EPC for in-postcode properties + ownership. ~8–12 s.

GET /api/postcode-analysis/epc-stream?postcode=SW1X9LQ
  → SSE: user-triggered EPC for ALL properties (full street). ~60–90 s.
     type=start    {total}
     type=epc      {paon, saon, updates}
     type=complete {median_sqm, investment_updates}
     type=error    {error}

DB access: single sequential query on company_ownership.
"""
import json
import logging
import re
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from flask import Blueprint, Response, jsonify, request, stream_with_context
import requests
from sqlalchemy import text
from database import engine

try:
    from curl_cffi import requests as _cffi_requests
    from bs4 import BeautifulSoup as _BeautifulSoup
    _RM_AVAILABLE = True
except ImportError:
    _RM_AVAILABLE = False
from routes.property_data import (
    _parse_lr_date, _PROP_TYPE, _TENURE,
    _fetch_epc, _fetch_epc_detail,
)
from middleware.auth import require_auth

from middleware.tracking import track_feature

logger = logging.getLogger(__name__)
postcode_analysis_bp = Blueprint("postcode_analysis", __name__)

_PC_RE = re.compile(r'^([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})$', re.IGNORECASE)
_HN_RE = re.compile(r'^(\d+[A-Z]?)', re.IGNORECASE)
_FLAT_PREFIX = re.compile(
    r'^(flat|apartment|apt|unit|floor|suite|room)\s+\S+[,\s]+',
    re.IGNORECASE,
)


def _fmt(raw: str) -> str | None:
    raw = raw.strip().upper().replace(" ", "")
    if len(raw) < 5:
        return None
    formatted = raw[:-3] + " " + raw[-3:]
    if not _PC_RE.match(formatted):
        return None
    return formatted


def _house_number(s: str) -> str | None:
    s = _FLAT_PREFIX.sub("", s.strip()).strip().upper()
    m = _HN_RE.match(s)
    return m.group(1) if m else None


def _geocode(postcode: str) -> tuple:
    try:
        r = requests.get(
            f"https://api.postcodes.io/postcodes/{postcode.replace(' ', '')}",
            timeout=5,
        )
        if r.ok:
            res = (r.json() or {}).get("result") or {}
            return res.get("latitude"), res.get("longitude")
    except Exception:
        pass
    return None, None


_RM_TYPE_MAP = {
    "flat": "F", "apartment": "F", "maisonette": "F",
    "detached": "D", "semi-detached": "S",
    "terraced": "T", "bungalow": "D",
}
_RM_FLAT_PREFIX = re.compile(
    r'^(flat|apartment|apt|unit|floor|suite|room)\s+\S+[,\s]+', re.IGNORECASE
)
_RM_TYPE_RE   = re.compile(
    r'(?<![A-Za-z])(flat|detached|semi-detached|terraced|maisonette|apartment|bungalow)',
    re.IGNORECASE,
)
_RM_TENURE_RE = re.compile(r'(?<![A-Za-z])(leasehold|freehold)', re.IGNORECASE)
_RM_PC_RE     = re.compile(r'[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}', re.IGNORECASE)
_RM_TOWNS     = {"london", "england", "wales", "scotland", "ireland"}


def _fetch_rm_sold(postcode: str) -> tuple:
    """
    Fetch sold prices from Rightmove for the given postcode.
    Returns (prop_sales, main_street) where prop_sales is {(paon,saon): [sale_dicts]}.
    HMLR LDP API was decommissioned (403); Rightmove is the replacement source.
    """
    if not _RM_AVAILABLE:
        logger.error("[postcode_analysis] curl_cffi / bs4 not installed — cannot fetch sold prices")
        return {}, ""

    slug = postcode.replace(" ", "").lower()
    try:
        resp = _cffi_requests.get(
            f"https://www.rightmove.co.uk/house-prices/{slug}.html",
            impersonate="chrome110",
            timeout=15,
        )
    except Exception as exc:
        logger.warning("[postcode_analysis] Rightmove fetch error for %s: %s", postcode, exc)
        return {}, ""

    if resp.status_code != 200:
        logger.warning("[postcode_analysis] Rightmove HTTP %d for %s", resp.status_code, postcode)
        return {}, ""

    soup = _BeautifulSoup(resp.content, "lxml")
    trans_tables = soup.find_all(class_=re.compile(r'_transactionProviderTable_'))
    if not trans_tables:
        logger.info("[postcode_analysis] Rightmove: no sold data for %s", postcode)
        return {}, ""

    prop_sales: dict = defaultdict(list)
    streets: list = []

    for tt in trans_tables:
        # Find address in nearest DOM ancestor
        address_text = None
        node = tt.parent
        for _ in range(6):
            if node is None:
                break
            for el in node.find_all(["h2", "h3", "h4", "span", "p", "a"], recursive=False):
                txt = el.get_text(strip=True)
                if _RM_PC_RE.search(txt) and len(txt) > 8:
                    address_text = txt
                    break
            if address_text:
                break
            node = node.parent

        if not address_text:
            continue

        # Clean address at postcode boundary
        pc_m = _RM_PC_RE.search(address_text)
        clean_address = address_text[:pc_m.end()].strip() if pc_m else address_text

        # Extract type and tenure (may be concatenated after postcode: "LeaseholdPrevious")
        type_m   = _RM_TYPE_RE.search(address_text)
        tenure_m = _RM_TENURE_RE.search(address_text)
        raw_type       = _RM_TYPE_MAP.get(type_m.group(1).lower() if type_m else "", "")
        raw_tenure_key = tenure_m.group(1)[0].upper() if tenure_m else ""
        prop_type  = _PROP_TYPE.get(raw_type, "")
        tenure_str = _TENURE.get(raw_tenure_key, tenure_m.group(1).capitalize() if tenure_m else "")

        # Parse address into parts
        parts = [p.strip() for p in clean_address.split(",")]
        parts = [
            p for p in parts
            if p and not re.match(r'^[A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2}$', p.strip(), re.I)
        ]

        if _RM_FLAT_PREFIX.match(clean_address):
            saon   = parts[0].upper() if len(parts) > 0 else ""
            paon   = parts[1].upper() if len(parts) > 1 else ""
            street = parts[2].upper() if len(parts) > 2 else ""
        else:
            saon   = ""
            paon   = parts[0].upper() if len(parts) > 0 else ""
            street = parts[1].upper() if len(parts) > 1 else ""

        if street.lower() in _RM_TOWNS:
            street = ""
        if street:
            streets.append(street)

        display_address = ", ".join(p for p in [saon, paon, street] if p)

        # Extract sale rows
        for row in tt.find_all(class_=re.compile(r'_transactionTableRow_')):
            cells = row.find_all(class_=re.compile(r'_transactionTableCell_'))
            if len(cells) < 2:
                continue
            date_raw    = cells[0].get_text(strip=True)
            price_raw   = cells[1].get_text(strip=True)
            if not re.search(r'\d{4}', date_raw):
                continue
            price_clean = re.sub(r'[^0-9]', '', price_raw)
            if not price_clean:
                continue
            prop_sales[(paon, saon)].append({
                "price":     int(price_clean),
                "date":      _parse_lr_date(date_raw),
                "type":      prop_type,
                "tenure":    tenure_str,
                "new_build": None,
                "address":   display_address,
                "paon":      paon,
                "saon":      saon,
                "street":    street,
                "postcode":  postcode,
                "category":  "Standard",
            })

    main_street = Counter(streets).most_common(1)[0][0] if streets else ""
    return dict(prop_sales), main_street


def _fetch_ownership(postcode: str) -> list:
    try:
        with engine.connect() as conn:
            rows = conn.execute(text("""
                SELECT title_number, tenure, MIN(address) AS address, source,
                       MAX(date_added)::text AS date_added,
                       COUNT(*) AS proprietor_count,
                       STRING_AGG(company_name, ' | ' ORDER BY company_name) AS company_names,
                       STRING_AGG(COALESCE(company_number,''), ' | ' ORDER BY company_name) AS company_numbers,
                       MIN(proprietor_cat) AS proprietor_cat, MIN(country) AS country
                FROM company_ownership
                WHERE postcode = :pc
                GROUP BY title_number, tenure, source
                ORDER BY CASE WHEN tenure ILIKE 'f%%' THEN 0 ELSE 1 END,
                         MAX(date_added) DESC NULLS LAST
                LIMIT 30
            """), {"pc": postcode}).fetchall()
        return [
            {
                "title_number":     r.title_number,
                "tenure":           r.tenure,
                "address":          r.address,
                "source":           r.source,
                "date_added":       r.date_added,
                "proprietor_count": r.proprietor_count,
                "company_names":    [x for x in (r.company_names or "").split(" | ") if x],
                "company_numbers":  [x or None for x in (r.company_numbers or "").split(" | ")],
                "proprietor_cat":   r.proprietor_cat,
                "country":          r.country,
            }
            for r in rows
        ]
    except Exception as exc:
        logger.error("[postcode_analysis] ownership query failed: %s", exc)
        return []


def _build_prop_sales(postcode: str) -> tuple:
    """
    Fetch and parse sold price data for postcode.
    Returns (prop_sales, main_street) where prop_sales is {(paon,saon): [sale_dicts]}.
    """
    return _fetch_rm_sold(postcode)


def _sse(data: dict) -> str:
    return "data: " + json.dumps(data) + "\n\n"


# ── Main endpoint: LR + EPC for in-postcode + ownership → JSON ───────────────

@postcode_analysis_bp.route("/api/postcode-analysis")
@require_auth(feature="postcode_analysis")
@track_feature("postcode_analysis", action="view")
def get_postcode_analysis():
    postcode = _fmt(request.args.get("postcode", ""))
    if not postcode:
        return jsonify({"error": "Invalid or missing postcode"}), 400

    try:
        # LR data + geocode in parallel
        with ThreadPoolExecutor(max_workers=2) as pool:
            f_lr      = pool.submit(_build_prop_sales, postcode)
            f_geocode = pool.submit(_geocode, postcode)

        prop_sales, main_street = f_lr.result()
        lat, lng                = f_geocode.result()
    except Exception:
        logger.exception("[postcode_analysis] LR/geocode fetch failed for %s", postcode)
        return jsonify({
            "found": False, "postcode": postcode, "lat": None, "lng": None,
            "stats": {}, "properties": [], "main_street": "",
            "error": "Data fetch failed — please try again", "ownership_count": 0,
        })

    if not prop_sales:
        return jsonify({
            "found": False, "postcode": postcode, "lat": lat, "lng": lng,
            "stats": {}, "properties": [], "main_street": "",
            "error": "No sales found for this postcode", "ownership_count": 0,
        })

    # Build property map
    prop_map: dict = {}
    for (paon, saon), sales in prop_sales.items():
        key = (paon, saon)
        ref   = sorted(sales, key=lambda s: s.get("date", ""), reverse=True)[0]
        in_pc = any(s.get("postcode") == postcode for s in sales)
        prop_map[key] = {
            "paon": paon, "saon": saon,
            "display_address": ref["address"],
            "street": ref.get("street", ""),
            "postcode": ref["postcode"],
            "in_postcode": in_pc,
            "last_price": None, "last_date": None,
            "floor_area_m2": None, "price_per_m2": None, "investment_pct": None,
            "epc_cert": None, "epc_address": None,
            "epc_band": None, "epc_score": None,
            "epc_floor_area_raw": None, "epc_property_type": None,
            "epc_built_form": None, "epc_habitable_rooms": None,
            "ownership": [],
        }

    # ── EPC for in-postcode properties only (keep initial load fast) ──────────
    cert_for_prop:     dict = {}
    epc_addr_for_prop: dict = {}
    raw_rec_for_cert:  dict = {}

    prop_ref_pc = {
        k: sorted(v, key=lambda s: s.get("date", ""), reverse=True)[0]
        for k, v in prop_sales.items()
        if prop_map[k].get("in_postcode", False)
    }

    def _epc_for_prop(item):
        key, ref_sale = item
        try:
            result  = _fetch_epc(
                postcode, ref_sale["address"],
                paon=ref_sale.get("paon"), saon=ref_sale.get("saon"),
            )
            records = result.get("records", []) if result.get("found") else []
            return key, records
        except Exception:
            return key, []

    if prop_ref_pc:
        with ThreadPoolExecutor(max_workers=min(5, len(prop_ref_pc))) as pool:
            for key, records in pool.map(_epc_for_prop, prop_ref_pc.items()):
                if not records:
                    continue
                best_rec = records[0]
                cert = best_rec.get("certificateNumber") or best_rec.get("lmkKey")
                if not cert:
                    continue
                cert_for_prop[key] = cert
                raw_rec_for_cert[cert] = best_rec
                addr_parts = [
                    str(best_rec.get(fld) or "").strip()
                    for fld in ("addressLine1", "addressLine2", "addressLine3", "postcode")
                ]
                epc_addr_for_prop[key] = ", ".join(p for p in addr_parts if p)
                prop_map[key]["epc_band"]  = (
                    best_rec.get("currentEnergyRating") or best_rec.get("energyRating")
                )
                prop_map[key]["epc_score"] = best_rec.get("currentEnergyEfficiency")

    # EPC detail scraping (unique certs only, parallel)
    unique_certs = set(cert_for_prop.values())
    cert_detail: dict = {}
    if unique_certs:
        def _get_detail(cert):
            return cert, _fetch_epc_detail(cert, raw_rec_for_cert.get(cert))
        with ThreadPoolExecutor(max_workers=min(5, len(unique_certs))) as pool:
            for cert, detail in pool.map(_get_detail, unique_certs):
                cert_detail[cert] = detail

    # ── Ownership (single sequential DB query) ────────────────────────────────
    ownership_titles = _fetch_ownership(postcode)

    # ── Assemble property records ─────────────────────────────────────────────
    for key, prop in prop_map.items():
        cert = cert_for_prop.get(key)
        if cert:
            prop["epc_cert"]    = cert
            prop["epc_address"] = epc_addr_for_prop.get(key, "")
            detail = cert_detail.get(cert, {})
            if detail.get("found"):
                prop["floor_area_m2"]       = detail.get("floor_area_m2")
                prop["epc_floor_area_raw"]  = detail.get("floor_area_raw")
                prop["epc_property_type"]   = detail.get("property_type_epc")
                prop["epc_built_form"]      = detail.get("built_form")
                prop["epc_habitable_rooms"] = detail.get("habitable_rooms")
                if not prop.get("epc_band"):
                    prop["epc_band"]  = detail.get("current_band")
                if not prop.get("epc_score"):
                    prop["epc_score"] = detail.get("current_score")

        sorted_s = sorted(prop_sales[key], key=lambda s: s.get("date", ""), reverse=True)
        prop["last_price"] = sorted_s[0].get("price")
        prop["last_date"]  = sorted_s[0].get("date")
        prop["sales"]      = sorted_s

        area  = prop.get("floor_area_m2")
        price = prop.get("last_price")
        if area and isinstance(price, (int, float)) and area > 0:
            prop["price_per_m2"] = round(price / area)

        paon_hn = _house_number(prop["paon"])
        for title in ownership_titles:
            if paon_hn and _house_number(title.get("address", "")) == paon_hn:
                prop["ownership"].append(title)

    # ── Investment metric: price/m² vs in-postcode median ────────────────────
    sqm_props  = [p for p in prop_map.values() if p.get("price_per_m2")]
    sqm_values = sorted(p["price_per_m2"] for p in sqm_props)
    n = len(sqm_values)
    median_sqm = (sqm_values[n // 2] if n % 2 else (sqm_values[n // 2 - 1] + sqm_values[n // 2]) // 2) if n else None

    if median_sqm:
        for prop in prop_map.values():
            ppm2 = prop.get("price_per_m2")
            if ppm2:
                prop["investment_pct"] = round((ppm2 / median_sqm - 1) * 100, 1)

    # ── Sort and build response ───────────────────────────────────────────────
    all_props = sorted(
        prop_map.values(),
        key=lambda p: (
            0 if p.get("price_per_m2") else 1,
            -(p.get("investment_pct") or 0),
            p.get("paon", ""),
            p.get("saon", ""),
        ),
    )
    pc_props = [p for p in all_props if p.get("in_postcode")]
    prices   = sorted(p["last_price"] for p in pc_props if isinstance(p.get("last_price"), (int, float)))
    m = len(prices)
    median_price = (prices[m // 2] if m % 2 else (prices[m // 2 - 1] + prices[m // 2]) // 2) if prices else None

    return jsonify({
        "found":       True,
        "postcode":    postcode,
        "main_street": main_street,
        "lat":         lat,
        "lng":         lng,
        "stats": {
            "total_sales":         sum(len(v) for k, v in prop_sales.items() if prop_map[k].get("in_postcode")),
            "unique_props":        len(pc_props),
            "total_street_props":  len(all_props),
            "median_price":        median_price,
            "median_price_per_m2": median_sqm,
            "sqm_coverage":        len(sqm_props),
            "sqm_missing":         len(pc_props) - len([p for p in pc_props if p.get("floor_area_m2")]),
        },
        "properties":      list(all_props),
        "ownership_count": len(ownership_titles),
    })


# ── EPC stream: user-triggered, all properties via SSE ───────────────────────

@postcode_analysis_bp.route("/api/postcode-analysis/epc-stream")
@require_auth(feature="postcode_analysis")
def get_epc_stream():
    postcode = _fmt(request.args.get("postcode", ""))
    if not postcode:
        return jsonify({"error": "Invalid or missing postcode"}), 400

    def generate():
        try:
            yield from _stream_epc(postcode)
        except Exception as exc:
            logger.error("[postcode_analysis] epc stream error: %s", exc, exc_info=True)
            yield _sse({"type": "error", "error": "EPC stream failed. Please try again."})

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _stream_epc(postcode: str):
    prop_sales, _ = _build_prop_sales(postcode)

    if not prop_sales:
        yield _sse({"type": "error", "error": "No properties found for this postcode."})
        return

    prop_ref = {
        k: sorted(v, key=lambda s: s.get("date", ""), reverse=True)[0]
        for k, v in prop_sales.items()
    }

    # Let the frontend show a spinner while we work
    yield _sse({"type": "start", "total": len(prop_ref)})

    def _epc_for_prop(item):
        key, ref_sale = item
        prop_postcode = ref_sale.get("postcode") or postcode
        try:
            result  = _fetch_epc(
                prop_postcode, ref_sale["address"],
                paon=ref_sale.get("paon"), saon=ref_sale.get("saon"),
            )
            records = result.get("records", []) if result.get("found") else []
            return key, records, ref_sale
        except Exception:
            return key, [], ref_sale

    # Collect ALL results — send one grouped response instead of one per property
    epc_updates: list = []
    ppm2_map:    dict = {}

    with ThreadPoolExecutor(max_workers=5) as pool:
        for key, records, ref_sale in pool.map(_epc_for_prop, prop_ref.items()):
            try:
                if not records:
                    continue
                best_rec = records[0]
                cert = best_rec.get("certificateNumber") or best_rec.get("lmkKey")
                if not cert:
                    continue
                detail = _fetch_epc_detail(cert, best_rec)
                if not detail.get("found"):
                    continue

                floor_area = detail.get("floor_area_m2")
                band  = (best_rec.get("currentEnergyRating") or best_rec.get("energyRating")
                         or detail.get("current_band"))
                score = best_rec.get("currentEnergyEfficiency") or detail.get("current_score")
                addr_parts = [
                    str(best_rec.get(f) or "").strip()
                    for f in ("addressLine1", "addressLine2", "addressLine3", "postcode")
                ]
                updates = {
                    "floor_area_m2":       floor_area,
                    "epc_floor_area_raw":  detail.get("floor_area_raw"),
                    "epc_property_type":   detail.get("property_type_epc"),
                    "epc_built_form":      detail.get("built_form"),
                    "epc_habitable_rooms": detail.get("habitable_rooms"),
                    "epc_cert":    cert,
                    "epc_band":    band,
                    "epc_score":   score,
                    "epc_address": ", ".join(p for p in addr_parts if p),
                }

                price = ref_sale.get("price")
                if floor_area and isinstance(price, (int, float)) and floor_area > 0:
                    ppm2 = round(price / floor_area)
                    updates["price_per_m2"] = ppm2
                    ppm2_map[key] = ppm2

                epc_updates.append({"paon": key[0], "saon": key[1], "updates": updates})

            except Exception as exc:
                logger.warning("[postcode_analysis] EPC error for %s: %s", key, exc)

    # Compute investment metrics, then send everything in one response
    sqm_values = sorted(ppm2_map.values())
    n = len(sqm_values)
    median_sqm = (
        sqm_values[n // 2] if n % 2
        else (sqm_values[n // 2 - 1] + sqm_values[n // 2]) // 2
    ) if n else None

    investment_updates: dict = {}
    if median_sqm:
        for k, ppm2 in ppm2_map.items():
            investment_updates[f"{k[0]}|{k[1]}"] = round((ppm2 / median_sqm - 1) * 100, 1)

    yield _sse({
        "type":               "complete",
        "epc_updates":        epc_updates,
        "median_sqm":         median_sqm,
        "investment_updates": investment_updates,
    })
