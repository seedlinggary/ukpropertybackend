"""
Planning search route.

Data source (live — nothing stored in DB):
  datasette.planning.data.gov.uk — SQL interface over planning.data.gov.uk data
  contributed by LPAs to the MHCLG national planning dataset.

Approach:
  - Keyword LIKE search across all contributed planning applications nationally
  - Geocode postcode to identify user's council (LPA)
  - Highlight results from the user's own council
  - No spatial filtering (geometry field is empty in the dataset)
  - No data written to our DB

Coverage: ~20 LPAs contributing at time of writing (Doncaster, Worthing, Camden,
Wandsworth, Worthing, etc). See /api/planning/change-types for details.
"""

import logging
import threading
from flask import Blueprint, request, jsonify
import requests as _req
from .idox_scraper import find_council_portal, idox_search, IDOX_PORTALS

log = logging.getLogger(__name__)

planning_bp = Blueprint("planning", __name__)

# ─── Datasette base URL ───────────────────────────────────────────────────────

DATASETTE_BASE = "https://datasette.planning.data.gov.uk"
DATASETTE_TIMEOUT = 25  # seconds per query — loft/HMO can take 15-16s on busy days

# ─── Local Authority entity cache ─────────────────────────────────────────────
# Populated lazily on first request; maps entity_id (int) → {name, reference}

_la_cache: dict[int, dict] = {}
_la_cache_lock = threading.Lock()
_la_cache_loaded = False


def _load_la_cache() -> None:
    global _la_cache_loaded
    with _la_cache_lock:
        if _la_cache_loaded:
            return
        try:
            r = _req.get(
                f"{DATASETTE_BASE}/local-authority.json",
                params={
                    "sql": "SELECT entity, reference, name FROM entity",
                    "_shape": "array",
                },
                timeout=15,
                headers={"Accept": "application/json", "User-Agent": "PropSearch/1.0"},
            )
            if r.ok:
                for row in r.json():
                    eid = int(row["entity"])
                    _la_cache[eid] = {"name": row.get("name", ""), "reference": row.get("reference", "")}
                _la_cache_loaded = True
                log.info(f"[planning] Loaded {len(_la_cache)} local authorities from datasette")
        except Exception as e:
            log.warning(f"[planning] Could not load LA cache: {e}")


_LA_NOISE = {"council", "borough", "district", "city", "county", "of", "the",
              "and", "&", "london", "metropolitan", "royal"}


def _normalise_la_name(name: str) -> str:
    """Strip punctuation and noise words for fuzzy matching."""
    import re
    tokens = re.sub(r"[,.'()]", " ", name.lower()).split()
    return " ".join(t for t in tokens if t not in _LA_NOISE).strip()


def _find_la_entity(council_name: str) -> int | None:
    """Best-effort fuzzy match of a council name against the LA cache."""
    if not council_name:
        return None
    _load_la_cache()
    lower = council_name.lower().strip()
    norm = _normalise_la_name(council_name)

    # 1. exact match (original)
    for eid, la in _la_cache.items():
        if la["name"].lower() == lower:
            return eid

    # 2. normalised exact match
    for eid, la in _la_cache.items():
        if _normalise_la_name(la["name"]) == norm:
            return eid

    # 3. partial — original strings contain each other
    for eid, la in _la_cache.items():
        la_lower = la["name"].lower()
        if lower in la_lower or la_lower in lower:
            return eid

    # 4. normalised partial — normalised strings contain each other
    for eid, la in _la_cache.items():
        la_norm = _normalise_la_name(la["name"])
        if norm and (norm in la_norm or la_norm in norm):
            return eid

    # 5. significant-word overlap
    norm_words = set(norm.split())
    for eid, la in _la_cache.items():
        la_words = set(_normalise_la_name(la["name"]).split())
        if norm_words and norm_words <= la_words:
            return eid

    return None


# ─── Change-type → fast datasette search term ────────────────────────────────
# search_term is used in a single LIKE '%term%' clause.  Must complete <20s.
# None means: no keyword filter (return most-recent decided applications).
# client_keywords is a broader list used for in-Python relevance scoring.

CHANGE_TYPES = {
    "any": {
        "label": "Any / General Search",
        "search_term": None,
        "client_keywords": [],
    },
    "rear_extension": {
        "label": "Rear Extension",
        "search_term": "rear extension",
        "client_keywords": [
            "rear extension", "rear single storey", "rear two-storey",
            "rear two storey", "rear addition", "rear outrigger",
            "rear wrap around", "rear wraparound",
        ],
    },
    "side_extension": {
        "label": "Side Extension",
        "search_term": "side extension",
        "client_keywords": [
            "side extension", "side return", "side infill",
            "side single storey", "side two storey",
        ],
    },
    "loft": {
        "label": "Loft / Top Floor Addition",
        "search_term": "loft conversion",
        "client_keywords": [
            "loft conversion", "dormer", "hip to gable", "roof extension",
            "roof alteration", "mansard", "rooflight", "velux",
            "top floor", "attic conversion",
        ],
    },
    "hmo": {
        "label": "HMO Conversion",
        "search_term": "HMO",
        "client_keywords": [
            "house in multiple occupation", "hmo",
            "c3 to c4", "c3 to sui generis",
            "shared accommodation", "bedsit",
        ],
    },
    "new_build": {
        "label": "New Build (Residential)",
        "search_term": None,
        "client_keywords": [
            "erection of a dwelling", "erection of dwellings",
            "new dwelling", "new build", "new residential",
            "residential development", "construction of a dwelling",
            "demolition and erection",
        ],
    },
    "commercial_to_residential": {
        "label": "Commercial → Residential (Change of Use)",
        "search_term": "change of use",
        "client_keywords": [
            "class e to c3", "b1 to c3", "a1 to c3", "a2 to c3",
            "office to residential", "commercial to residential",
            "retail to residential", "prior approval",
            "change of use from class e", "change of use from b1",
            "change of use to c3", "permitted development",
        ],
    },
    "prior_approval": {
        "label": "Prior Approval (Permitted Development)",
        "search_term": "prior approval",
        "client_keywords": [
            "prior approval", "prior notification",
            "class q", "class ma", "class o",
            "permitted development right", "pd right",
        ],
    },
    "outbuilding": {
        "label": "Outbuilding / Garden Room / Annexe",
        "search_term": "outbuilding",
        "client_keywords": [
            "outbuilding", "garden room", "garden building",
            "annexe", "summer house", "detached garage",
            "ancillary accommodation",
        ],
    },
    "basement": {
        "label": "Basement Extension",
        "search_term": "basement",
        "client_keywords": [
            "basement", "cellar conversion",
            "lower ground floor extension", "subterranean extension",
        ],
    },
    "demolition": {
        "label": "Demolition",
        "search_term": "demolition",
        "client_keywords": [
            "demolition", "demolish", "knockdown", "prior approval for demolition",
            "demolition of existing", "demolition of a dwelling",
        ],
    },
    "retrospective": {
        "label": "Retrospective Application",
        "search_term": "retrospective",
        "client_keywords": [
            "retrospective", "retrospective permission",
            "retrospective planning", "works already carried out",
            "regularise", "regularisation",
        ],
    },
    "lawful_development": {
        "label": "Lawful Development Certificate (LDC)",
        "search_term": "Lawful Development Certificate",
        "client_keywords": [
            "lawful development certificate", "ldc",
            "certificate of lawfulness", "lawful use",
            "lawful development", "existing use",
        ],
    },
    "solar": {
        "label": "Solar Panels / Renewable Energy",
        "search_term": "solar",
        "client_keywords": [
            "solar panel", "solar pv", "photovoltaic", "solar array",
            "solar energy", "solar farm", "ground mounted solar",
            "solar installation",
        ],
    },
    "parking": {
        "label": "Parking / Driveway / Dropped Kerb",
        "search_term": "parking",
        "client_keywords": [
            "parking", "driveway", "dropped kerb", "vehicle crossover",
            "hardstanding", "parking space", "car park",
            "vehicular access",
        ],
    },
    "porch": {
        "label": "Porch / Front Extension",
        "search_term": "porch",
        "client_keywords": [
            "porch", "front porch", "entrance porch", "canopy",
            "front extension",
        ],
    },
    "householder": {
        "label": "Householder (General Small Works)",
        "search_term": "householder",
        "client_keywords": [
            "householder", "householder application",
            "minor works", "alterations and extensions",
        ],
    },
    "cladding": {
        "label": "External Cladding / Render / Insulation",
        "search_term": "cladding",
        "client_keywords": [
            "cladding", "external insulation", "render", "external render",
            "wall cladding", "external wall insulation",
            "overcladding",
        ],
    },
}

# ─── Decision normalisation ───────────────────────────────────────────────────


def _norm_decision(planning_decision: str, status: str, decision_date: str) -> str:
    """
    Derive a human-readable decision string from the planning-application fields.
    planning-decision: "Approve" | "Refuse" | ""
    planning-application-status: "Registered" | "Final Decision" | ""
    decision-date: "YYYY-MM-DD" or ""
    """
    pd = (planning_decision or "").strip()
    if pd.lower() == "approve":
        return "Approved"
    if pd.lower() == "refuse":
        return "Refused"
    if pd.lower() == "withdraw":
        return "Withdrawn"

    st = (status or "").strip().lower()
    if st in ("final decision",):
        return "Decided"
    if st in ("registered", "pending consideration", "under consideration"):
        return "Pending"

    if decision_date:
        return "Decided"
    return "Pending / Unknown"


# ─── Geocode ──────────────────────────────────────────────────────────────────


def _geocode(postcode: str) -> tuple[float, float, str, str]:
    """Returns (lat, lng, normalised_postcode, admin_district) or raises ValueError."""
    clean = postcode.replace(" ", "").upper()
    try:
        r = _req.get(f"https://api.postcodes.io/postcodes/{clean}", timeout=6)
    except Exception as e:
        raise ValueError(f"Geocode request failed: {e}")
    if r.status_code != 200:
        raise ValueError(f"Postcode not found: {postcode}")
    result = r.json().get("result", {})
    lat = result["latitude"]
    lng = result["longitude"]
    norm_pc = result.get("postcode", clean)
    admin_district = result.get("admin_district", "")
    return lat, lng, norm_pc, admin_district


# ─── Datasette search ─────────────────────────────────────────────────────────


def _fetch_via_datasette(search_term: str | None, limit: int = 50) -> tuple[list[dict], bool]:
    """
    Query datasette for planning applications.
    Filters to applications that have a decision-date (i.e., decided).
    Returns (list_of_raw_rows, timed_out).
    """
    _load_la_cache()

    if search_term:
        sql = (
            f"SELECT entity, reference, organisation_entity, json "
            f"FROM entity "
            f"WHERE json LIKE '%decision-date%' AND json LIKE '%{search_term}%' "
            f"ORDER BY entity DESC LIMIT {limit}"
        )
    else:
        sql = (
            f"SELECT entity, reference, organisation_entity, json "
            f"FROM entity "
            f"WHERE json LIKE '%decision-date%' "
            f"ORDER BY entity DESC LIMIT {limit}"
        )

    try:
        r = _req.get(
            f"{DATASETTE_BASE}/planning-application.json",
            params={"sql": sql, "_shape": "array"},
            headers={"Accept": "application/json", "User-Agent": "PropSearch/1.0"},
            timeout=DATASETTE_TIMEOUT,
        )
        if r.status_code == 400:
            log.warning(f"[planning] datasette timeout for term={search_term!r}")
            return [], True
        if not r.ok:
            log.warning(f"[planning] datasette HTTP {r.status_code}")
            return [], False
        return r.json(), False
    except Exception as e:
        log.warning(f"[planning] datasette request failed: {e}")
        return [], False


import json as _json


def _parse_rows(rows: list[dict], user_la_entity: int | None) -> list[dict]:
    """Convert raw datasette rows into the API result shape."""
    _load_la_cache()
    results = []
    for row in rows:
        entity_id = row.get("entity")
        reference = row.get("reference", "")
        org_entity = row.get("organisation_entity")

        raw_json = row.get("json") or "{}"
        try:
            j = _json.loads(raw_json)
        except Exception:
            j = {}

        description = j.get("description", "")
        if not description:
            continue  # skip records with no description

        address = j.get("address-text", "")
        decision_date = j.get("decision-date", "")
        planning_decision = j.get("planning-decision", "")
        status = j.get("planning-application-status", "")
        doc_url = j.get("documentation-url", "")

        # Prefer the council's own portal URL; fall back to the national entity page
        if doc_url:
            source_url = doc_url
        elif entity_id:
            source_url = f"https://www.planning.data.gov.uk/entity/{entity_id}"
        else:
            source_url = None

        # Council name from cache
        council_name = ""
        if org_entity:
            la = _la_cache.get(int(org_entity), {})
            council_name = la.get("name", f"LPA entity {org_entity}")

        is_user_lpa = (
            user_la_entity is not None
            and org_entity is not None
            and int(org_entity) == user_la_entity
        )

        decision = _norm_decision(planning_decision, status, decision_date)

        results.append({
            "id": str(entity_id) if entity_id else reference,
            "reference": reference,
            "address": address,
            "description": description,
            "decision": decision,
            "decision_raw": planning_decision or status,
            "decision_date": decision_date,
            "council": council_name,
            "is_user_lpa": is_user_lpa,
            "application_type": "",
            "application_type_raw": "",
            "source_name": "planning.data.gov.uk",
            "source_label": council_name or "MHCLG Planning Data",
            "source_url": source_url,
            "data_freshness": "Contributed by LPA to the national planning dataset",
        })
    return results


# ─── Relevance scoring ────────────────────────────────────────────────────────


def _score(result: dict, change_type: str) -> int:
    if change_type in ("any", "new_build"):
        return 1
    keywords = CHANGE_TYPES.get(change_type, {}).get("client_keywords", [])
    if not keywords:
        return 1
    text = (result.get("description") or "").lower()
    return sum(1 for kw in keywords if kw in text)


# ─── Routes ───────────────────────────────────────────────────────────────────


@planning_bp.route("/api/planning/search")
def planning_search():
    postcode    = request.args.get("postcode", "").strip()
    change_type = request.args.get("change_type", "any").strip()

    if not postcode:
        return jsonify({"error": "postcode is required"}), 400
    if change_type not in CHANGE_TYPES:
        return jsonify({"error": f"Unknown change_type. Valid: {list(CHANGE_TYPES)}"}), 400

    # 1. Geocode + identify user's LPA
    try:
        lat, lng, norm_pc, admin_district = _geocode(postcode)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    user_la_entity = _find_la_entity(admin_district)
    user_council = admin_district

    # 2. Search datasette + IDOX portal in parallel
    ct = CHANGE_TYPES[change_type]
    search_term = ct["search_term"]
    query_limit = 50 if search_term is None else 20

    # Identify user's council IDOX portal (if any)
    council_portal = find_council_portal(user_council) if user_council else None

    import concurrent.futures as _cf
    idox_results: list[dict] = []
    sources_queried = ["datasette.planning.data.gov.uk (MHCLG)"]

    with _cf.ThreadPoolExecutor(max_workers=2) as pool:
        f_datasette = pool.submit(_fetch_via_datasette, search_term, query_limit)
        # Run IDOX in parallel if the council has a portal AND there's a keyword
        # For 'any' / 'new_build' (no keyword) IDOX would return too many results
        f_idox = None
        if council_portal and search_term:
            matched_key, portal_url = council_portal
            f_idox = pool.submit(idox_search, matched_key, portal_url, search_term)

        rows, timed_out = f_datasette.result()
        if f_idox is not None:
            try:
                idox_results = f_idox.result(timeout=15) or []
                if idox_results:
                    sources_queried.append(f"{council_portal[0]} Planning Portal (IDOX)")
            except Exception:
                idox_results = []

    # 3. Parse rows
    results = _parse_rows(rows, user_la_entity)

    # 4. Merge IDOX results (user's own council first)
    # IDOX results are already for the user's council — insert at top, after dedup
    results = idox_results + results

    # 5. Score + filter
    if change_type not in ("any", "new_build"):
        scored = [(r, _score(r, change_type)) for r in results]
        scored.sort(key=lambda x: (x[0]["is_user_lpa"], x[1]), reverse=True)
        results = [r for r, s in scored if s > 0]
    else:
        results.sort(key=lambda r: (r.get("is_user_lpa", False), r.get("decision_date") or ""),
                     reverse=True)

    # 6. Deduplicate by reference
    seen: set[str] = set()
    deduped = []
    for r in results:
        key = r.get("reference") or r.get("id") or ""
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        deduped.append(r)

    # Summary counts
    approved = sum(1 for r in deduped if r["decision"] == "Approved")
    refused  = sum(1 for r in deduped if r["decision"] == "Refused")
    pending  = sum(1 for r in deduped if r["decision"] in ("Pending", "Pending / Unknown", "Decided"))

    warning = None
    if timed_out and not idox_results:
        warning = (
            "The keyword search timed out on the national dataset. "
            "Try a different change type or check back later."
        )
    elif change_type == "new_build":
        warning = (
            "New-build keywords are too common for a national keyword search; "
            "showing the most recent decided applications nationally. "
            "Filter results by description to find new-build precedents."
        )
    elif council_portal and not idox_results and search_term:
        warning = (
            f"Your council ({user_council}) has an IDOX planning portal but no local results "
            "were returned for this change type — the portal may be temporarily unavailable."
        )

    # Coverage note
    if idox_results:
        coverage_note = (
            f"Showing {len(idox_results)} result(s) from {council_portal[0]}'s local planning portal "
            f"(your council) plus national results from the MHCLG dataset. "
            "Local council results appear first."
        )
    else:
        coverage_note = (
            f"Results are national (not limited to {norm_pc}). "
            f"Results from your council ({user_council}) are highlighted with a badge. "
            "Spatial search is not available — the dataset does not include coordinates for most applications."
        )

    return jsonify({
        "postcode": norm_pc,
        "lat": lat,
        "lng": lng,
        "user_council": user_council,
        "user_la_entity": user_la_entity,
        "change_type": change_type,
        "change_type_label": ct["label"],
        "total": len(deduped),
        "approved": approved,
        "refused": refused,
        "pending": pending,
        "results": deduped,
        "coverage_note": coverage_note,
        "warning": warning,
        "sources_queried": sources_queried,
        "idox_count": len(idox_results),
        "council_has_idox_portal": council_portal is not None,
    })


# ─── Coverage data ────────────────────────────────────────────────────────────
# Which councils have which types of planning data available.
# Sourced empirically from the MHCLG national planning dataset.

# Councils with full planning APPLICATION search (keyword search over all applications)
FULL_APPLICATION_LPAS = [
    {"name": "London Borough of Camden",            "region": "London"},
    {"name": "Worthing Borough Council",            "region": "South East"},
    {"name": "Adur District Council",               "region": "South East"},
    {"name": "Doncaster Metropolitan Borough Council", "region": "Yorkshire and the Humber"},
]

# Councils with Tree Preservation Order data (77 LPAs)
TPO_LPAS = [
    "Adur District Council", "Ashford Borough Council", "Borough Council of King's Lynn and West Norfolk",
    "Bristol City Council", "Broadland District Council", "Calderdale Metropolitan Borough Council",
    "Cambridge City Council", "Canterbury City Council", "Castle Point Borough Council",
    "Central Bedfordshire Council", "City of Westminster", "Colchester City Council",
    "Cotswold District Council", "Coventry City Council", "Dartford Borough Council",
    "Dover District Council", "East Cambridgeshire District Council", "East Hampshire District Council",
    "Epsom and Ewell Borough Council", "Forest of Dean District Council",
    "Gateshead Metropolitan Borough Council", "Gravesham Borough Council",
    "Great Yarmouth Borough Council", "Halton Borough Council", "Harlow District Council",
    "Havant Borough Council", "Horsham District Council", "Huntingdonshire District Council",
    "Knowsley Metropolitan Borough Council", "Leeds City Council", "Leicester City Council",
    "Liverpool City Council", "London Borough of Barnet", "London Borough of Brent",
    "London Borough of Hillingdon", "London Borough of Lambeth", "London Borough of Tower Hamlets",
    "London Borough of Waltham Forest", "Maidstone Borough Council", "Medway Council",
    "Milton Keynes City Council", "New Forest District Council", "Newcastle City Council",
    "North Lincolnshire Council", "North Norfolk District Council", "North Somerset Council",
    "North Tyneside Council", "Northumberland County Council", "Oxford City Council",
    "Peterborough City Council", "Plymouth City Council", "Rossendale Borough Council",
    "Rother District Council", "Royal Borough of Kensington and Chelsea", "Salford City Council",
    "Sandwell Metropolitan Borough Council", "Sefton Metropolitan Borough Council",
    "South Cambridgeshire District Council", "South Gloucestershire Council",
    "South Norfolk District Council", "South Staffordshire Council", "Southampton City Council",
    "Spelthorne Borough Council", "St Albans City and District Council", "Stevenage Borough Council",
    "Stockport Metropolitan Borough Council", "Stoke-on-Trent City Council", "Swale Borough Council",
    "Tandridge District Council", "Thanet District Council", "Torbay Council",
    "Wirral Borough Council", "Worthing Borough Council",
]


@planning_bp.route("/api/planning/coverage")
def planning_coverage():
    """Returns structured coverage data for the frontend coverage panel."""
    idox_council_list = sorted(IDOX_PORTALS.keys())
    return jsonify({
        "full_application_lpas": FULL_APPLICATION_LPAS,
        "tpo_lpa_count": len(TPO_LPAS),
        "tpo_lpas": TPO_LPAS,
        "idox_lpa_count": len(IDOX_PORTALS),
        "idox_lpas": idox_council_list,
        "conservation_area_approx": "100+ LPAs contributing nationally (10,994 records)",
        "article_4_approx": "100+ LPAs contributing nationally (3,234 records)",
        "layers": {
            "layer_1": {
                "name": "MHCLG National Planning Dataset",
                "description": "4 LPAs contributing full planning applications; 100+ LPAs contributing context data (conservation areas, article 4 directions, TPOs, heritage at risk, brownfield sites).",
                "url": "https://datasette.planning.data.gov.uk",
            },
            "layer_2": {
                "name": "IDOX Council Portals",
                "description": f"{len(IDOX_PORTALS)} council planning portals powered by IDOX. When your postcode is in a covered council, we also search their local portal for recent applications (last 2 years).",
                "url": "https://www.idoxgroup.com/products/planning",
            },
        },
        "sources": {
            "planning_applications": "datasette.planning.data.gov.uk/planning-application",
            "idox_portals": "Individual council planning portals (IDOX system)",
            "conservation_areas": "datasette.planning.data.gov.uk/conservation-area",
            "article_4": "datasette.planning.data.gov.uk/article-4-direction",
            "tpos": "datasette.planning.data.gov.uk/tree-preservation-order",
            "heritage": "datasette.planning.data.gov.uk/heritage-at-risk",
            "brownfield": "datasette.planning.data.gov.uk/brownfield-site",
        },
    })


@planning_bp.route("/api/planning/change-types")
def change_types_route():
    """Returns the available change types for the frontend dropdown."""
    return jsonify([
        {"value": k, "label": v["label"]}
        for k, v in CHANGE_TYPES.items()
    ])


# ─── Planning context helpers ─────────────────────────────────────────────────


def _fetch_context_items(database: str, org_entity: int, limit: int = 50) -> list[dict]:
    """Fetch named planning constraint records for one LPA from a datasette database."""
    sql = (
        f"SELECT entity, name, json FROM entity "
        f"WHERE organisation_entity = {org_entity} "
        f"ORDER BY name ASC LIMIT {limit}"
    )
    try:
        r = _req.get(
            f"{DATASETTE_BASE}/{database}.json",
            params={"sql": sql, "_shape": "array"},
            headers={"Accept": "application/json", "User-Agent": "PropSearch/1.0"},
            timeout=10,
        )
        if not r.ok:
            return []
        items = []
        for row in r.json():
            raw_json = row.get("json") or "{}"
            try:
                j = _json.loads(raw_json)
            except Exception:
                j = {}
            name = row.get("name") or j.get("name", "")
            if not name:
                continue
            entity_id = row.get("entity")
            items.append({
                "name": name,
                "doc_url": j.get("documentation-url") or j.get("document-url"),
                "notes": j.get("notes", ""),
                "entity_url": f"https://www.planning.data.gov.uk/entity/{entity_id}" if entity_id else None,
            })
        return items
    except Exception as e:
        log.warning(f"[planning] context {database} query failed: {e}")
        return []


def _fetch_context_count(database: str, org_entity: int) -> int:
    """Count planning constraint records for one LPA."""
    sql = f"SELECT count(*) as cnt FROM entity WHERE organisation_entity = {org_entity}"
    try:
        r = _req.get(
            f"{DATASETTE_BASE}/{database}.json",
            params={"sql": sql, "_shape": "array"},
            headers={"Accept": "application/json", "User-Agent": "PropSearch/1.0"},
            timeout=8,
        )
        if not r.ok:
            return 0
        rows = r.json()
        return int(rows[0]["cnt"]) if rows else 0
    except Exception:
        return 0


@planning_bp.route("/api/planning/context")
def planning_context():
    """
    Planning constraint context for a postcode's LPA.
    Returns conservation areas, article 4 directions, heritage at risk count,
    and brownfield site count — all fetched live from datasette. No DB writes.
    """
    postcode = request.args.get("postcode", "").strip()
    if not postcode:
        return jsonify({"error": "postcode is required"}), 400

    try:
        _, _, norm_pc, admin_district = _geocode(postcode)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    la_entity = _find_la_entity(admin_district)

    if not la_entity:
        return jsonify({
            "postcode": norm_pc,
            "council": admin_district,
            "la_entity": None,
            "conservation_areas": [],
            "article_4_directions": [],
            "heritage_at_risk_count": 0,
            "brownfield_count": 0,
            "coverage_note": (
                f"Council '{admin_district}' was not matched in the national planning dataset. "
                "The council may not yet have contributed constraint data."
            ),
        })

    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        f_ca   = pool.submit(_fetch_context_items, "conservation-area",       la_entity, 50)
        f_a4   = pool.submit(_fetch_context_items, "article-4-direction",     la_entity, 50)
        f_har  = pool.submit(_fetch_context_count, "heritage-at-risk",        la_entity)
        f_bf   = pool.submit(_fetch_context_count, "brownfield-site",         la_entity)
        f_tpo  = pool.submit(_fetch_context_count, "tree-preservation-order", la_entity)
        f_lb   = pool.submit(_fetch_context_count, "listed-building-outline", la_entity)
        conservation_areas   = f_ca.result()
        article_4_directions = f_a4.result()
        heritage_count       = f_har.result()
        brownfield_count     = f_bf.result()
        tpo_count            = f_tpo.result()
        listed_building_count = f_lb.result()

    return jsonify({
        "postcode": norm_pc,
        "council": admin_district,
        "la_entity": la_entity,
        "conservation_areas": conservation_areas,
        "article_4_directions": article_4_directions,
        "heritage_at_risk_count": heritage_count,
        "brownfield_count": brownfield_count,
        "tpo_count": tpo_count,
        "listed_building_count": listed_building_count,
        "coverage_note": (
            f"Planning constraints for {admin_district} from the national MHCLG planning dataset. "
            "Data is contributed by LPAs and may be incomplete — always verify with the council directly."
        ),
    })


# Pre-load the LA cache in the background so the first HTTP request doesn't
# block waiting for the datasette local-authority query.
threading.Thread(target=_load_la_cache, daemon=True).start()
