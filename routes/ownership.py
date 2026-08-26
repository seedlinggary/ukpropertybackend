"""
Company Ownership Search  — HMLR CCOD / OCOD
=============================================
GET /api/ownership/search?q=<query>&mode=name|reg&page=1&limit=20
GET /api/ownership/titles?reg=<reg_number>&name=<name>&page=1&limit=50
GET /api/ownership/by-address?postcode=<postcode>
GET /api/ownership/companies-house/<company_number>
GET /api/ownership/stats
"""
import logging
import os
import re
import time
import functools
import urllib.request
import urllib.error
import urllib.parse
import base64
import json as _json
from concurrent.futures import ThreadPoolExecutor
from flask import Blueprint, jsonify, request
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from database import engine

logger = logging.getLogger(__name__)
ownership_bp = Blueprint("ownership", __name__)


def _db_retry(max_retries=2, delay=0.75):
    """Retry a route on transient SSL/connection drops (e.g. during heavy index builds)."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            for attempt in range(max_retries + 1):
                try:
                    return fn(*args, **kwargs)
                except OperationalError as exc:
                    if attempt < max_retries:
                        logger.warning(
                            f"[ownership] DB disconnect on {fn.__name__} "
                            f"(attempt {attempt + 1}/{max_retries}), retrying in {delay}s — {exc}"
                        )
                        time.sleep(delay * (attempt + 1))
                    else:
                        raise
        return wrapper
    return decorator


# ---------------------------------------------------------------------------
# Table bootstrap  (called once on first request)
# ---------------------------------------------------------------------------

_TABLE_READY = False


def _ensure_table():
    global _TABLE_READY
    if _TABLE_READY:
        return
    try:
        with engine.connect() as conn:
            # Disable statement timeout for this session — GIN index builds on large
            # tables are slow and the pooler's default timeout would cancel them.
            conn.execute(text("SET statement_timeout = 0"))

            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS company_ownership (
                    id              BIGSERIAL PRIMARY KEY,
                    company_name    TEXT NOT NULL,
                    company_number  TEXT,
                    proprietor_cat  TEXT,
                    country         TEXT,
                    title_number    TEXT NOT NULL,
                    tenure          TEXT,
                    address         TEXT,
                    district        TEXT,
                    county          TEXT,
                    region          TEXT,
                    postcode        TEXT,
                    date_added      DATE,
                    source          TEXT DEFAULT 'CCOD'
                )
            """))
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS ownership_meta (
                    key   TEXT PRIMARY KEY,
                    value TEXT
                )
            """))

            # B-tree indexes only — GIN trigram index is managed exclusively by
            # the import script (_ensure_indexes) which runs after all data is
            # loaded with statement_timeout = 0.  Building it here would compete
            # with a running bulk import and could time out.
            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_co_number
                ON company_ownership(company_number)
            """))
            conn.execute(text("""
                CREATE INDEX IF NOT EXISTS idx_co_postcode
                ON company_ownership(postcode)
            """))

            conn.commit()
        _TABLE_READY = True
        logger.info("[ownership] Tables ready")
    except Exception as exc:
        logger.warning(f"[ownership] Table setup failed (will retry): {exc}")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@ownership_bp.route("/api/ownership/search")
@_db_retry()
def search_ownership():
    _ensure_table()
    q    = request.args.get("q", "").strip()
    mode = request.args.get("mode", "name")        # "name" | "reg"
    page = max(1, int(request.args.get("page", 1)))
    limit = min(50, max(1, int(request.args.get("limit", 20))))
    offset = (page - 1) * limit

    if not q or len(q) < 2:
        return jsonify({"error": "q must be at least 2 characters"}), 400

    try:
        with engine.connect() as conn:
            conn.execute(text("SET statement_timeout = 0"))
            if mode == "reg":
                rows = conn.execute(text("""
                    SELECT company_name, company_number, proprietor_cat, country,
                           COUNT(*) AS title_count, MIN(source) AS source
                    FROM company_ownership
                    WHERE company_number = :q
                    GROUP BY company_name, company_number, proprietor_cat, country
                    ORDER BY title_count DESC, company_name
                    LIMIT :lim OFFSET :off
                """), {"q": q, "lim": limit, "off": offset}).fetchall()

                total_row = conn.execute(text("""
                    SELECT COUNT(DISTINCT COALESCE(company_number, ''))
                    FROM company_ownership
                    WHERE company_number = :q
                """), {"q": q}).fetchone()
            else:
                # Name search — use company_name_suggest (top 100k, fast) if built,
                # otherwise fall back to company_name_index (938k, slower).
                # Use information_schema so the check never aborts the transaction
                # (a failed SELECT on a missing table puts PG in aborted-txn state).
                suggest_exists = conn.execute(text("""
                    SELECT EXISTS(
                        SELECT 1 FROM information_schema.tables
                        WHERE table_schema = 'public'
                          AND table_name   = 'company_name_suggest'
                    )
                """)).scalar()
                name_table = "company_name_suggest" if suggest_exists else "company_name_index"

                rows = conn.execute(text(f"""
                    WITH top_names AS (
                        SELECT company_name, title_count
                        FROM {name_table}
                        WHERE company_name ILIKE :pat
                        ORDER BY title_count DESC
                        LIMIT :lim OFFSET :off
                    )
                    SELECT
                        s.company_name,
                        MAX(s.company_number)   AS company_number,
                        MIN(s.proprietor_cat)   AS proprietor_cat,
                        MIN(s.country)          AS country,
                        n.title_count,
                        MIN(s.source)           AS source
                    FROM company_ownership_summary s
                    JOIN top_names n ON s.company_name = n.company_name
                    GROUP BY s.company_name, n.title_count
                    ORDER BY n.title_count DESC, s.company_name
                """), {"pat": f"%{q}%", "lim": limit, "off": offset}).fetchall()

                total_row = conn.execute(text(f"""
                    SELECT COUNT(*) FROM {name_table} WHERE company_name ILIKE :pat
                """), {"pat": f"%{q}%"}).fetchone()

        companies = [
            {
                "company_name":   r[0],
                "company_number": r[1],
                "proprietor_cat": r[2],
                "country":        r[3],
                "title_count":    r[4],
                "source":         r[5],
            }
            for r in rows
        ]
        total = int(total_row[0]) if total_row else 0

        return jsonify({
            "companies": companies,
            "total":     total,
            "page":      page,
            "has_more":  (offset + limit) < total,
        })

    except Exception as exc:
        logger.exception("[ownership] search error")
        return jsonify({"error": str(exc)}), 500


@ownership_bp.route("/api/ownership/titles")
@_db_retry()
def get_titles():
    _ensure_table()
    reg  = request.args.get("reg",  "").strip()
    name = request.args.get("name", "").strip()
    page  = max(1, int(request.args.get("page", 1)))
    limit = min(100, max(1, int(request.args.get("limit", 50))))
    offset = (page - 1) * limit

    if not reg and not name:
        return jsonify({"error": "reg or name param required"}), 400

    try:
        with engine.connect() as conn:
            conn.execute(text("SET statement_timeout = 0"))
            if reg and name:
                # Use name ILIKE as the sole filter — HMLR company numbers are
                # unreliable (missing leading zeros, reused across unrelated companies)
                # so adding OR company_number = :reg contaminates results with
                # unrelated companies that happen to share the same HMLR number.
                # name ILIKE already catches all records for the company regardless
                # of whether individual rows have a company_number or not.
                rows = conn.execute(text("""
                    SELECT title_number, tenure, address, postcode, district,
                           county, region, source, company_name,
                           date_added::text
                    FROM company_ownership
                    WHERE company_name ILIKE :name
                    ORDER BY address NULLS LAST
                    LIMIT :lim OFFSET :off
                """), {"name": name, "lim": limit, "off": offset}).fetchall()

                total_row = conn.execute(text("""
                    SELECT COUNT(*) FROM company_ownership
                    WHERE company_name ILIKE :name
                """), {"name": name}).fetchone()
            elif reg:
                rows = conn.execute(text("""
                    SELECT title_number, tenure, address, postcode, district,
                           county, region, source, company_name,
                           date_added::text
                    FROM company_ownership
                    WHERE company_number = :reg
                    ORDER BY address NULLS LAST
                    LIMIT :lim OFFSET :off
                """), {"reg": reg, "lim": limit, "off": offset}).fetchall()

                total_row = conn.execute(text("""
                    SELECT COUNT(*) FROM company_ownership WHERE company_number = :reg
                """), {"reg": reg}).fetchone()
            else:
                # Exact name match for the titles view
                rows = conn.execute(text("""
                    SELECT title_number, tenure, address, postcode, district,
                           county, region, source, company_name,
                           date_added::text
                    FROM company_ownership
                    WHERE company_name ILIKE :name
                    ORDER BY address NULLS LAST
                    LIMIT :lim OFFSET :off
                """), {"name": name, "lim": limit, "off": offset}).fetchall()

                total_row = conn.execute(text("""
                    SELECT COUNT(*) FROM company_ownership
                    WHERE company_name ILIKE :name
                """), {"name": name}).fetchone()

        titles = [
            {
                "title_number": r[0],
                "tenure":       r[1],
                "address":      r[2],
                "postcode":     r[3],
                "district":     r[4],
                "county":       r[5],
                "region":       r[6],
                "source":       r[7],
                "company_name": r[8],
                "date_added":   r[9],
            }
            for r in rows
        ]
        total = int(total_row[0]) if total_row else 0

        return jsonify({
            "titles":   titles,
            "total":    total,
            "page":     page,
            "has_more": (offset + limit) < total,
        })

    except Exception as exc:
        logger.exception("[ownership] titles error")
        return jsonify({"error": str(exc)}), 500


@ownership_bp.route("/api/ownership/suggest")
@_db_retry()
def suggest_ownership():
    """Return up to N distinct company names matching a partial query — used for autocomplete."""
    _ensure_table()
    q     = request.args.get("q", "").strip()
    limit = min(10, max(1, int(request.args.get("limit", 8))))

    if len(q) < 2:
        return jsonify({"suggestions": []})

    # Try company_name_suggest (top 100k, fast) first; fall back to full index.
    # Each attempt uses its own connection so a failed query doesn't abort
    # the transaction for the fallback.
    try:
        with engine.connect() as conn:
            conn.execute(text("SET statement_timeout = 0"))
            rows = conn.execute(text("""
                SELECT company_name, title_count
                FROM company_name_suggest
                WHERE company_name ILIKE :pat
                ORDER BY title_count DESC
                LIMIT :lim
            """), {"pat": f"%{q}%", "lim": limit}).fetchall()
    except Exception:
        try:
            with engine.connect() as conn:
                conn.execute(text("SET statement_timeout = 0"))
                rows = conn.execute(text("""
                    SELECT company_name, title_count
                    FROM company_name_index
                    WHERE company_name ILIKE :pat
                    ORDER BY title_count DESC
                    LIMIT :lim
                """), {"pat": f"%{q}%", "lim": limit}).fetchall()
        except Exception as exc:
            logger.exception("[ownership] suggest error")
            return jsonify({"suggestions": [], "error": str(exc)})

    return jsonify({"suggestions": [{"name": r[0], "title_count": int(r[1])} for r in rows]})


_UK_PC_RE = re.compile(r'\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b', re.IGNORECASE)


def _extract_house_number(address: str) -> str | None:
    """
    Pull the primary building number out of a UK address string.
    Strips flat/floor/unit prefixes so "Flat 3, 53 High St" → "53".
    Returns None if no number can be found (e.g. named buildings like "Knightsbridge Court").
    """
    if not address:
        return None
    stripped = re.sub(
        r'^(flat|apartment|unit|floor|suite|room|ground floor|first floor|'
        r'second floor|third floor|lower ground|upper ground)\s+\S+[,\s]+',
        "", address.strip(), flags=re.IGNORECASE,
    ).strip()
    m = re.match(r'^(\d+[a-zA-Z]?)', stripped)
    return m.group(1) if m else None


def _extract_postcode(address: str) -> str | None:
    """Extract a formatted UK postcode from a free-text address string (e.g. Mapbox place_name)."""
    if not address:
        return None
    m = _UK_PC_RE.search(address)
    if not m:
        return None
    raw = m.group(1).upper().replace(" ", "")
    return (raw[:-3] + " " + raw[-3:]) if len(raw) >= 5 else None


def _run_ownership_query(conn, sql_template: str, params: dict) -> list:
    return conn.execute(text(sql_template), params).fetchall()


@ownership_bp.route("/api/ownership/by-address")
@_db_retry()
def ownership_by_address():
    """Find companies owning a title at the given postcode (for the map-click ownership lookup).

    Strategy (in priority order):
    1. Query the property-data postcode filtered to the house number from the address hint.
    2. Also query any different postcode embedded in the address hint (Mapbox place_name
       includes the postcode — e.g. "Knightsbridge Court, Sloane Street, London SW1X 9LQ").
       This catches the common case where the EPC postcode differs from the HMLR postcode.
    3. Deduplicate results by title_number.
    4. If nothing is found after steps 1+2, fall back to the full unfiltered postcode set.
    """
    _ensure_table()
    raw          = request.args.get("postcode", "").strip().upper().replace(" ", "")
    address_hint = request.args.get("address",  "").strip()

    if not raw:
        return jsonify({"matches": []})

    formatted     = (raw[:-3] + " " + raw[-3:]) if len(raw) >= 5 else raw
    house_num     = _extract_house_number(address_hint)
    address_pc    = _extract_postcode(address_hint)   # postcode embedded in Mapbox place_name

    # All postcodes to search — deduplicated, both are indexed so cost is low
    postcodes = list(dict.fromkeys(pc for pc in [formatted, address_pc] if pc))

    _BASE_SQL = """
        SELECT
            title_number,
            tenure,
            MIN(address)                                              AS address,
            source,
            MAX(date_added)::text                                     AS date_added,
            COUNT(*)                                                  AS proprietor_count,
            STRING_AGG(company_name,    ' | ' ORDER BY company_name) AS company_names,
            STRING_AGG(COALESCE(company_number,''), ' | '
                       ORDER BY company_name)                         AS company_numbers,
            MIN(proprietor_cat)                                       AS proprietor_cat,
            MIN(country)                                              AS country
        FROM company_ownership
        WHERE postcode = ANY(:pcs) {extra}
        GROUP BY title_number, tenure, source
        ORDER BY
            CASE WHEN tenure ILIKE 'f%%' THEN 0 ELSE 1 END,
            MAX(date_added) DESC NULLS LAST,
            title_number
        LIMIT 20
    """

    try:
        with engine.connect() as conn:
            conn.execute(text("SET statement_timeout = 0"))

            filtered = False
            if house_num:
                num_space = f"{house_num} %"
                num_comma = f"{house_num},%"
                num_mid   = f"%, {house_num} %"
                extra_clause = (
                    "AND (address ILIKE :ns OR address ILIKE :nc OR address ILIKE :nm)"
                )
                rows = _run_ownership_query(conn, _BASE_SQL.format(extra=extra_clause), {
                    "pcs": postcodes, "ns": num_space, "nc": num_comma, "nm": num_mid,
                })
                if rows:
                    filtered = True
                else:
                    rows = _run_ownership_query(conn, _BASE_SQL.format(extra=""), {"pcs": postcodes})
            else:
                rows = _run_ownership_query(conn, _BASE_SQL.format(extra=""), {"pcs": postcodes})

        matches = [
            {
                "title_number":     r[0],
                "tenure":           r[1],
                "address":          r[2],
                "source":           r[3],
                "date_added":       r[4],
                "proprietor_count": int(r[5]),
                "company_names":    [n.strip() for n in (r[6] or "").split("|") if n.strip()],
                "company_numbers":  [n.strip() or None for n in (r[7] or "").split("|")],
                "proprietor_cat":   r[8],
                "country":          r[9],
            }
            for r in rows
        ]
        return jsonify({
            "matches":  matches,
            "postcode": formatted,
            "filtered": filtered,   # True = matched by house number; False = full postcode fallback
        })
    except Exception as exc:
        logger.exception("[ownership] by-address error")
        return jsonify({"matches": [], "error": str(exc)})


def _ch_get(url: str, creds: str, timeout: int = 10) -> dict:
    """Authenticated GET to the Companies House API; raises HTTPError on non-200."""
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Basic {creds}")
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return _json.loads(r.read())


def _ch_resolve_name(name: str, creds: str) -> str | None:
    """
    Search CH by company name and return the company_number on an exact
    case-insensitive name match, or None.

    Only returns on exact match — no first-result fallback.  HMLR names are
    reliable; CH numbers are not.  Using the first non-exact result risks
    resolving to a completely different company with a similar name.
    """
    qs = urllib.parse.urlencode({"q": name, "items_per_page": "5"})
    try:
        data = _ch_get(
            f"https://api.company-information.service.gov.uk/search/companies?{qs}",
            creds, timeout=8,
        )
    except Exception:
        return None
    name_upper = name.upper().strip()
    for item in data.get("items", []):
        if item.get("title", "").upper().strip() == name_upper:
            return item.get("company_number")
    return None


@ownership_bp.route("/api/ownership/companies-house/<company_number>")
def companies_house_lookup(company_number: str):
    """Proxy to Companies House free API — requires COMPANIES_HOUSE_API_KEY in .env.
    Accepts optional ?name= param; used as fallback when the HMLR number returns 404
    (HMLR frequently omits leading zeros or stores incorrect registration numbers).
    """
    api_key = os.environ.get("COMPANIES_HOUSE_API_KEY", "").strip()
    if not api_key:
        return jsonify({"error": "COMPANIES_HOUSE_API_KEY not set", "error_type": "no_api_key"}), 200

    clean = re.sub(r"[^A-Z0-9]", "", company_number.upper())
    if not clean or len(clean) > 10:
        return jsonify({"error": "Invalid company number"}), 400

    name  = request.args.get("name", "").strip()
    creds = base64.b64encode(f"{api_key}:".encode()).decode()

    # Name is the reliable identifier; HMLR numbers are not.
    # When a name is supplied, resolve via CH name search first so we always
    # fetch the correct company — even when the HMLR number belongs to an
    # entirely different company on CH (a known data quality issue in HMLR).
    if name:
        resolved = _ch_resolve_name(name, creds)
        lookup_num = resolved if resolved else clean
    else:
        lookup_num = clean

    def _fetch(num):
        return _ch_get(
            f"https://api.company-information.service.gov.uk/company/{num}",
            creds,
        )

    try:
        return jsonify(_fetch(lookup_num))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return jsonify({"error": "Invalid API key", "error_type": "auth_error"}), 200
        if e.code == 404:
            return jsonify({"error": "Company not found", "error_type": "not_found"}), 200
        return jsonify({"error": f"Companies House returned {e.code}", "error_type": "api_error"}), 200
    except Exception as exc:
        logger.exception("[ownership] companies-house lookup error")
        return jsonify({"error": str(exc), "error_type": "server_error"}), 200


@ownership_bp.route("/api/ownership/companies-house/<company_number>/charges")
def companies_house_charges(company_number: str):
    """Proxy to Companies House charges API — requires COMPANIES_HOUSE_API_KEY in .env.
    Same name-fallback logic as companies_house_lookup.
    """
    api_key = os.environ.get("COMPANIES_HOUSE_API_KEY", "").strip()
    if not api_key:
        return jsonify({"error": "COMPANIES_HOUSE_API_KEY not set", "error_type": "no_api_key", "items": []}), 200

    clean = re.sub(r"[^A-Z0-9]", "", company_number.upper())
    if not clean or len(clean) > 10:
        return jsonify({"error": "Invalid company number", "items": []}), 400

    name  = request.args.get("name", "").strip()
    creds = base64.b64encode(f"{api_key}:".encode()).decode()

    # Same name-first resolution as the company profile endpoint.
    if name:
        resolved = _ch_resolve_name(name, creds)
        lookup_num = resolved if resolved else clean
    else:
        lookup_num = clean

    def _fetch(num):
        return _ch_get(
            f"https://api.company-information.service.gov.uk/company/{num}/charges",
            creds,
        )

    try:
        return jsonify(_fetch(lookup_num))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return jsonify({"error": "Invalid API key", "error_type": "auth_error", "items": []}), 200
        if e.code == 404:
            return jsonify({"items": [], "total_count": 0}), 200
        return jsonify({"error": f"Companies House returned {e.code}", "error_type": "api_error", "items": []}), 200
    except Exception as exc:
        logger.exception("[ownership] companies-house charges error")
        return jsonify({"error": str(exc), "error_type": "server_error", "items": []}), 200


@ownership_bp.route("/api/ownership/ch-company-search")
def ch_company_search():
    """Search Companies House company registry by name — used as a fallback when HMLR has 0 results."""
    api_key = os.environ.get("COMPANIES_HOUSE_API_KEY", "").strip()
    if not api_key:
        return jsonify({"error": "no_api_key", "items": [], "total": 0}), 200

    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify({"items": [], "total": 0}), 200

    creds = base64.b64encode(f"{api_key}:".encode()).decode()
    try:
        qs_str = urllib.parse.urlencode({"q": q, "items_per_page": "20"})
        data = _ch_get(
            f"https://api.company-information.service.gov.uk/search/companies?{qs_str}",
            creds, timeout=8,
        )
        items = []
        for item in data.get("items", []):
            items.append({
                "company_name":     item.get("title", ""),
                "company_number":   item.get("company_number", ""),
                "company_status":   item.get("company_status", ""),
                "company_type":     item.get("company_type", ""),
                "date_of_creation": item.get("date_of_creation"),
                "address_snippet":  item.get("address_snippet", ""),
            })
        return jsonify({"items": items, "total": data.get("total_results", len(items))})
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return jsonify({"error": "auth_error", "items": [], "total": 0}), 200
        return jsonify({"error": f"CH returned {e.code}", "items": [], "total": 0}), 200
    except Exception as exc:
        logger.exception("[ownership] ch-company-search error")
        return jsonify({"error": str(exc), "items": [], "total": 0}), 200


@ownership_bp.route("/api/ownership/stats")
@_db_retry()
def get_stats():
    _ensure_table()
    try:
        with engine.connect() as conn:
            conn.execute(text("SET statement_timeout = 0"))
            # Use cached counts from ownership_meta — avoids full table scan on 4.5M rows
            meta = {
                row[0]: row[1]
                for row in conn.execute(text("SELECT key, value FROM ownership_meta")).fetchall()
            }

        return jsonify({
            "total":       int(meta.get("total_count", 0)),
            "ccod_count":  int(meta.get("ccod_count", 0)),
            "ocod_count":  int(meta.get("ocod_count", 0)),
            "last_import": meta.get("last_import"),
        })
    except Exception as exc:
        logger.exception("[ownership] stats error")
        return jsonify({"error": str(exc), "total": 0, "last_import": None})


# ---------------------------------------------------------------------------
# Person / officer name search  (via Companies House officers search API)
# ---------------------------------------------------------------------------

@ownership_bp.route("/api/ownership/person-search")
def person_search():
    """Search Companies House for persons (officers/directors) by name."""
    api_key = os.environ.get("COMPANIES_HOUSE_API_KEY", "").strip()
    if not api_key:
        return jsonify({"error": "COMPANIES_HOUSE_API_KEY not set", "error_type": "no_api_key", "officers": []}), 200

    q = request.args.get("q", "").strip()
    if not q or len(q) < 2:
        return jsonify({"error": "q must be at least 2 characters", "officers": []}), 400

    creds = base64.b64encode(f"{api_key}:".encode()).decode()
    try:
        qs_str = urllib.parse.urlencode({"q": q, "items_per_page": "20"})
        data = _ch_get(
            f"https://api.company-information.service.gov.uk/search/officers?{qs_str}",
            creds, timeout=8,
        )
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return jsonify({"error": "Invalid API key", "error_type": "auth_error", "officers": []}), 200
        return jsonify({"error": f"Companies House returned {e.code}", "error_type": "api_error", "officers": []}), 200
    except Exception as exc:
        logger.exception("[ownership] person-search error")
        return jsonify({"error": str(exc), "error_type": "server_error", "officers": []}), 200

    items = data.get("items", [])
    officers = []
    for item in items:
        dob = item.get("date_of_birth") or {}
        # CH search API returns links.self = "/officers/<id>/appointments"
        self_url = (item.get("links") or {}).get("self", "")
        officer_id = ""
        if self_url:
            parts = self_url.strip("/").split("/")
            # ["officers", "<id>", "appointments"]
            if len(parts) >= 2 and parts[0] == "officers":
                officer_id = parts[1]
        officers.append({
            "name":            item.get("title", ""),
            "description":     item.get("description", ""),
            "address_snippet": item.get("address_snippet", ""),
            "dob_month":       dob.get("month"),
            "dob_year":        dob.get("year"),
            "officer_id":      officer_id,
        })

    return jsonify({
        "officers": officers,
        "total":    data.get("total_results", len(items)),
    })


@ownership_bp.route("/api/ownership/person-appointments/<officer_id>")
def person_appointments(officer_id: str):
    """Return all company appointments for a CH officer (paginated), enriched with title counts."""
    api_key = os.environ.get("COMPANIES_HOUSE_API_KEY", "").strip()
    if not api_key:
        return jsonify({"error": "no_api_key", "items": []}), 200

    clean = re.sub(r"[^A-Za-z0-9_\-]", "", officer_id)
    if not clean:
        return jsonify({"error": "Invalid officer ID", "items": []}), 400

    creds = base64.b64encode(f"{api_key}:".encode()).decode()

    # Paginate through all appointments — CH defaults to ~35 per page
    items: list = []
    total_results = 0
    start_index = 0
    page_size = 50
    while True:
        try:
            data = _ch_get(
                f"https://api.company-information.service.gov.uk/officers/{clean}/appointments"
                f"?items_per_page={page_size}&start_index={start_index}",
                creds, timeout=10,
            )
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return jsonify({"items": [], "total_results": 0}), 200
            return jsonify({"error": f"CH returned {e.code}", "items": []}), 200
        except Exception as exc:
            logger.exception("[ownership] person-appointments error")
            return jsonify({"error": str(exc), "items": []}), 200

        page_items = data.get("items", [])
        if not items:
            total_results = data.get("total_results", 0)
        items.extend(page_items)
        start_index += len(page_items)
        # Stop when we have all results or hit the cap (200 to protect against abuse)
        if not page_items or start_index >= total_results or start_index >= 200:
            break
    company_numbers = list({
        (i.get("appointed_to") or {}).get("company_number", "").upper()
        for i in items
        if (i.get("appointed_to") or {}).get("company_number")
    })

    title_counts: dict = {}
    if company_numbers:
        try:
            with engine.connect() as conn:
                conn.execute(text("SET statement_timeout = '5s'"))
                placeholders = ", ".join(f":c{i}" for i, _ in enumerate(company_numbers))
                params = {f"c{i}": cn for i, cn in enumerate(company_numbers)}
                rows = conn.execute(text(f"""
                    SELECT company_number, COUNT(*) AS cnt
                    FROM company_ownership
                    WHERE company_number IN ({placeholders})
                    GROUP BY company_number
                """), params).fetchall()
                title_counts = {r[0]: int(r[1]) for r in rows}
        except Exception as exc:
            logger.warning("[ownership] person-appointments title count query failed: %s", exc)

    result_items = []
    for item in items:
        at = item.get("appointed_to") or {}
        cn = at.get("company_number", "").upper()
        result_items.append({
            "company_name":         at.get("company_name", ""),
            "company_number":       cn,
            "company_status":       at.get("company_status", ""),
            "company_type":         at.get("company_type", ""),
            "role":                 item.get("officer_role", ""),
            "appointed_on":         item.get("appointed_on"),
            "resigned_on":          item.get("resigned_on"),
            "nationality":          item.get("nationality"),
            "occupation":           item.get("occupation"),
            "country_of_residence": item.get("country_of_residence"),
            "is_pre_1992":          item.get("is_pre_1992_appointment", False),
            "title_count":          title_counts.get(cn, 0),
        })

    return jsonify({
        "items":         result_items,
        "total_results": data.get("total_results", len(items)),
    })


# ---------------------------------------------------------------------------
# Company network — related entities via shared directors/officers
# ---------------------------------------------------------------------------

@ownership_bp.route("/api/ownership/company-network/<company_number>")
def company_network(company_number: str):
    """
    Return all companies related to <company_number> via shared active officers.
    Steps:
      1. Fetch company officers from CH (active only, up to 15)
      2. For each officer, fetch their other appointments concurrently (max 5 workers)
      3. Deduplicate related company_numbers, query DB for title counts
      4. Sort by title_count desc
    """
    api_key = os.environ.get("COMPANIES_HOUSE_API_KEY", "").strip()
    if not api_key:
        return jsonify({"error": "no_api_key", "error_type": "no_api_key", "related": []}), 200

    clean = re.sub(r"[^A-Za-z0-9]", "", company_number).upper()
    if not clean:
        return jsonify({"error": "Invalid company number", "related": []}), 400

    creds = base64.b64encode(f"{api_key}:".encode()).decode()

    # Step 1: officers for this company
    try:
        officers_data = _ch_get(
            f"https://api.company-information.service.gov.uk/company/{clean}/officers"
            f"?items_per_page=20&register_type=directors",
            creds, timeout=8,
        )
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return jsonify({"related": [], "officer_count": 0, "company_number": clean}), 200
        return jsonify({"error": f"CH returned {e.code}", "related": []}), 200
    except Exception as exc:
        logger.warning("[ownership] company-network officers fetch failed: %s", exc)
        return jsonify({"error": str(exc), "related": []}), 200

    all_officers = officers_data.get("items", [])
    # Active officers only (no resigned_on), cap at 15 to limit downstream calls
    active_officers = [o for o in all_officers if not o.get("resigned_on")][:15]

    def _extract_officer_id(officer: dict) -> str:
        links = officer.get("links") or {}
        officer_links = links.get("officer", {})
        appts_url = officer_links.get("appointments", "") if isinstance(officer_links, dict) else ""
        if not appts_url:
            return ""
        parts = appts_url.strip("/").split("/")
        return parts[1] if len(parts) >= 2 and parts[0] == "officers" else ""

    def _get_appointments(officer: dict) -> tuple:
        name = officer.get("name", "")
        role = officer.get("officer_role", "")
        oid = _extract_officer_id(officer)
        if not oid:
            return name, role, []
        try:
            data = _ch_get(
                f"https://api.company-information.service.gov.uk/officers/{oid}/appointments"
                f"?items_per_page=50",
                creds, timeout=6,
            )
            return name, role, data.get("items", [])
        except Exception:
            return name, role, []

    # Step 2: concurrent appointment fetches
    # related: company_number -> {company_name, company_status, via: set of officer names}
    related: dict = {}

    with ThreadPoolExecutor(max_workers=5) as pool:
        for officer_name, officer_role, appts in pool.map(_get_appointments, active_officers):
            for appt in appts:
                at = appt.get("appointed_to") or {}
                cn = at.get("company_number", "").upper()
                if not cn or cn == clean:
                    continue
                if cn not in related:
                    related[cn] = {
                        "company_name":   at.get("company_name", ""),
                        "company_number": cn,
                        "company_status": at.get("company_status", ""),
                        "via": set(),
                    }
                related[cn]["via"].add(officer_name)

    # Step 3: single DB query for title counts
    company_numbers = list(related.keys())
    title_counts: dict = {}
    if company_numbers:
        try:
            with engine.connect() as conn:
                conn.execute(text("SET statement_timeout = '5s'"))
                placeholders = ", ".join(f":c{i}" for i, _ in enumerate(company_numbers))
                params = {f"c{i}": cn for i, cn in enumerate(company_numbers)}
                rows = conn.execute(text(f"""
                    SELECT company_number, COUNT(*) AS cnt
                    FROM company_ownership
                    WHERE company_number IN ({placeholders})
                    GROUP BY company_number
                """), params).fetchall()
                title_counts = {r[0]: int(r[1]) for r in rows}
        except Exception as exc:
            logger.warning("[ownership] company-network title count query failed: %s", exc)

    # Step 4: build and sort result
    result = []
    for cn, info in related.items():
        result.append({
            "company_name":   info["company_name"],
            "company_number": cn,
            "company_status": info["company_status"],
            "via":            sorted(info["via"]),
            "title_count":    title_counts.get(cn, 0),
        })
    result.sort(key=lambda x: (-x["title_count"], x["company_name"]))

    return jsonify({
        "related":       result,
        "officer_count": len(active_officers),
        "company_number": clean,
    })
