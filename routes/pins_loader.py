"""
Planning Inspectorate (PINS) appeals data loader.

Downloads the two GOV.UK Excel files (recent 5yr + older 5yr) in a background
thread on app startup, parses them into memory, and exposes a fast in-memory
search by ONS LPA code.

No data is stored in the database — everything lives in process memory.
Data covers ~10 years of decided planning appeals for every council in England.
"""

import io
import logging
import re
import threading

import openpyxl
import requests as _req

log = logging.getLogger(__name__)

# ─── In-process cache ────────────────────────────────────────────────────────

_rows: list[dict] = []
_by_ons: dict[str, list[dict]] = {}   # ONS GSS code → sorted rows
_by_name: dict[str, list[dict]] = {}  # normalised LPA name → sorted rows
_ready = threading.Event()            # set once loading finishes (success or fail)

GOV_UK_PAGE = (
    "https://www.gov.uk/government/publications/"
    "planning-inspectorate-appeals-database"
)

# ─── Change-type → PINS Development Type filter ──────────────────────────────
# PINS categorises at a higher level than our change types — there are no
# "loft" or "HMO" keywords in the data fields. We map each change type to the
# closest PINS Development Type substring.
# dev: substring to match in development_type (case-insensitive), None = all
# reason_kw: keyword in appeal_reason (case-insensitive), None = no filter

_CT_FILTER: dict[str, dict] = {
    "any":                       {"dev": None,            "reason_kw": None},
    # All householder-scale works sit in "Householder developments"
    "rear_extension":            {"dev": "householder",   "reason_kw": None},
    "side_extension":            {"dev": "householder",   "reason_kw": None},
    "loft":                      {"dev": "householder",   "reason_kw": None},
    "outbuilding":               {"dev": "householder",   "reason_kw": None},
    "basement":                  {"dev": "householder",   "reason_kw": None},
    "porch":                     {"dev": "householder",   "reason_kw": None},
    "parking":                   {"dev": "householder",   "reason_kw": None},
    "householder":               {"dev": "householder",   "reason_kw": None},
    "cladding":                  {"dev": "householder",   "reason_kw": None},
    # Change of use covers HMO and commercial→resi
    "hmo":                       {"dev": "change of use", "reason_kw": None},
    "commercial_to_residential": {"dev": "change of use", "reason_kw": None},
    # Dwellings for new build
    "new_build":                 {"dev": "dwelling",      "reason_kw": None},
    # Prior approval has its own appeal reason
    "prior_approval":            {"dev": None,            "reason_kw": "prior approval"},
    # Lawful development has its own appeal reason
    "lawful_development":        {"dev": None,            "reason_kw": "certificate of lawful"},
    # Others — no dev_type filter, return all recent
    "demolition":                {"dev": None,            "reason_kw": None},
    "retrospective":             {"dev": None,            "reason_kw": None},
    "solar":                     {"dev": "householder",   "reason_kw": None},
}


def _normalise_name(name: str) -> str:
    """Strip noise words for fuzzy LPA name matching."""
    noise = {"council", "borough", "district", "city", "county", "of", "the",
             "and", "london", "metropolitan", "royal", "unitary"}
    tokens = re.sub(r"[,.'()]", " ", (name or "").lower()).split()
    return " ".join(t for t in tokens if t not in noise).strip()


# ─── File parsing ─────────────────────────────────────────────────────────────


def _parse_excel(raw: bytes, is_older: bool) -> list[dict]:
    wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    ws = wb.active
    it = ws.iter_rows(values_only=True)
    header = [str(h or "").strip() for h in next(it)]
    col = {h: i for i, h in enumerate(header)}

    def get(row, name):
        i = col.get(name)
        return row[i] if i is not None and i < len(row) else None

    ref_col = "Older Casework Data Ref No" if is_older else "Case Number"
    addr_col = "Site Address Postcode" if is_older else "Site Address"

    out = []
    for row in it:
        decision = str(get(row, "Decision") or "").strip()
        if not decision or decision == "None":
            continue
        dd = get(row, "Decision Date")
        date_str = dd.strftime("%Y-%m-%d") if hasattr(dd, "strftime") else str(dd or "")[:10]
        ref = str(get(row, ref_col) or "")
        # Only Horizon-sourced cases have valid ACP case IDs (2M-3.4M range).
        # Back Office (6M+) and RoW-Mod (31M+) cases are not accessible via ACP ViewCase.
        # Older cases use a hyphen-format ref (e.g. "2608-00002") incompatible with ACP.
        data_source = str(get(row, "Data Source") or "") if not is_older else ""
        case_url = (
            f"https://acp.planninginspectorate.gov.uk/ViewCase.aspx?caseid={ref}"
            if ref and not is_older and data_source == "Horizon" else ""
        )
        out.append({
            "reference":       ref,
            "casework_type":   str(get(row, "Type of Casework") or ""),
            "lpa_name":        str(get(row, "LPA Name") or ""),
            "ons_code":        str(get(row, "ONS LPA Code") or ""),
            "decision_date":   date_str,
            "decision":        decision,
            "procedure":       str(get(row, "Procedure") or ""),
            "development_type":str(get(row, "Development Type") or ""),
            "appeal_reason":   str(get(row, "Appeal Type Reason") or ""),
            "type_detail":     str(get(row, "Type - Detail") or ""),
            "site_address":    str(get(row, addr_col) or ""),
            "case_url":        case_url,
        })
    return out


# ─── Background load ──────────────────────────────────────────────────────────


def _load() -> None:
    global _rows, _by_ons, _by_name
    try:
        log.info("PINS: fetching download URLs from GOV.UK")
        page = _req.get(GOV_UK_PAGE, timeout=20)
        urls = list(dict.fromkeys(
            re.findall(r"https://assets\.publishing\.service\.gov\.uk[^\s\"']+\.xlsx", page.text)
        ))
        if len(urls) < 2:
            log.warning(f"PINS: expected 2 xlsx links, found {len(urls)} — aborting")
            return

        recent_url, older_url = urls[0], urls[1]

        log.info("PINS: downloading recent file")
        recent_raw = _req.get(recent_url, timeout=90).content
        log.info(f"PINS: parsing recent ({len(recent_raw)//1024//1024} MB)")
        recent = _parse_excel(recent_raw, is_older=False)
        log.info(f"PINS: recent → {len(recent):,} rows")

        log.info("PINS: downloading older file")
        older_raw = _req.get(older_url, timeout=90).content
        log.info(f"PINS: parsing older ({len(older_raw)//1024//1024} MB)")
        older = _parse_excel(older_raw, is_older=True)
        log.info(f"PINS: older → {len(older):,} rows")

        combined = recent + older

        by_ons: dict[str, list[dict]] = {}
        by_name: dict[str, list[dict]] = {}
        for r in combined:
            ons = r["ons_code"]
            if ons:
                by_ons.setdefault(ons, []).append(r)
            nm = _normalise_name(r["lpa_name"])
            if nm:
                by_name.setdefault(nm, []).append(r)

        # Sort each bucket newest-first
        for lst in by_ons.values():
            lst.sort(key=lambda r: r["decision_date"], reverse=True)
        for lst in by_name.values():
            lst.sort(key=lambda r: r["decision_date"], reverse=True)

        _rows = combined
        _by_ons = by_ons
        _by_name = by_name
        log.info(f"PINS: loaded {len(combined):,} rows across {len(by_ons)} councils")

    except Exception:
        log.exception("PINS loader failed — appeals data unavailable")
    finally:
        _ready.set()


def start_background_load() -> None:
    """Call once at app startup — returns immediately, loads in background."""
    t = threading.Thread(target=_load, daemon=True, name="pins-loader")
    t.start()


# ─── Public search API ────────────────────────────────────────────────────────


def is_ready() -> bool:
    return _ready.is_set()


def search(
    ons_code: str | None,
    admin_district: str | None,
    change_type: str = "any",
    sample: int = 50,
) -> tuple[list[dict], dict, bool]:
    """
    Returns (sample_rows, stats, loading).
    sample_rows: up to `sample` most-recent matching rows (for display).
    stats: { total, allowed, dismissed, other, year_range }
    loading: True if data not yet available.
    """
    if not _ready.is_set():
        return [], {}, True

    # Resolve LPA bucket
    rows: list[dict] = []
    if ons_code and ons_code in _by_ons:
        rows = _by_ons[ons_code]
    elif admin_district:
        norm = _normalise_name(admin_district)
        if norm in _by_name:
            rows = _by_name[norm]
        else:
            for key, val in _by_name.items():
                if norm and (norm in key or key in norm):
                    rows = val
                    break

    if not rows:
        return [], {}, False

    # Apply change-type filter
    f = _CT_FILTER.get(change_type, _CT_FILTER["any"])
    dev_filter = f["dev"]
    reason_kw = f["reason_kw"]

    def matches(r: dict) -> bool:
        if dev_filter and dev_filter not in r["development_type"].lower():
            return False
        if reason_kw and reason_kw not in r["appeal_reason"].lower():
            return False
        return True

    filtered = [r for r in rows if matches(r)]  # already newest-first

    # Aggregate stats across ALL matching rows (not just the sample)
    allowed   = sum(1 for r in filtered if r["decision"] == "Allowed")
    dismissed = sum(1 for r in filtered if r["decision"] == "Dismissed")
    dates     = [r["decision_date"][:4] for r in filtered if r["decision_date"]]
    stats = {
        "total":      len(filtered),
        "allowed":    allowed,
        "dismissed":  dismissed,
        "other":      len(filtered) - allowed - dismissed,
        "year_from":  min(dates) if dates else "",
        "year_to":    max(dates) if dates else "",
        "allow_pct":  round(allowed / len(filtered) * 100) if filtered else 0,
    }

    return filtered[:sample], stats, False
