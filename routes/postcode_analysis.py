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
from routes.property_data import (
    _lr_val, _parse_lr_date, _PROP_TYPE, _TENURE, TIMEOUT,
    _fetch_epc, _fetch_epc_detail,
)

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


def _fetch_lr_sales(postcode: str) -> list:
    all_items: list = []
    for page in range(3):
        try:
            r = requests.get(
                "https://landregistry.data.gov.uk/data/ppi/transaction-record.json",
                params={
                    "propertyAddress.postcode": postcode,
                    "_pageSize": 100,
                    "_sort": "-transactionDate",
                    "_page": page,
                },
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
            logger.warning("[postcode_analysis] LR page %d error: %s", page, exc)
            break
    return all_items


def _fetch_lr_sales_by_street(street: str, town: str) -> list:
    all_items: list = []
    for page in range(3):
        try:
            r = requests.get(
                "https://landregistry.data.gov.uk/data/ppi/transaction-record.json",
                params={
                    "propertyAddress.street": street,
                    "propertyAddress.town": town,
                    "_pageSize": 100,
                    "_sort": "-transactionDate",
                    "_page": page,
                },
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
            logger.warning("[postcode_analysis] street LR page %d error: %s", page, exc)
            break
    return all_items


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
    Fetch and parse LR data for postcode + street expansion.
    Returns (prop_sales, main_street) where prop_sales is {(paon,saon): [sale_dicts]}.
    """
    lr_postcode_items = _fetch_lr_sales(postcode)
    if not lr_postcode_items:
        return {}, ""

    _sc: Counter = Counter()
    _tc: Counter = Counter()
    for _item in lr_postcode_items:
        _addr = _item.get("propertyAddress") or {}
        _sv = str(_lr_val(_addr.get("street")) or "").strip().upper()
        _tv = str(_lr_val(_addr.get("town"))   or "").strip().upper()
        if _sv: _sc[_sv] += 1
        if _tv: _tc[_tv] += 1
    main_street = _sc.most_common(1)[0][0] if _sc else ""
    main_town   = _tc.most_common(1)[0][0] if _tc else ""

    lr_street_items = (
        _fetch_lr_sales_by_street(main_street, main_town)
        if main_street and main_town else []
    )

    # Limit street expansion to the same postcode district (e.g. "PL4") so we
    # don't pull in properties from neighbouring districts on the same street name.
    _target_district = re.match(
        r"^([A-Z]{1,2}\d{1,2}[A-Z]?)",
        re.sub(r"\s+", "", postcode.upper()),
    )
    _target_district = _target_district.group(1) if _target_district else ""

    def _item_district(item: dict) -> str:
        pc = str(
            _lr_val((item.get("propertyAddress") or {}).get("postcode")) or ""
        ).replace(" ", "").upper()
        m = re.match(r"^([A-Z]{1,2}\d{1,2}[A-Z]?)", pc)
        return m.group(1) if m else ""

    if _target_district:
        lr_street_items = [
            item for item in lr_street_items
            if _item_district(item) == _target_district
        ]

    seen_txids: set = {item.get("transactionId", "") for item in lr_postcode_items}
    lr_items = lr_postcode_items + [
        item for item in lr_street_items
        if item.get("transactionId", "") not in seen_txids
    ]

    def _s(v):
        return _lr_val(v) if isinstance(v, dict) else v

    raw_sales: list = []
    for item in lr_items:
        addr       = item.get("propertyAddress") or {}
        paon       = str(_s(addr.get("paon"))     or "")
        saon       = str(_s(addr.get("saon"))     or "")
        street_n   = str(_s(addr.get("street"))   or "")
        town_n     = str(_s(addr.get("town"))     or "")
        pc         = str(_s(addr.get("postcode")) or postcode)
        raw_type   = str(_lr_val(item.get("propertyType")) or "")
        raw_tenure = str(_lr_val(item.get("estateType"))   or "")
        raw_cat    = str(_lr_val(item.get("transactionCategory")) or "")
        parts      = [p for p in [saon, paon, street_n, town_n] if p]
        raw_sales.append({
            "price":     _s(item.get("pricePaid")),
            "date":      _parse_lr_date(str(item.get("transactionDate") or "")),
            "type":      _PROP_TYPE.get(raw_type, raw_type.title() if raw_type else ""),
            "tenure":    _TENURE.get(raw_tenure, raw_tenure.title() if raw_tenure else ""),
            "new_build": _s(item.get("newBuild")),
            "address":   ", ".join(parts),
            "paon":      paon,
            "saon":      saon,
            "street":    street_n,
            "postcode":  pc,
            "category":  "Additional" if "additional" in raw_cat.lower() else "Standard",
        })

    prop_sales: dict = defaultdict(list)
    for sale in raw_sales:
        prop_sales[(sale["paon"], sale["saon"])].append(sale)

    return dict(prop_sales), main_street


def _sse(data: dict) -> str:
    return "data: " + json.dumps(data) + "\n\n"


# ── Main endpoint: LR + EPC for in-postcode + ownership → JSON ───────────────

@postcode_analysis_bp.route("/api/postcode-analysis")
def get_postcode_analysis():
    postcode = _fmt(request.args.get("postcode", ""))
    if not postcode:
        return jsonify({"error": "Invalid or missing postcode"}), 400

    # LR data + geocode in parallel
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_lr      = pool.submit(_build_prop_sales, postcode)
        f_geocode = pool.submit(_geocode, postcode)

    prop_sales, main_street = f_lr.result()
    lat, lng                = f_geocode.result()

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
            result  = _fetch_epc(postcode, ref_sale["address"])
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
            result  = _fetch_epc(prop_postcode, ref_sale["address"])
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
