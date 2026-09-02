"""
Ownership Intelligence  —  person and company profile aggregation
=================================================================
GET /api/ownership/intelligence/person/<officer_id>?name=<name>
GET /api/ownership/intelligence/company/<company_number>?name=<name>
"""
import base64
import json as _json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

from flask import Blueprint, jsonify, request
from sqlalchemy import text

from database import engine

logger = logging.getLogger(__name__)
intelligence_bp = Blueprint("intelligence", __name__)

CH_BASE = "https://api.company-information.service.gov.uk"

_SOURCES = {
    "hmlr": "https://use-land-property-data.service.gov.uk/datasets/ccod",
    "ch": "https://find-and-update.company-information.service.gov.uk",
    "note": "HMLR data covers corporate-owned titles only (CCOD/OCOD).",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ch_get(url: str, creds: str, timeout: int = 10) -> dict:
    """Authenticated GET to the Companies House API; raises HTTPError on non-200."""
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Basic {creds}")
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return _json.loads(r.read())


def _is_overdue(profile: dict) -> bool:
    """True if company accounts are overdue."""
    try:
        nd = (profile.get("accounts") or {}).get("next_due")
        return bool(nd) and datetime.fromisoformat(nd).date() < date.today()
    except Exception:
        return False


def _within_months(date_str: str | None, months: int) -> bool:
    """True if date_str is within last N months."""
    if not date_str:
        return False
    try:
        d = datetime.fromisoformat(date_str.replace("Z", "+00:00")).date()
        today = date.today()
        dm = (today.year - d.year) * 12 + (today.month - d.month)
        return dm <= months
    except Exception:
        return False


def _query_hmlr_titles(company_numbers: list) -> list:
    """Query HMLR DB for all given company_numbers; returns list of title dicts."""
    if not company_numbers:
        return []
    try:
        with engine.connect() as conn:
            conn.execute(text("SET statement_timeout = '5s'"))
            placeholders = ", ".join(f":c{i}" for i, _ in enumerate(company_numbers))
            params = {f"c{i}": cn for i, cn in enumerate(company_numbers)}
            rows = conn.execute(text(f"""
                SELECT title_number, tenure, address, postcode, district, county, region,
                       source, company_name, company_number, date_added::text
                FROM company_ownership
                WHERE company_number IN ({placeholders})
                ORDER BY tenure, address NULLS LAST
            """), params).fetchall()
        return [
            {
                "title_number":   r[0],
                "tenure":         r[1],
                "address":        r[2],
                "postcode":       r[3],
                "district":       r[4],
                "county":         r[5],
                "region":         r[6],
                "source":         r[7],
                "company_name":   r[8],
                "company_number": r[9],
                "date_added":     r[10],
            }
            for r in rows
        ]
    except Exception as exc:
        logger.warning("[intelligence] HMLR title query failed: %s", exc)
        return []


def _query_title_counts(company_numbers: list) -> dict:
    """Return {company_number: count} for the given company numbers."""
    if not company_numbers:
        return {}
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
        return {r[0]: int(r[1]) for r in rows}
    except Exception as exc:
        logger.warning("[intelligence] title count query failed: %s", exc)
        return {}


def _compute_score_and_signals(
    company_profiles: dict,
    charges_by_cn: dict,
    titles: list,
    items: list,
) -> tuple:
    """
    Compute motivation_score (0-100) and signals list from aggregated data.
    Returns (score, signals, outstanding_charges, freehold_titles, leasehold_titles, regions).
    """
    score = 25
    signals = []

    active_cos = [
        p for p in company_profiles.values()
        if (p.get("company_status") or "").lower() == "active"
    ]
    inactive_cos = [
        p for p in company_profiles.values()
        if (p.get("company_status") or "").lower() != "active"
    ]

    if inactive_cos:
        score += min(20, len(inactive_cos) * 5)
        signals.append({
            "type":   "warning",
            "label":  f"{len(inactive_cos)} dissolved/struck-off companies",
            "detail": "Non-active entities indicate portfolio wind-down.",
            "source": "Companies House",
        })

    # Check overdue accounts
    overdue = [p for p in active_cos if _is_overdue(p)]
    if overdue:
        score += 10
        signals.append({
            "type":   "warning",
            "label":  f"{len(overdue)} companies with overdue accounts",
            "detail": "Late CH filings may indicate financial difficulties.",
            "source": "Companies House",
        })

    all_charges = [c for clist in charges_by_cn.values() for c in clist]
    outstanding = [c for c in all_charges if (c.get("status") or "").lower() == "outstanding"]
    recent_6m = [c for c in all_charges if _within_months(c.get("creation_on"), 6)]
    recent_12m = [c for c in all_charges if _within_months(c.get("creation_on"), 12)]

    if recent_6m:
        score += 15
        signals.append({
            "type":   "warning",
            "label":  f"{len(recent_6m)} charges registered in last 6 months",
            "detail": "Recent secured debt may indicate financing pressure.",
            "source": "Companies House Charges Register",
        })
    elif recent_12m:
        score += 10
        signals.append({
            "type":   "info",
            "label":  f"{len(recent_12m)} charges registered in last 12 months",
            "detail": "Recent secured borrowing on portfolio assets.",
            "source": "Companies House Charges Register",
        })

    total_titles = len(titles)
    if outstanding and total_titles > 0:
        pct = min(1.0, len(outstanding) / max(total_titles, 1))
        if pct > 0.5:
            score += 10
            signals.append({
                "type":   "info",
                "label":  (
                    f"High charge density — {len(outstanding)} outstanding charges "
                    f"across {total_titles} titles"
                ),
                "detail": f"~{round(pct * 100)}% of titles carry outstanding charges.",
                "source": "Companies House & HMLR",
            })

    # Portfolio contraction
    resigned = [i for i in items if i.get("resigned_on")]
    active_appts = [i for i in items if not i.get("resigned_on")]
    if resigned and active_appts and len(resigned) > len(active_appts) * 0.4:
        score += 10
        signals.append({
            "type":   "info",
            "label":  (
                f"Portfolio contraction — {len(resigned)} resigned vs "
                f"{len(active_appts)} active roles"
            ),
            "detail": "High proportion of resigned roles can signal wind-down.",
            "source": "Companies House Officers",
        })

    # Positive signals
    freehold = [t for t in titles if (t.get("tenure") or "").lower().startswith("f")]
    leasehold = [t for t in titles if (t.get("tenure") or "").lower().startswith("l")]

    if total_titles > 0 and len(outstanding) < total_titles * 0.3:
        score -= 5
        signals.append({
            "type":   "positive",
            "label":  (
                f"High equity — {total_titles - len(outstanding)} of {total_titles} "
                f"titles unencumbered"
            ),
            "detail": "Majority carry no outstanding registered charge.",
            "source": "Companies House & HMLR",
        })

    if not all_charges:
        score -= 5
        signals.append({
            "type":   "positive",
            "label":  "No registered charges found across associated companies",
            "detail": "Fully unencumbered based on available data.",
            "source": "Companies House Charges Register",
        })

    # Bona Vacantia: HMLR titles registered to dissolved companies
    dissolved_statuses = {"dissolved", "liquidation", "administration", "receivership"}
    dissolved_cns = {
        cn.upper() for cn, p in company_profiles.items()
        if (p.get("company_status") or "").lower() in dissolved_statuses
    }
    bv_titles = [
        t for t in titles
        if (t.get("company_number") or "").upper() in dissolved_cns
    ]
    if bv_titles:
        bv_companies = sorted({t.get("company_name") or t.get("company_number", "") for t in bv_titles})
        score += 25
        signals.insert(0, {
            "type":   "warning",
            "label":  f"Bona Vacantia — {len(bv_titles)} HMLR title(s) registered to dissolved company/companies",
            "detail": (
                f"HMLR records show {len(bv_titles)} title(s) legally registered to "
                f"{', '.join(bv_companies[:3])}{'...' if len(bv_companies) > 3 else ''} "
                f"which {'are' if len(bv_companies) > 1 else 'is'} dissolved. "
                "Under s.1012 Companies Act 2006, property of a dissolved company vests in the Crown "
                "as bona vacantia. The BVLS (Bona Vacantia & Lost Property Service, part of the Treasury Solicitor) "
                "can disclaim these assets. Titles cannot be transferred without either restoring the company "
                "to the register or obtaining a formal Crown disclaimer. This may affect receivability of title."
            ),
            "source": "Companies Act 2006 s.1012 / BVLS / HMLR",
        })

    if freehold or leasehold:
        dominant = "freehold" if len(freehold) >= len(leasehold) else "leasehold"
        signals.append({
            "type":   "info",
            "label":  (
                f"Predominantly {dominant} "
                f"({len(freehold)} freehold, {len(leasehold)} leasehold)"
            ),
            "detail": "HMLR CCOD/OCOD data. Corporate-owned titles only.",
            "source": "HM Land Registry CCOD/OCOD",
        })

    regions = {t.get("region") for t in titles if t.get("region")}
    if len(regions) > 2:
        signals.append({
            "type":   "info",
            "label":  f"Portfolio spans {len(regions)} regions",
            "detail": "Geographically diversified portfolio.",
            "source": "HM Land Registry",
        })

    score = max(0, min(100, score))
    return score, signals, outstanding, freehold, leasehold, regions


def _build_aml_risk(company_profiles: dict) -> dict:
    insolvency_statuses = {"dissolved", "liquidation", "administration", "receivership"}
    insolvency_events = len([
        cn for cn, p in company_profiles.items()
        if (p.get("company_status") or "").lower() in insolvency_statuses
    ])
    return {
        "insolvency_events": insolvency_events,
        "disqualified":      "not_checked",
        "adverse_media":     "not_checked",
        "sanctions":         "not_checked",
    }


def _build_underwriting_summary(
    name: str,
    total_titles: int,
    total_companies: int,
    outstanding_count: int,
    motivation_score: int,
    aml_risk: dict,
    regions: set,
) -> str:
    parts = []
    if name:
        parts.append(f"{name} is associated with {total_companies} company appointment(s)")
    else:
        parts.append(f"Subject has {total_companies} company appointment(s)")
    if total_titles:
        parts.append(f"with {total_titles} HMLR-registered title(s)")
    if outstanding_count:
        parts.append(f"{outstanding_count} outstanding registered charge(s)")
    if aml_risk.get("insolvency_events"):
        n = aml_risk["insolvency_events"]
        parts.append(f"{n} entity/entities in insolvency or dissolved status")
    if regions:
        parts.append(f"portfolio spread across {len(regions)} region(s)")
    parts.append(f"Motivation score: {motivation_score}/100")
    return ". ".join(parts) + "."


def _build_person_timeline(charges_by_cn: dict, items: list) -> list:
    """Build a sorted timeline of charge and appointment events for a person."""
    events = []

    for cn, charges in charges_by_cn.items():
        for c in charges:
            if c.get("created_on"):
                classification = c.get("classification") or {}
                detail = classification.get("description", "") if isinstance(classification, dict) else ""
                events.append({
                    "date":   c["created_on"],
                    "type":   "charge_created",
                    "label":  f"Charge registered on {cn}",
                    "detail": detail,
                    "source": "Companies House Charges Register",
                })
            if c.get("satisfied_on"):
                events.append({
                    "date":   c["satisfied_on"],
                    "type":   "charge_satisfied",
                    "label":  f"Charge satisfied on {cn}",
                    "detail": "",
                    "source": "Companies House Charges Register",
                })

    for item in items:
        at = item.get("appointed_to") or {}
        co_name = at.get("company_name", at.get("company_number", ""))
        role = item.get("officer_role", "")
        if item.get("appointed_on"):
            events.append({
                "date":   item["appointed_on"],
                "type":   "appointment",
                "label":  f"Appointed to {co_name}",
                "detail": role,
                "source": "Companies House Officers",
            })
        if item.get("resigned_on"):
            events.append({
                "date":   item["resigned_on"],
                "type":   "resignation",
                "label":  f"Resigned from {co_name}",
                "detail": role,
                "source": "Companies House Officers",
            })

    events.sort(key=lambda e: e.get("date") or "", reverse=True)
    return events[:50]


def _build_company_timeline(charges: list, incorporation_date: str | None) -> list:
    """Build a sorted timeline of charge and incorporation events for a company."""
    events = []
    for c in charges:
        created = c.get("creation_on") or c.get("created_on")
        if created:
            classification = c.get("classification") or {}
            detail = classification.get("description", "") if isinstance(classification, dict) else ""
            events.append({
                "date":   created,
                "type":   "charge_registered",
                "label":  "Charge registered",
                "detail": detail,
                "source": "Companies House Charges Register",
            })
        if c.get("satisfied_on"):
            events.append({
                "date":   c["satisfied_on"],
                "type":   "charge_satisfied",
                "label":  "Charge satisfied",
                "detail": "",
                "source": "Companies House Charges Register",
            })
    if incorporation_date:
        events.append({
            "date":   incorporation_date,
            "type":   "incorporation",
            "label":  "Company incorporated",
            "detail": "",
            "source": "Companies House",
        })
    events.sort(key=lambda e: e.get("date") or "", reverse=True)
    return events[:50]


def _extract_officer_id(officer: dict) -> str:
    """Pull the officer ID from a CH officer item's links."""
    links = officer.get("links") or {}
    officer_links = links.get("officer", {})
    appts_url = officer_links.get("appointments", "") if isinstance(officer_links, dict) else ""
    if not appts_url:
        return ""
    parts = appts_url.strip("/").split("/")
    return parts[1] if len(parts) >= 2 and parts[0] == "officers" else ""


# ---------------------------------------------------------------------------
# Person intelligence endpoint
# ---------------------------------------------------------------------------

@intelligence_bp.route("/api/ownership/intelligence/person/<officer_id>")
def person_intelligence(officer_id: str):
    """
    Aggregate person-level ownership intelligence from CH appointments and HMLR titles.
    """
    api_key = os.environ.get("COMPANIES_HOUSE_API_KEY", "").strip()
    if not api_key:
        return jsonify({"error": "COMPANIES_HOUSE_API_KEY not set", "error_type": "no_api_key"}), 200

    clean = re.sub(r"[^A-Za-z0-9_\-]", "", officer_id)
    if not clean:
        return jsonify({"error": "Invalid officer ID"}), 400

    name_param = request.args.get("name", "").strip()
    creds = base64.b64encode(f"{api_key}:".encode()).decode()

    # ------------------------------------------------------------------
    # Step 1: Paginate through all appointments (cap 200)
    # ------------------------------------------------------------------
    items: list = []
    total_results = 0
    start_index = 0
    page_size = 50
    person_name = name_param

    while True:
        try:
            data = _ch_get(
                f"{CH_BASE}/officers/{clean}/appointments"
                f"?items_per_page={page_size}&start_index={start_index}",
                creds, timeout=10,
            )
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return jsonify({"error": "Officer not found", "officer_id": clean}), 200
            return jsonify({"error": f"Companies House returned {e.code}"}), 200
        except Exception as exc:
            logger.exception("[intelligence] person appointments fetch error")
            return jsonify({"error": str(exc)}), 500

        page_items = data.get("items", [])

        if not items:
            total_results = data.get("total_results", 0)
            # Collect officer name from first item's name field or top-level name
            if not person_name:
                person_name = (
                    (page_items[0].get("name", "") if page_items else "")
                    or data.get("name", "")
                    or name_param
                )

        items.extend(page_items)
        start_index += len(page_items)
        if not page_items or start_index >= total_results or start_index >= 200:
            break

    # ------------------------------------------------------------------
    # Step 2: Collect company numbers; fetch profiles + charges for active
    # ------------------------------------------------------------------
    all_company_numbers = list({
        (i.get("appointed_to") or {}).get("company_number", "").upper()
        for i in items
        if (i.get("appointed_to") or {}).get("company_number")
    })

    active_items = [i for i in items if not i.get("resigned_on")]
    all_active_cns = list({
        (i.get("appointed_to") or {}).get("company_number", "").upper()
        for i in active_items
        if (i.get("appointed_to") or {}).get("company_number")
    })
    # Top 20 get full profile + charges; the rest get profile-only (for accurate status)
    full_fetch_cns   = all_active_cns[:20]
    status_only_cns  = all_active_cns[20:100]

    company_profiles: dict = {}   # cn -> profile dict
    charges_by_cn: dict = {}      # cn -> list of charge dicts

    def _fetch_company_data(cn: str):
        profile = {}
        charges = []
        try:
            profile = _ch_get(f"{CH_BASE}/company/{cn}", creds, timeout=8)
        except Exception:
            pass
        try:
            ch_data = _ch_get(f"{CH_BASE}/company/{cn}/charges", creds, timeout=8)
            charges = ch_data.get("items", [])
        except Exception:
            pass
        return cn, profile, charges

    def _fetch_profile_only(cn: str):
        try:
            return cn, _ch_get(f"{CH_BASE}/company/{cn}", creds, timeout=6)
        except Exception:
            return cn, {}

    if full_fetch_cns:
        with ThreadPoolExecutor(max_workers=5) as pool:
            for cn, profile, charges in pool.map(_fetch_company_data, full_fetch_cns):
                company_profiles[cn] = profile
                charges_by_cn[cn] = charges

    if status_only_cns:
        with ThreadPoolExecutor(max_workers=8) as pool:
            for cn, profile in pool.map(_fetch_profile_only, status_only_cns):
                if cn not in company_profiles:
                    company_profiles[cn] = profile

    # ------------------------------------------------------------------
    # Step 3: Query HMLR DB for all company numbers (not just active)
    # ------------------------------------------------------------------
    titles = _query_hmlr_titles(all_company_numbers)
    title_counts = _query_title_counts(all_company_numbers)

    # ------------------------------------------------------------------
    # Step 4: Motivation score and signals
    # ------------------------------------------------------------------
    score, signals, outstanding, freehold, leasehold, regions = _compute_score_and_signals(
        company_profiles, charges_by_cn, titles, items
    )

    # ------------------------------------------------------------------
    # Step 5: Build entities list
    # ------------------------------------------------------------------
    entities = []
    for item in items:
        at = item.get("appointed_to") or {}
        cn = at.get("company_number", "").upper()
        full_profile = company_profiles.get(cn, {})
        # Prefer the freshly-fetched full CH profile status — the appointments API
        # can return a stale/incorrect company_status for the appointed_to field.
        status = (
            full_profile.get("company_status")
            or at.get("company_status", "")
        )
        entities.append({
            "company_name":       full_profile.get("company_name") or at.get("company_name", ""),
            "company_number":     cn,
            "company_status":     status,
            "company_type":       full_profile.get("type") or at.get("company_type", ""),
            "role":               item.get("officer_role", ""),
            "appointed_on":       item.get("appointed_on"),
            "resigned_on":        item.get("resigned_on"),
            "title_count":        title_counts.get(cn, 0),
            "charge_count":       len([
                c for c in charges_by_cn.get(cn, [])
                if (c.get("status") or "").lower() == "outstanding"
            ]),
            "sic_codes":          company_profiles.get(cn, {}).get("sic_codes", []),
            "incorporation_date": company_profiles.get(cn, {}).get("date_of_creation"),
            "accounts_overdue":   _is_overdue(company_profiles.get(cn, {})),
            "ch_url": (
                f"https://find-and-update.company-information.service.gov.uk/company/{cn}"
                if cn else None
            ),
        })

    # Sort: active first (no resigned_on), then by title_count desc
    entities.sort(key=lambda e: (1 if e.get("resigned_on") else 0, -e.get("title_count", 0)))

    # ------------------------------------------------------------------
    # Step 6: Portfolio summary
    # ------------------------------------------------------------------
    regions_map: dict = {}
    for t in titles:
        r = t.get("region")
        if r:
            regions_map[r] = regions_map.get(r, 0) + 1

    active_appts = [i for i in items if not i.get("resigned_on")]

    portfolio_summary = {
        "total_titles":       len(titles),
        "freehold_count":     len(freehold),
        "leasehold_count":    len(leasehold),
        "mortgaged_count":    min(len(outstanding), len(titles)),
        "unencumbered_count": max(0, len(titles) - len(outstanding)),
        "total_companies":    total_results,
        "active_companies":   len(active_appts),
        "regions":            regions_map,
    }

    # ------------------------------------------------------------------
    # Step 7: Timeline
    # ------------------------------------------------------------------
    timeline = _build_person_timeline(charges_by_cn, items)

    # ------------------------------------------------------------------
    # Step 8: AML risk
    # ------------------------------------------------------------------
    aml_risk = _build_aml_risk(company_profiles)

    # ------------------------------------------------------------------
    # Step 9: Underwriting summary
    # ------------------------------------------------------------------
    underwriting_summary = _build_underwriting_summary(
        person_name,
        len(titles),
        total_results,
        len(outstanding),
        score,
        aml_risk,
        regions,
    )

    return jsonify({
        "type":                 "person",
        "officer_id":           clean,
        "name":                 person_name,
        "total_appointments":   total_results,
        "portfolio_summary":    portfolio_summary,
        "entities":             entities,
        "titles":               titles,
        "signals":              signals,
        "motivation_score":     score,
        "aml_risk":             aml_risk,
        "timeline":             timeline,
        "underwriting_summary": underwriting_summary,
        "sources":              _SOURCES,
    })


# ---------------------------------------------------------------------------
# Company intelligence endpoint
# ---------------------------------------------------------------------------

@intelligence_bp.route("/api/ownership/intelligence/company/<company_number>")
def company_intelligence(company_number: str):
    """
    Aggregate company-level ownership intelligence from CH profile, charges, officers
    and HMLR titles.
    """
    api_key = os.environ.get("COMPANIES_HOUSE_API_KEY", "").strip()
    if not api_key:
        return jsonify({"error": "COMPANIES_HOUSE_API_KEY not set", "error_type": "no_api_key"}), 200

    clean = re.sub(r"[^A-Za-z0-9]", "", company_number).upper()
    if not clean or len(clean) > 10:
        return jsonify({"error": "Invalid company number"}), 400

    name_param = request.args.get("name", "").strip()
    creds = base64.b64encode(f"{api_key}:".encode()).decode()

    # ------------------------------------------------------------------
    # Parallel fetch: profile, charges, officers
    # ------------------------------------------------------------------
    def _fetch_profile():
        try:
            return _ch_get(f"{CH_BASE}/company/{clean}", creds, timeout=10)
        except Exception:
            return {}

    def _fetch_charges():
        try:
            data = _ch_get(f"{CH_BASE}/company/{clean}/charges", creds, timeout=10)
            return data.get("items", [])
        except Exception:
            return []

    def _fetch_officers():
        try:
            data = _ch_get(
                f"{CH_BASE}/company/{clean}/officers?items_per_page=30",
                creds, timeout=10,
            )
            return data.get("items", [])
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=3) as pool:
        f_profile  = pool.submit(_fetch_profile)
        f_charges  = pool.submit(_fetch_charges)
        f_officers = pool.submit(_fetch_officers)
        profile  = f_profile.result()
        charges  = f_charges.result()
        officers = f_officers.result()

    ch_not_found = not profile
    # Do NOT bail early — company may be a registered society, charity, or LLP
    # not indexed under the same number on CH. Still return all HMLR data.

    # ------------------------------------------------------------------
    # HMLR titles for this company number
    # ------------------------------------------------------------------
    titles = _query_hmlr_titles([clean])

    # ------------------------------------------------------------------
    # Related entities via shared active officers (max 8 officers)
    # ------------------------------------------------------------------
    active_officers = [o for o in officers if not o.get("resigned_on")][:8]

    def _get_officer_appointments(officer: dict) -> tuple:
        oid = _extract_officer_id(officer)
        name = officer.get("name", "")
        if not oid:
            return name, []
        try:
            data = _ch_get(
                f"{CH_BASE}/officers/{oid}/appointments?items_per_page=50",
                creds, timeout=6,
            )
            return name, data.get("items", [])
        except Exception:
            return name, []

    # cn -> {company_name, via: set of officer names}
    related_company_map: dict = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        for officer_name, appts in pool.map(_get_officer_appointments, active_officers):
            for appt in appts:
                at = appt.get("appointed_to") or {}
                cn = at.get("company_number", "").upper()
                if cn and cn != clean:
                    if cn not in related_company_map:
                        related_company_map[cn] = {
                            "company_name": at.get("company_name", ""),
                            "company_status": at.get("company_status", ""),
                            "via": set(),
                        }
                    related_company_map[cn]["via"].add(officer_name)

    related_cns = list(related_company_map.keys())
    related_title_counts = _query_title_counts(related_cns)

    related_entities = [
        {
            "company_number": cn,
            "company_name":   related_company_map[cn]["company_name"],
            "company_status": related_company_map[cn]["company_status"],
            "via":            sorted(related_company_map[cn]["via"]),
            "title_count":    related_title_counts.get(cn, 0),
            "ch_url": f"https://find-and-update.company-information.service.gov.uk/company/{cn}",
        }
        for cn in related_cns
    ]
    related_entities.sort(key=lambda x: -x["title_count"])

    # ------------------------------------------------------------------
    # Motivation score and signals
    # ------------------------------------------------------------------
    # Only include profile when CH actually returned data (avoids false "inactive" signals)
    company_profiles = {clean: profile} if profile and profile.get("company_status") else {}
    charges_by_cn = {clean: charges}

    score, signals, outstanding, freehold, leasehold, regions = _compute_score_and_signals(
        company_profiles, charges_by_cn, titles, []
    )

    company_status = (profile.get("company_status") or "").lower()
    cessation_date = profile.get("date_of_cessation", "")

    # ------------------------------------------------------------------
    # Successor candidate search — when dissolved, find active companies
    # with the same registered name so both can be shown side-by-side
    # ------------------------------------------------------------------
    successor_candidates = []
    if company_status == "dissolved":
        search_name = (profile.get("company_name") or name_param or "").strip().upper()
        if search_name:
            try:
                from urllib.parse import quote as _quote
                search_data = _ch_get(
                    f"{CH_BASE}/search/companies?q={_quote(search_name)}&items_per_page=20",
                    creds, timeout=6,
                )
                for item in search_data.get("items", []):
                    cn = (item.get("company_number") or "").upper()
                    if cn == clean:
                        continue
                    if (item.get("company_status") or "").lower() != "active":
                        continue
                    if (item.get("company_name") or "").upper() != search_name:
                        continue
                    # Fetch full profile for this candidate
                    try:
                        succ_profile = _ch_get(f"{CH_BASE}/company/{cn}", creds, timeout=6)
                    except Exception:
                        succ_profile = item
                    addr = succ_profile.get("registered_office_address") or item.get("address") or {}
                    successor_candidates.append({
                        "company_number":    cn,
                        "company_name":      succ_profile.get("company_name") or item.get("company_name"),
                        "company_status":    succ_profile.get("company_status") or "active",
                        "company_type":      succ_profile.get("type") or item.get("company_type"),
                        "date_of_creation":  succ_profile.get("date_of_creation") or item.get("date_of_creation"),
                        "jurisdiction":      succ_profile.get("jurisdiction"),
                        "registered_office": ", ".join(filter(None, [
                            addr.get("premises"), addr.get("address_line_1"),
                            addr.get("address_line_2"), addr.get("locality"),
                            addr.get("postal_code"),
                        ])),
                        "sic_codes":         succ_profile.get("sic_codes", []),
                        "has_charges":       succ_profile.get("has_charges", False),
                        "accounts":          succ_profile.get("accounts"),
                        "officers_url":      f"{CH_BASE}/company/{cn}/officers",
                        "ch_url":            f"https://find-and-update.company-information.service.gov.uk/company/{cn}",
                    })
                    if len(successor_candidates) >= 5:
                        break
            except Exception:
                pass

    # Predecessor search — when active, find dissolved companies with the same name
    # so the user can see the full BV picture on the active company's profile too
    predecessor_companies = []
    if company_status == "active":
        search_name = (profile.get("company_name") or name_param or "").strip().upper()
        if search_name:
            try:
                from urllib.parse import quote as _quote_pred
                search_data = _ch_get(
                    f"{CH_BASE}/search/companies?q={_quote_pred(search_name)}&items_per_page=20",
                    creds, timeout=6,
                )
                for item in search_data.get("items", []):
                    cn = (item.get("company_number") or "").upper()
                    if cn == clean:
                        continue
                    if (item.get("company_status") or "").lower() not in {"dissolved", "liquidation"}:
                        continue
                    if (item.get("title") or item.get("company_name") or "").upper().strip() != search_name:
                        continue
                    # Fetch full profile so we have registered office, SIC codes, accounts, etc.
                    try:
                        pred_profile = _ch_get(f"{CH_BASE}/company/{cn}", creds, timeout=6)
                    except Exception:
                        pred_profile = item
                    pred_addr = pred_profile.get("registered_office_address") or item.get("address") or {}
                    pred_addr_parts = [
                        pred_addr.get("care_of"), pred_addr.get("premises"),
                        pred_addr.get("address_line_1"), pred_addr.get("address_line_2"),
                        pred_addr.get("locality"), pred_addr.get("region"),
                        pred_addr.get("postal_code"), pred_addr.get("country"),
                    ]
                    predecessor_companies.append({
                        "company_number":    cn,
                        "company_name":      pred_profile.get("company_name") or item.get("title"),
                        "company_status":    pred_profile.get("company_status") or item.get("company_status"),
                        "company_type":      pred_profile.get("type") or item.get("company_type"),
                        "date_of_creation":  pred_profile.get("date_of_creation") or item.get("date_of_creation"),
                        "date_of_cessation": pred_profile.get("date_of_cessation") or item.get("date_of_cessation"),
                        "jurisdiction":      pred_profile.get("jurisdiction"),
                        "registered_office": ", ".join(p for p in pred_addr_parts if p),
                        "sic_codes":         pred_profile.get("sic_codes", []),
                        "accounts":          pred_profile.get("accounts"),
                        "confirmation_statement": pred_profile.get("confirmation_statement"),
                        "address_snippet":   item.get("address_snippet", ""),
                        "ch_url": f"https://find-and-update.company-information.service.gov.uk/company/{cn}",
                    })
                    if len(predecessor_companies) >= 5:
                        break
            except Exception:
                pass

    # Bona Vacantia: this company is dissolved but holds HMLR titles
    if company_status == "dissolved" and titles:
        score = min(100, score + 30)
        signals.insert(0, {
            "type":   "warning",
            "label":  f"Bona Vacantia — dissolved company holds {len(titles)} HMLR registered title(s)",
            "detail": (
                f"This company was dissolved{' on ' + cessation_date if cessation_date else ''} "
                "yet remains the registered owner of HMLR land titles. "
                "Under s.1012 Companies Act 2006, all property of a dissolved company immediately vests in the Crown "
                "as bona vacantia. The BVLS (Bona Vacantia & Lost Property Service, part of the King's Solicitor) "
                "is the Crown's nominee for these assets and may disclaim them. "
                "These titles cannot lawfully be sold or mortgaged without either: (1) restoring the company to the "
                "register via Companies Act 2006 s.1029 and then transferring the asset, or (2) obtaining a formal "
                "Crown disclaimer. Buyers and lenders should seek specialist legal advice before transacting."
            ),
            "source": "Companies Act 2006 ss.1012–1013 / BVLS / HM Land Registry",
        })

    # ------------------------------------------------------------------
    # Portfolio summary
    # ------------------------------------------------------------------
    regions_map: dict = {}
    for t in titles:
        r = t.get("region")
        if r:
            regions_map[r] = regions_map.get(r, 0) + 1

    is_active = (profile.get("company_status") or "").lower() == "active"

    portfolio_summary = {
        "total_titles":       len(titles),
        "freehold_count":     len(freehold),
        "leasehold_count":    len(leasehold),
        "mortgaged_count":    min(len(outstanding), len(titles)),
        "unencumbered_count": max(0, len(titles) - len(outstanding)),
        "total_companies":    1,
        "active_companies":   1 if is_active else 0,
        "regions":            regions_map,
    }

    # ------------------------------------------------------------------
    # Timeline
    # ------------------------------------------------------------------
    incorporation_date = profile.get("date_of_creation")
    timeline = _build_company_timeline(charges, incorporation_date)

    # ------------------------------------------------------------------
    # AML risk
    # ------------------------------------------------------------------
    aml_risk = _build_aml_risk(company_profiles)

    # ------------------------------------------------------------------
    # Underwriting summary
    # ------------------------------------------------------------------
    company_name_display = (profile.get("company_name") if profile else None) or name_param or clean
    underwriting_summary = _build_underwriting_summary(
        company_name_display,
        len(titles),
        1,
        len(outstanding),
        score,
        aml_risk,
        regions,
    )

    return jsonify({
        "type":                 "company",
        "company_number":       clean,
        "company_name":         company_name_display,
        "ch_profile":           profile,
        "charges":              charges,
        "officers":             officers,
        "related_entities":     related_entities,
        "portfolio_summary":    portfolio_summary,
        "titles":               titles,
        "signals":              signals,
        "motivation_score":     score,
        "aml_risk":             aml_risk,
        "timeline":             timeline,
        "underwriting_summary": underwriting_summary,
        "ch_not_found":          ch_not_found,
        "bona_vacantia":         company_status == "dissolved" and len(titles) > 0,
        "cessation_date":        cessation_date or None,
        "successor_candidates":  successor_candidates,
        "predecessor_companies": predecessor_companies,
        "ch_url": f"https://find-and-update.company-information.service.gov.uk/company/{clean}",
        "sources":              _SOURCES,
    })


# ---------------------------------------------------------------------------
# Postcode price lookup  (used by the Properties tab in the frontend)
# ---------------------------------------------------------------------------

_LR_PRICE_HEADERS = {
    "Accept": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}


def _parse_lr_item(item: dict) -> dict:
    """Extract price, date, type, tenure, address fields from one LR transaction item."""
    from datetime import datetime as _dt

    price = item.get("pricePaid")

    date_raw = item.get("transactionDate")
    if isinstance(date_raw, dict):
        date_raw = date_raw.get("@value", "")
    date_str = None
    if date_raw:
        s = str(date_raw).strip()
        # ISO date already: "2014-03-20" or "2014-03-20T00:00:00"
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            date_str = s[:10]
        else:
            # LR sometimes returns pre-formatted strings like "Thu, 20 Mar 2014"
            for fmt in ("%a, %d %b %Y", "%d %b %Y", "%B %d, %Y", "%d/%m/%Y", "%Y/%m/%d"):
                try:
                    date_str = _dt.strptime(s, fmt).strftime("%Y-%m-%d")
                    break
                except ValueError:
                    pass
            if not date_str:
                # Last resort: keep the raw value so caller can still display something
                date_str = s

    def _prefLabel(obj):
        if not isinstance(obj, dict):
            return None
        lst = obj.get("prefLabel") or obj.get("label")
        if isinstance(lst, list) and lst:
            return lst[0].get("_value") if isinstance(lst[0], dict) else lst[0]
        if isinstance(lst, str):
            return lst
        raw = str(obj.get("_about", ""))
        return raw.split("/")[-1].replace("-", " ").capitalize() if raw else None

    pt = _prefLabel(item.get("propertyType") or {})
    ten = _prefLabel(item.get("estateType") or {})

    addr = item.get("propertyAddress") or {}
    paon = (addr.get("paon") or "").strip().upper()
    saon = (addr.get("saon") or "").strip().upper()
    street = (addr.get("street") or "").strip()
    town = (addr.get("town") or "").strip()
    addr_parts = [p for p in [saon, paon, street, town] if p]
    addr_str = ", ".join(addr_parts) or None

    new_build = item.get("newBuild")
    if isinstance(new_build, dict):
        new_build = new_build.get("_value")

    return {
        "price":         int(price) if price is not None else None,
        "date":          date_str,
        "address":       addr_str,
        "paon":          paon,
        "saon":          saon,
        "property_type": pt,
        "tenure":        ten,
        "new_build":     new_build,
    }


def _fetch_lr_price_one(pc: str) -> tuple:
    """Return (postcode, price_dict | None). One LR API call per postcode.
    Returns up to 10 recent transactions so the client can match by paon.
    """
    import requests as _req
    try:
        r = _req.get(
            "https://landregistry.data.gov.uk/data/ppi/transaction-record.json",
            params={
                "propertyAddress.postcode": pc,
                "_pageSize": 10,
                "_page": 0,
                "_sort": "-transactionDate",
            },
            headers=_LR_PRICE_HEADERS,
            timeout=6,
        )
        if not r.ok:
            logger.warning("[intelligence/prices] LR %d for %s", r.status_code, pc)
            return pc, None
        items = (r.json() or {}).get("result", {}).get("items", []) or []
        if not items:
            return pc, None

        # Build per-paon dict: most recent sale per building (items are date-sorted desc)
        properties: dict = {}
        for item in items:
            parsed = _parse_lr_item(item)
            paon = parsed["paon"]
            if paon and paon not in properties:
                properties[paon] = parsed

        # Postcode-level summary uses the overall most-recent transaction
        latest = _parse_lr_item(items[0])

        return pc, {
            "last_price":        latest["price"],
            "last_date":         latest["date"],
            "address":           latest["address"],
            "property_type":     latest["property_type"],
            "tenure":            latest["tenure"],
            "new_build":         latest["new_build"],
            "transaction_count": len(items),
            "source":            "HM Land Registry Price Paid Data",
            "source_url":        "https://landregistry.data.gov.uk/data/ppi",
            "properties":        properties,   # paon → most-recent sale for that building
        }
    except Exception as exc:
        logger.debug("[intelligence/prices] %s error: %s", pc, exc)
        return pc, None


@intelligence_bp.route("/api/ownership/intelligence/postcode-prices")
def postcode_prices():
    """
    Batch-fetch most recent sale price for up to 100 postcodes.
    ?postcodes=SW1A1AA,EC1A1BB  (comma-separated, un-spaced or spaced, max 100)
    Returns: {"SW1A 1AA": {last_price, last_date, ...}, ...}
    """
    raw = request.args.get("postcodes", "").strip()
    if not raw:
        return jsonify({}), 200

    def _fmt(pc: str) -> str | None:
        pc = pc.strip().upper().replace(" ", "")
        if len(pc) < 5:
            return None
        return pc[:-3] + " " + pc[-3:]

    postcodes = list({_fmt(p) for p in raw.split(",") if _fmt(p)})[:100]
    if not postcodes:
        return jsonify({}), 200

    result: dict = {}
    with ThreadPoolExecutor(max_workers=10) as pool:
        for pc, data in pool.map(_fetch_lr_price_one, postcodes):
            if data:
                result[pc] = data

    return jsonify(result)
