"""
Property matching service.

Given a user's preferences JSON, scores each PropertyListing 0-100.
Properties that fail any "required" dimension score -1 (excluded).
Returns (score, reasons, breakdown) — breakdown is structured per-dimension data for the UI.
"""
from __future__ import annotations

import logging
from math import radians, cos, sin, asin, sqrt
from typing import Any

logger = logging.getLogger(__name__)

_IMPORTANCE_WEIGHT = {
    "required": None,   # special — excludes on miss
    "high":     1.0,
    "medium":   0.6,
    "low":      0.3,
    "ignore":   0.0,
}

AUCTION_SOURCES = {"cliveemson", "auctionhouse", "allsop", "savills_auctions", "strettons"}

_DIMENSION_LABELS = {
    "price":         "Price",
    "bedrooms":      "Bedrooms",
    "location":      "Location",
    "article4":      "Article 4",
    "property_type": "Property Type",
    "tenure":        "Tenure",
    "price_per_m2":  "Price per m²",
    "size":          "Size",
    "source":        "Source",
}


def _imp(prefs: dict, key: str) -> str:
    return (prefs.get("importance") or {}).get(key, "medium")


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371.0
    dlat = radians(lat2 - lat1)
    dlng = radians(lng2 - lng1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlng / 2) ** 2
    return R * 2 * asin(sqrt(a))


def score_property(prop_dict: dict, prefs: dict) -> tuple[float, list[str], list[dict]]:
    """
    Returns (score, reasons, breakdown).
    score == -1  →  property is excluded (failed a "required" dimension)
    score ∈ [0, 100]  →  how well it matches
    reasons  →  human-readable match descriptions
    breakdown  →  structured per-dimension data for UI popover
    """
    score = 0.0
    reasons: list[str] = []
    breakdown: list[dict] = []

    def _bd(dimension: str, pts_earned: float, pts_max: int, importance: str,
            matched: bool | None, na: bool = False) -> None:
        breakdown.append({
            "dimension": dimension,
            "label": _DIMENSION_LABELS.get(dimension, dimension),
            "pts_earned": round(pts_earned, 1),
            "pts_max": pts_max,
            "importance": importance,
            "matched": matched,
            "na": na,
        })

    price       = prop_dict.get("price")
    bedrooms    = prop_dict.get("bedrooms")
    city        = (prop_dict.get("city") or "").strip().lower()
    article4    = prop_dict.get("article4", False)
    prop_type   = (prop_dict.get("property_type") or "").lower()
    tenure      = (prop_dict.get("tenure") or "").lower()
    source      = (prop_dict.get("source") or "").lower()
    size_m2     = prop_dict.get("size_m2")
    lat         = prop_dict.get("lat")
    lng         = prop_dict.get("lng")

    price_per_m2 = (price / size_m2) if (price and size_m2 and size_m2 > 0) else None

    # ── Price ─────────────────────────────────────────────────────────────────
    max_price = prefs.get("max_price")
    min_price = prefs.get("min_price")
    imp_price = _imp(prefs, "price")

    if max_price:
        if price is None:
            if imp_price == "required":
                return -1, [], []
            _bd("price", 0, 25, imp_price, None, na=True)
        elif price > max_price:
            if imp_price == "required":
                return -1, [], []
            w = _IMPORTANCE_WEIGHT.get(imp_price, 0.6)
            pts = -25 * w
            score += pts
            _bd("price", pts, 25, imp_price, False)
        else:
            w = _IMPORTANCE_WEIGHT.get(imp_price, 0.6) or 1.0
            pts = 25 * w
            score += pts
            reasons.append(f"Price £{price:,} within budget £{max_price:,}")
            _bd("price", pts, 25, imp_price, True)
    else:
        _bd("price", 0, 25, imp_price, None, na=True)

    if min_price and price is not None and price < min_price:
        imp = _imp(prefs, "price")
        if imp == "required":
            return -1, [], []
        score -= 5

    # ── Bedrooms ──────────────────────────────────────────────────────────────
    min_beds = prefs.get("min_bedrooms")
    imp_beds = _imp(prefs, "bedrooms")
    if min_beds:
        if bedrooms is None or bedrooms < min_beds:
            if imp_beds == "required":
                return -1, [], []
            w = _IMPORTANCE_WEIGHT.get(imp_beds, 0.6)
            pts = -15 * w
            score += pts
            _bd("bedrooms", pts, 15, imp_beds, False)
        else:
            w = _IMPORTANCE_WEIGHT.get(imp_beds, 0.6) or 1.0
            pts = 15 * w
            score += pts
            reasons.append(f"{bedrooms} bed{'s' if bedrooms != 1 else ''} meets minimum ({min_beds}+)")
            _bd("bedrooms", pts, 15, imp_beds, True)
    else:
        _bd("bedrooms", 0, 15, imp_beds, None, na=True)

    # ── Location ──────────────────────────────────────────────────────────────
    preferred_cities = [c.lower() for c in (prefs.get("preferred_cities") or []) if c]
    center_lat = prefs.get("center_lat")
    center_lng = prefs.get("center_lng")
    radius_km  = prefs.get("radius_km") or 5.0
    imp_loc    = _imp(prefs, "location")

    location_match = False
    location_reason = ""

    if preferred_cities:
        for pc in preferred_cities:
            if pc in city or (prop_dict.get("address") or "").lower().find(pc) != -1:
                location_match = True
                location_reason = f"In preferred location ({pc.title()})"
                break

    if not location_match and center_lat and center_lng and lat and lng:
        dist = _haversine_km(center_lat, center_lng, lat, lng)
        if dist <= radius_km:
            location_match = True
            label = prefs.get("center_label") or f"{center_lat:.3f},{center_lng:.3f}"
            location_reason = f"Within {radius_km:.0f}km of {label}"

    if preferred_cities or (center_lat and center_lng):
        w = _IMPORTANCE_WEIGHT.get(imp_loc, 0.6)
        if location_match:
            pts = 20 * (w or 1.0)
            score += pts
            if location_reason:
                reasons.append(location_reason)
            _bd("location", pts, 20, imp_loc, True)
        else:
            if imp_loc == "required":
                return -1, [], []
            pts = -10 * (w or 0.6)
            score += pts
            _bd("location", pts, 20, imp_loc, False)
    else:
        _bd("location", 0, 20, imp_loc, None, na=True)

    # ── Article 4 ────────────────────────────────────────────────────────────
    pref_a4  = prefs.get("article4", "any")
    imp_a4   = _imp(prefs, "article4")

    if pref_a4 != "any" and imp_a4 != "ignore":
        want_a4 = pref_a4 == "yes"
        w = _IMPORTANCE_WEIGHT.get(imp_a4, 0.6)
        if bool(article4) == want_a4:
            pts = 15 * (w or 1.0)
            score += pts
            reasons.append(f"Article 4: {'restricted' if want_a4 else 'unrestricted'} as preferred")
            _bd("article4", pts, 15, imp_a4, True)
        else:
            if imp_a4 == "required":
                return -1, [], []
            pts = -15 * (w or 0.6)
            score += pts
            _bd("article4", pts, 15, imp_a4, False)
    else:
        _bd("article4", 0, 15, imp_a4, None, na=True)

    # ── Property type ─────────────────────────────────────────────────────────
    pref_types = [t.lower() for t in (prefs.get("property_types") or []) if t]
    imp_type   = _imp(prefs, "property_type")

    if pref_types and imp_type != "ignore":
        w = _IMPORTANCE_WEIGHT.get(imp_type, 0.6)
        type_match = any(t in prop_type for t in pref_types)
        if type_match:
            pts = 10 * (w or 1.0)
            score += pts
            reasons.append(f"Property type ({prop_type or 'unknown'}) matches preference")
            _bd("property_type", pts, 10, imp_type, True)
        else:
            if imp_type == "required":
                return -1, [], []
            pts = -5 * (w or 0.6)
            score += pts
            _bd("property_type", pts, 10, imp_type, False)
    else:
        _bd("property_type", 0, 10, imp_type, None, na=True)

    # ── Tenure ───────────────────────────────────────────────────────────────
    pref_tenure = (prefs.get("tenure") or "").lower()
    imp_tenure  = _imp(prefs, "tenure")

    if pref_tenure and pref_tenure != "any" and imp_tenure != "ignore":
        w = _IMPORTANCE_WEIGHT.get(imp_tenure, 0.3)
        if tenure == pref_tenure:
            pts = 5 * (w or 1.0)
            score += pts
            reasons.append(f"Tenure ({tenure}) matches preference")
            _bd("tenure", pts, 5, imp_tenure, True)
        else:
            if imp_tenure == "required":
                return -1, [], []
            pts = -3 * (w or 0.3)
            score += pts
            _bd("tenure", pts, 5, imp_tenure, False)
    else:
        _bd("tenure", 0, 5, imp_tenure, None, na=True)

    # ── Price per m² ─────────────────────────────────────────────────────────
    max_ppm2 = prefs.get("max_price_per_m2")
    imp_ppm2 = _imp(prefs, "price_per_m2")

    if max_ppm2 and imp_ppm2 != "ignore":
        w = _IMPORTANCE_WEIGHT.get(imp_ppm2, 0.6)
        if price_per_m2 is None:
            _bd("price_per_m2", 0, 10, imp_ppm2, None, na=True)
        elif price_per_m2 <= max_ppm2:
            pts = 10 * (w or 1.0)
            score += pts
            reasons.append(f"£{price_per_m2:,.0f}/m² under target £{max_ppm2:,}/m²")
            _bd("price_per_m2", pts, 10, imp_ppm2, True)
        else:
            if imp_ppm2 == "required":
                return -1, [], []
            pts = -5 * (w or 0.6)
            score += pts
            _bd("price_per_m2", pts, 10, imp_ppm2, False)
    else:
        _bd("price_per_m2", 0, 10, imp_ppm2, None, na=True)

    # ── Size ─────────────────────────────────────────────────────────────────
    min_sz = prefs.get("min_size_m2")
    max_sz = prefs.get("max_size_m2")
    imp_sz = _imp(prefs, "size")
    size_pts = 0.0
    size_matched: bool | None = None

    if imp_sz != "ignore":
        w = _IMPORTANCE_WEIGHT.get(imp_sz, 0.3)
        if min_sz and size_m2 is not None:
            if size_m2 < min_sz:
                if imp_sz == "required":
                    return -1, [], []
                size_pts = -5 * (w or 0.3)
                size_matched = False
            else:
                size_pts = 5 * (w or 1.0)
                size_matched = True
                reasons.append(f"{size_m2:.0f}m² meets minimum ({min_sz:.0f}m²)")
            score += size_pts
        if max_sz and size_m2 is not None and size_m2 > max_sz:
            if imp_sz == "required":
                return -1, [], []
            score -= 3 * (w or 0.3)
        _bd("size", size_pts, 5, imp_sz, size_matched if (min_sz or max_sz) else None,
            na=(not min_sz and not max_sz))
    else:
        _bd("size", 0, 5, imp_sz, None, na=True)

    # ── Source preference ────────────────────────────────────────────────────
    pref_source = (prefs.get("source") or "").lower()
    imp_src     = _imp(prefs, "source")

    if pref_source and imp_src != "ignore":
        w = _IMPORTANCE_WEIGHT.get(imp_src, 0.3)
        if pref_source == "auction_houses":
            src_match = source in AUCTION_SOURCES
        else:
            src_match = source == pref_source
        if src_match:
            pts = 5 * (w or 1.0)
            score += pts
            reasons.append("Source matches preference")
            _bd("source", pts, 5, imp_src, True)
        else:
            if imp_src == "required":
                return -1, [], []
            _bd("source", 0, 5, imp_src, False)
    else:
        _bd("source", 0, 5, imp_src, None, na=True)

    # ── Clamp to [0, 100] ────────────────────────────────────────────────────
    final = max(0.0, min(100.0, score))
    return round(final, 1), reasons, breakdown


def max_possible_score(prefs: dict) -> float:
    """
    Maximum score a perfectly-matching property could earn with these prefs.
    Returns 0 when no scoring dimensions are active (everything ignored / unset).
    """
    imp = prefs.get("importance") or {}

    def w(key: str) -> float:
        v = imp.get(key, "medium")
        if v == "ignore":
            return 0.0
        if v == "required":
            return 1.0  # "required" matches use weight 1.0 in score_property
        return _IMPORTANCE_WEIGHT.get(v, 0.6) or 0.0

    total = 0.0

    if prefs.get("max_price"):
        total += 25 * w("price")

    if prefs.get("min_bedrooms"):
        total += 15 * w("bedrooms")

    cities = [c for c in (prefs.get("preferred_cities") or []) if c]
    has_center = prefs.get("center_lat") and prefs.get("center_lng")
    if cities or has_center:
        total += 20 * w("location")

    if (prefs.get("article4") or "any") != "any":
        total += 15 * w("article4")

    types = [t for t in (prefs.get("property_types") or []) if t]
    if types:
        total += 10 * w("property_type")

    tenure = (prefs.get("tenure") or "").lower()
    if tenure and tenure != "any":
        total += 5 * w("tenure")

    if prefs.get("max_price_per_m2"):
        total += 10 * w("price_per_m2")

    if prefs.get("min_size_m2") or prefs.get("max_size_m2"):
        total += 5 * w("size")

    if (prefs.get("source") or "").lower():
        total += 5 * w("source")

    return total


def run_matching(prefs: dict, properties: list[dict], min_score: float = 0.0) -> list[dict]:
    """
    Score all properties against prefs, normalize to 0–100 as a real percentage,
    and return sorted matches at or above min_score.

    When no preferences are configured (max_possible == 0), every non-excluded
    property gets score 100 so it is always returned.
    """
    max_poss = max_possible_score(prefs)

    results = []
    for prop in properties:
        try:
            sc, reasons, breakdown = score_property(prop, prefs)
            if sc < 0:
                continue  # failed a "required" dimension

            # Normalise raw score → true percentage of what was achievable
            if max_poss > 0:
                normalized = round(min(100.0, (sc / max_poss) * 100), 1)
            else:
                # No active preferences — every property is a perfect match
                normalized = 100.0

            if normalized >= min_score:
                results.append({
                    **prop,
                    "match_score": normalized,
                    "match_reasons": reasons,
                    "match_breakdown": breakdown,
                })
        except Exception:
            logger.debug("Scoring error for property %s", prop.get("id"), exc_info=True)
    results.sort(key=lambda x: x["match_score"], reverse=True)
    return results
