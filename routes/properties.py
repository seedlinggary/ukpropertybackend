from math import cos, radians
from flask import Blueprint, jsonify, request
from sqlalchemy import or_
from database import SessionLocal
from models.property_listing import PropertyListing

properties_bp = Blueprint("properties", __name__)


def _keyword_filter(q, text: str):
    """
    For each whitespace-separated word, require that at least one of
    city / address / description / property_type contains it (case-insensitive).
    All words must match — so "london flat terrace" finds listings where
    'london' is in any column AND 'flat' is in any column AND 'terrace' is in any column.
    """
    for word in text.split():
        pattern = f"%{word}%"
        q = q.filter(or_(
            PropertyListing.city.ilike(pattern),
            PropertyListing.address.ilike(pattern),
            PropertyListing.description.ilike(pattern),
            PropertyListing.property_type.ilike(pattern),
        ))
    return q


@properties_bp.route("/properties", methods=["GET"])
def list_properties():
    page  = max(1, request.args.get("page",  1,    type=int))
    limit = min(100, max(1, request.args.get("limit", 20, type=int)))
    offset = (page - 1) * limit

    session = SessionLocal()
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
            q = q.filter(PropertyListing.source == source)

        city = request.args.get("city", "").strip()
        if city:
            q = q.filter(PropertyListing.city.ilike(f"%{city}%"))

        property_type = request.args.get("property_type", "").strip()
        if property_type:
            q = q.filter(PropertyListing.property_type.ilike(f"%{property_type}%"))

        # keyword: multi-word search across city, address, description, property_type
        keyword = request.args.get("keyword", "").strip()
        if keyword:
            q = _keyword_filter(q, keyword)

        # search (from SearchBar): also multi-column now
        search = request.args.get("search", "").strip()
        if search:
            q = _keyword_filter(q, search)

        # Bounding-box proximity filter (postcode search)
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

        total = q.count()
        rows  = (
            q.order_by(PropertyListing.created_at.desc())
             .offset(offset)
             .limit(limit)
             .all()
        )

        return jsonify({
            "properties": [r.to_dict() for r in rows],
            "total":    total,
            "page":     page,
            "limit":    limit,
            "has_more": offset + len(rows) < total,
        })
    finally:
        session.close()


@properties_bp.route("/properties/<property_id>", methods=["GET"])
def get_property(property_id: str):
    session = SessionLocal()
    try:
        prop = session.query(PropertyListing).filter_by(id=property_id).first()
        if not prop:
            return jsonify({"error": "Not found"}), 404
        return jsonify(prop.to_dict())
    finally:
        session.close()
