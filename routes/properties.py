from math import cos, radians
import time
from flask import Blueprint, jsonify, request
from sqlalchemy import or_, text
from sqlalchemy.exc import OperationalError
from database import SessionLocal
from models.property_listing import PropertyListing

properties_bp = Blueprint("properties", __name__)

# Cache the pg_class row-count estimate so we don't hit it every request.
# This is an O(1) metadata read vs a full table scan for COUNT(*).
_total_cache: dict = {}
_TOTAL_TTL = 180  # seconds


def _new_session():
    """Create a fresh session and immediately set unlimited statement timeout."""
    session = SessionLocal()
    session.execute(text("SET statement_timeout = 0"))
    return session


def _pg_estimate(session) -> int | None:
    """Read the planner's row-count estimate from pg_class — no table scan, O(1)."""
    now = time.time()
    cached = _total_cache.get("all")
    if cached and now - cached[1] < _TOTAL_TTL:
        return cached[0]
    try:
        n = session.execute(text(
            "SELECT reltuples::bigint FROM pg_class WHERE relname = 'property_listings'"
        )).scalar()
        result = int(n or 0)
        _total_cache["all"] = (result, now)
        return result
    except Exception:
        return None


def _keyword_filter(q, text: str):
    """
    For each whitespace-separated word, require that at least one of
    city / address / description / property_type / source / agent_name contains it.
    All words must match — so "london flat allsop" finds listings where
    'london' is in any column AND 'flat' is in any column AND 'allsop' is in any column.
    """
    for word in text.split():
        pattern = f"%{word}%"
        q = q.filter(or_(
            PropertyListing.city.ilike(pattern),
            PropertyListing.address.ilike(pattern),
            PropertyListing.description.ilike(pattern),
            PropertyListing.property_type.ilike(pattern),
            PropertyListing.source.ilike(pattern),
            PropertyListing.agent_name.ilike(pattern),
        ))
    return q


@properties_bp.route("/properties", methods=["GET"])
def list_properties():
    page  = max(1, request.args.get("page",  1,    type=int))
    limit = min(100, max(1, request.args.get("limit", 20, type=int)))
    offset = (page - 1) * limit

    for attempt in range(3):
        if attempt > 0:
            time.sleep(0.75 * attempt)
        session = _new_session()
        try:
            q = session.query(PropertyListing)

            article4 = request.args.get("article4")
            if article4 == "true":
                q = q.filter(PropertyListing.article4 == True)
            elif article4 == "false":
                q = q.filter(PropertyListing.article4 == False)

            min_price = request.args.get("min_price", type=int)
            max_price = request.args.get("max_price", type=int)
            if min_price is not None:
                q = q.filter(PropertyListing.price >= min_price)
            if max_price is not None:
                q = q.filter(PropertyListing.price <= max_price)

            min_beds = request.args.get("min_bedrooms", type=int)
            if min_beds is not None:
                q = q.filter(PropertyListing.bedrooms >= min_beds)

            min_size = request.args.get("min_size_m2", type=float)
            max_size = request.args.get("max_size_m2", type=float)
            if min_size is not None:
                q = q.filter(PropertyListing.size_m2 >= min_size)
            if max_size is not None:
                q = q.filter(PropertyListing.size_m2 <= max_size)

            source = request.args.get("source")
            if source:
                if source == "auction_houses":
                    q = q.filter(PropertyListing.source != "zoopla")
                else:
                    q = q.filter(PropertyListing.source == source)

            city = request.args.get("city", "").strip()
            if city:
                q = q.filter(PropertyListing.city.ilike(f"%{city}%"))

            property_type = request.args.get("property_type", "").strip()
            if property_type:
                q = q.filter(PropertyListing.property_type.ilike(f"%{property_type}%"))

            keyword = request.args.get("keyword", "").strip()
            if keyword:
                q = _keyword_filter(q, keyword)

            search = request.args.get("search", "").strip()
            if search:
                q = _keyword_filter(q, search)

            lat = request.args.get("lat", type=float)
            lng = request.args.get("lng", type=float)
            radius_km = request.args.get("radius_km", 5.0, type=float)
            if lat is not None and lng is not None:
                lat_d = radius_km / 111.0
                lng_d = radius_km / (111.0 * max(0.01, abs(cos(radians(lat)))))
                q = q.filter(
                    PropertyListing.lat.between(lat - lat_d, lat + lat_d),
                    PropertyListing.lng.between(lng - lng_d, lng + lng_d),
                )

            sort_by = request.args.get("sort_by", "newest")
            if sort_by == "auction_date_asc":
                order_clause = PropertyListing.auction_date.asc().nullslast()
            elif sort_by == "auction_date_desc":
                order_clause = PropertyListing.auction_date.desc().nullsfirst()
            elif sort_by == "price_asc":
                order_clause = PropertyListing.price.asc().nullslast()
            elif sort_by == "price_desc":
                order_clause = PropertyListing.price.desc().nullsfirst()
            else:
                order_clause = PropertyListing.created_at.desc()

            is_filtered = any([
                article4, min_price, max_price, min_beds,
                min_size, max_size, source, city,
                property_type, keyword, search, lat is not None,
            ])

            if not is_filtered:
                # Use pg_class planner estimate — no table scan, survives I/O pressure.
                # Fetch exact limit; has_more derived from estimate.
                total = _pg_estimate(session) or 0
                rows = (
                    q.order_by(order_clause)
                     .offset(offset)
                     .limit(limit)
                     .all()
                )
                has_more = offset + len(rows) < total
            else:
                # Filtered: fetch one extra row to detect has_more without COUNT(*).
                rows = (
                    q.order_by(order_clause)
                     .offset(offset)
                     .limit(limit + 1)
                     .all()
                )
                has_more = len(rows) > limit
                rows = rows[:limit]
                total = offset + len(rows) + (1 if has_more else 0)

            return jsonify({
                "properties": [r.to_dict() for r in rows],
                "total":    total,
                "page":     page,
                "limit":    limit,
                "has_more": has_more,
            })
        except OperationalError:
            if attempt == 2:
                return jsonify({
                    "error": "DB connection failed after retries",
                    "properties": [],
                    "total": 0,
                    "page": page,
                    "limit": limit,
                    "has_more": False,
                }), 503
        finally:
            session.close()


@properties_bp.route("/properties/<property_id>", methods=["GET"])
def get_property(property_id: str):
    for attempt in range(3):
        if attempt > 0:
            time.sleep(0.75 * attempt)
        session = _new_session()
        try:
            prop = session.query(PropertyListing).filter_by(id=property_id).first()
            if not prop:
                return jsonify({"error": "Not found"}), 404
            return jsonify(prop.to_dict())
        except OperationalError:
            if attempt == 2:
                return jsonify({"error": "DB connection failed after retries"}), 503
        finally:
            session.close()
