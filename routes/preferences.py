"""
User property preferences + "For You" matching + named profiles.

GET  /preferences/me              — load active profile's prefs + profile list
PUT  /preferences/me              — save prefs to the active profile
POST /preferences/profiles        — create a new named profile (copy_from optional)
DELETE /preferences/profiles/<name> — delete a named profile
PUT  /preferences/profiles/active — switch which profile is active
GET  /for-you                     — run live matching, returns scored + filtered results
"""
import copy
import logging
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request, g
from sqlalchemy import text

from database import SessionLocal
from middleware.auth import require_auth
from models.preferences import UserPropertyPrefs, DEFAULT_PREFS, _deep_merge
from models.property_listing import PropertyListing
from services.matching import run_matching

preferences_bp = Blueprint("preferences", __name__)
logger = logging.getLogger(__name__)

_FOR_YOU_LOOKBACK_DAYS = 90
_FOR_YOU_PROP_LIMIT    = 2000   # properties scanned per request
_FOR_YOU_PAGE_SIZE     = 30     # results returned per page

# Internal meta keys stored in prefs_json (prefixed _ to avoid collision)
_PROFILES_KEY = "_profiles"  # dict: profile_name → prefs dict
_ACTIVE_KEY   = "_active"    # str: active profile name ("default" or named)


def _session():
    s = SessionLocal()
    s.execute(text("SET statement_timeout = '5s'"))
    return s


# ── Profile helpers ───────────────────────────────────────────────────────────

def _get_profile_names(prefs_json: dict) -> list[str]:
    return ["default"] + list((prefs_json.get(_PROFILES_KEY) or {}).keys())


def _get_active_profile(prefs_json: dict) -> str:
    name = prefs_json.get(_ACTIVE_KEY, "default")
    # If active profile was deleted, fall back to default
    if name != "default" and name not in (prefs_json.get(_PROFILES_KEY) or {}):
        return "default"
    return name


def _get_profile_prefs(prefs_json: dict, name: str) -> dict:
    if name == "default":
        return {k: v for k, v in prefs_json.items() if not k.startswith("_")}
    profiles = prefs_json.get(_PROFILES_KEY) or {}
    return copy.deepcopy(profiles.get(name) or {})


def _set_profile_prefs(prefs_json: dict, name: str, new_prefs: dict) -> None:
    clean = {k: v for k, v in new_prefs.items() if not k.startswith("_")}
    if name == "default":
        for k, v in clean.items():
            prefs_json[k] = v
    else:
        if _PROFILES_KEY not in prefs_json:
            prefs_json[_PROFILES_KEY] = {}
        prefs_json[_PROFILES_KEY][name] = clean


def _effective_prefs(prefs_json: dict) -> dict:
    """Return active profile's prefs merged over DEFAULT_PREFS."""
    active = _get_active_profile(prefs_json)
    raw = _get_profile_prefs(prefs_json, active)
    merged = copy.deepcopy(DEFAULT_PREFS)
    _deep_merge(merged, raw)
    return merged


# ── Endpoints ─────────────────────────────────────────────────────────────────

@preferences_bp.route("/preferences/me", methods=["GET"])
@require_auth()
def get_preferences():
    user_id = str(g.current_user.id)
    session = _session()
    try:
        row = session.query(UserPropertyPrefs).filter_by(user_id=user_id).first()
        if not row:
            return jsonify({
                "prefs": DEFAULT_PREFS,
                "configured": False,
                "profiles": ["default"],
                "active_profile": "default",
            })
        prefs_json = row.prefs_json or {}
        return jsonify({
            "prefs": _effective_prefs(prefs_json),
            "configured": True,
            "profiles": _get_profile_names(prefs_json),
            "active_profile": _get_active_profile(prefs_json),
        })
    finally:
        session.close()


@preferences_bp.route("/preferences/me", methods=["PUT"])
@require_auth()
def save_preferences():
    user_id = str(g.current_user.id)
    body = request.get_json(silent=True) or {}
    new_prefs = body.get("prefs") or {}

    session = _session()
    try:
        row = session.query(UserPropertyPrefs).filter_by(user_id=user_id).first()
        if row:
            prefs_json = copy.deepcopy(row.prefs_json or {})
        else:
            prefs_json = {}

        active = _get_active_profile(prefs_json)

        # Build merged prefs for the active profile
        existing = _get_profile_prefs(prefs_json, active)
        base = copy.deepcopy(DEFAULT_PREFS)
        _deep_merge(base, existing)
        _deep_merge(base, new_prefs)

        # Save back to the active profile slot
        _set_profile_prefs(prefs_json, active, base)

        if row:
            row.prefs_json = prefs_json
            row.updated_at = datetime.utcnow()
        else:
            row = UserPropertyPrefs(user_id=user_id, prefs_json=prefs_json)
            session.add(row)

        session.commit()
        return jsonify({
            "prefs": _effective_prefs(prefs_json),
            "configured": True,
            "profiles": _get_profile_names(prefs_json),
            "active_profile": active,
        })
    finally:
        session.close()


@preferences_bp.route("/preferences/profiles", methods=["POST"])
@require_auth()
def create_profile():
    user_id = str(g.current_user.id)
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    copy_from = (body.get("copy_from") or "default").strip()

    if not name or name == "default":
        return jsonify({"error": "Profile name must be non-empty and not 'default'"}), 400

    session = _session()
    try:
        row = session.query(UserPropertyPrefs).filter_by(user_id=user_id).first()
        if not row:
            prefs_json: dict = {}
            row = UserPropertyPrefs(user_id=user_id, prefs_json=prefs_json)
            session.add(row)
        else:
            prefs_json = copy.deepcopy(row.prefs_json or {})

        if name in (prefs_json.get(_PROFILES_KEY) or {}):
            return jsonify({"error": f"Profile '{name}' already exists"}), 409

        # Copy from source profile
        source_prefs = _get_profile_prefs(prefs_json, copy_from)
        _set_profile_prefs(prefs_json, name, source_prefs)

        # Switch to new profile
        prefs_json[_ACTIVE_KEY] = name

        row.prefs_json = prefs_json
        row.updated_at = datetime.utcnow()
        session.commit()

        return jsonify({
            "profiles": _get_profile_names(prefs_json),
            "active_profile": name,
        })
    finally:
        session.close()


@preferences_bp.route("/preferences/profiles/<name>", methods=["DELETE"])
@require_auth()
def delete_profile(name: str):
    if name == "default":
        return jsonify({"error": "Cannot delete the default profile"}), 400

    user_id = str(g.current_user.id)
    session = _session()
    try:
        row = session.query(UserPropertyPrefs).filter_by(user_id=user_id).first()
        if not row:
            return jsonify({"error": "No preferences found"}), 404

        prefs_json = copy.deepcopy(row.prefs_json or {})
        profiles = prefs_json.get(_PROFILES_KEY) or {}

        if name not in profiles:
            return jsonify({"error": f"Profile '{name}' not found"}), 404

        del profiles[name]
        prefs_json[_PROFILES_KEY] = profiles

        # If active profile was deleted, fall back to default
        if prefs_json.get(_ACTIVE_KEY) == name:
            prefs_json[_ACTIVE_KEY] = "default"

        row.prefs_json = prefs_json
        row.updated_at = datetime.utcnow()
        session.commit()

        return jsonify({
            "profiles": _get_profile_names(prefs_json),
            "active_profile": _get_active_profile(prefs_json),
        })
    finally:
        session.close()


@preferences_bp.route("/preferences/profiles/active", methods=["PUT"])
@require_auth()
def switch_active_profile():
    user_id = str(g.current_user.id)
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "default").strip()

    session = _session()
    try:
        row = session.query(UserPropertyPrefs).filter_by(user_id=user_id).first()
        if not row:
            return jsonify({"error": "No preferences found"}), 404

        prefs_json = copy.deepcopy(row.prefs_json or {})

        if name != "default" and name not in (prefs_json.get(_PROFILES_KEY) or {}):
            return jsonify({"error": f"Profile '{name}' not found"}), 404

        prefs_json[_ACTIVE_KEY] = name
        row.prefs_json = prefs_json
        row.updated_at = datetime.utcnow()
        session.commit()

        return jsonify({
            "prefs": _effective_prefs(prefs_json),
            "configured": True,
            "profiles": _get_profile_names(prefs_json),
            "active_profile": name,
        })
    finally:
        session.close()


@preferences_bp.route("/for-you", methods=["GET"])
@require_auth()
def for_you():
    """
    Live matching: fetches recent properties, scores against active profile's prefs,
    filters out dismissed properties, returns sorted results.
    """
    user_id = str(g.current_user.id)

    session = _session()
    try:
        pref_row = session.query(UserPropertyPrefs).filter_by(user_id=user_id).first()
        if not pref_row:
            return jsonify({"matches": [], "configured": False, "message": "Set your preferences first."})

        prefs = _effective_prefs(pref_row.prefs_json or {})

        # Determine lookback window
        max_days = prefs.get("max_days_old") or _FOR_YOU_LOOKBACK_DAYS
        cutoff = datetime.utcnow() - timedelta(days=int(max_days))

        q = session.query(PropertyListing).filter(PropertyListing.created_at >= cutoff)

        # Apply hard pre-filters for "required" dimensions only (keeps Nano-safe)
        importance = prefs.get("importance") or {}

        max_price = prefs.get("max_price")
        if max_price and importance.get("price") == "required":
            q = q.filter(PropertyListing.price <= max_price)

        min_beds = prefs.get("min_bedrooms")
        if min_beds and importance.get("bedrooms") == "required":
            q = q.filter(PropertyListing.bedrooms >= min_beds)

        article4_pref = prefs.get("article4", "any")
        if article4_pref != "any" and importance.get("article4") == "required":
            q = q.filter(PropertyListing.article4 == (article4_pref == "yes"))

        props = q.order_by(PropertyListing.created_at.desc()).limit(_FOR_YOU_PROP_LIMIT).all()
        prop_dicts = [p.to_dict() for p in props]

        # Load dismissed IDs and filter them out
        try:
            from models.dismissed import UserDismissedProperty
            dismissed_rows = (
                session.query(UserDismissedProperty.property_id)
                .filter_by(user_id=user_id)
                .all()
            )
            dismissed_set = {str(r.property_id) for r in dismissed_rows}
            if dismissed_set:
                prop_dicts = [p for p in prop_dicts if p.get("id") not in dismissed_set]
        except Exception:
            logger.debug("Could not load dismissed properties", exc_info=True)

        min_score = float(request.args.get("min_score", 0))
        sort      = request.args.get("sort", "score")   # "score" | "newest"
        offset    = max(0, int(request.args.get("offset", 0)))

        all_matches = run_matching(prefs, prop_dicts, min_score=min_score)

        if sort == "newest":
            all_matches.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        # run_matching already sorts by score descending (default)

        total_matches = len(all_matches)
        page = all_matches[offset: offset + _FOR_YOU_PAGE_SIZE]
        has_more = offset + _FOR_YOU_PAGE_SIZE < total_matches

        active_profile = _get_active_profile(pref_row.prefs_json or {})
        return jsonify({
            "matches": page,
            "configured": True,
            "total": total_matches,
            "has_more": has_more,
            "offset": offset,
            "properties_scanned": len(prop_dicts),
            "active_profile": active_profile,
        })
    finally:
        session.close()
