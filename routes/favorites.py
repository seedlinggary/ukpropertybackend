from flask import Blueprint, jsonify, request, g
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from database import SessionLocal
from middleware.auth import require_auth
from models.notes import UserFavorite

favorites_bp = Blueprint("favorites", __name__)


def _new_session():
    session = SessionLocal()
    session.execute(text("SET statement_timeout = '5s'"))
    return session


@favorites_bp.route("/favorites", methods=["GET"])
@require_auth()
def list_favorites():
    user_id = str(g.current_user.id)
    session = _new_session()
    try:
        favs = (
            session.query(UserFavorite)
            .filter(UserFavorite.user_id == user_id)
            .order_by(UserFavorite.created_at.desc())
            .limit(500)
            .all()
        )
        return jsonify({"favorites": [f.to_dict() for f in favs]})
    finally:
        session.close()


@favorites_bp.route("/favorites/ids", methods=["GET"])
@require_auth()
def list_favorite_ids():
    user_id = str(g.current_user.id)
    session = _new_session()
    try:
        rows = (
            session.query(UserFavorite.property_id)
            .filter(UserFavorite.user_id == user_id)
            .limit(500)
            .all()
        )
        return jsonify({"ids": [r.property_id for r in rows]})
    finally:
        session.close()


@favorites_bp.route("/favorites/<property_id>", methods=["POST"])
@require_auth()
def toggle_favorite(property_id: str):
    user_id = str(g.current_user.id)
    data = request.get_json(silent=True) or {}
    address = data.get("address") or data.get("property_address")
    snapshot = data.get("snapshot") or data.get("property_snapshot")

    session = _new_session()
    try:
        existing = (
            session.query(UserFavorite)
            .filter(
                UserFavorite.user_id == user_id,
                UserFavorite.property_id == property_id,
            )
            .first()
        )
        if existing:
            # Refresh snapshot whenever one is provided, then toggle
            if snapshot is not None:
                existing.property_snapshot = snapshot
            if address:
                existing.property_address = address
            session.delete(existing)
            session.commit()
            return jsonify({"favorited": False, "property_id": property_id})

        fav = UserFavorite(
            user_id=user_id,
            property_id=property_id,
            property_address=address,
            property_snapshot=snapshot,
        )
        session.add(fav)
        session.commit()
        return jsonify({"favorited": True, "property_id": property_id, "favorite": fav.to_dict()})
    except IntegrityError:
        session.rollback()
        return jsonify({"favorited": True, "property_id": property_id})
    finally:
        session.close()
