"""
Dismissed properties: users can hide a For You card so it never resurfaces.

GET  /dismissed-properties/ids          — lightweight list of dismissed IDs (for client sync)
POST /dismissed-properties/<property_id> — toggle: dismiss if not dismissed, restore if already dismissed
"""
import uuid

from flask import Blueprint, jsonify, g
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from database import SessionLocal
from middleware.auth import require_auth
from models.dismissed import UserDismissedProperty

dismissed_bp = Blueprint("dismissed", __name__)


def _session():
    s = SessionLocal()
    s.execute(text("SET statement_timeout = '5s'"))
    return s


@dismissed_bp.route("/dismissed-properties/ids", methods=["GET"])
@require_auth()
def get_dismissed_ids():
    user_id = str(g.current_user.id)
    session = _session()
    try:
        rows = (
            session.query(UserDismissedProperty.property_id)
            .filter_by(user_id=user_id)
            .all()
        )
        return jsonify({"ids": [str(r.property_id) for r in rows]})
    finally:
        session.close()


@dismissed_bp.route("/dismissed-properties/<property_id>", methods=["POST"])
@require_auth()
def toggle_dismissed(property_id: str):
    user_id = str(g.current_user.id)
    try:
        pid = uuid.UUID(property_id)
    except ValueError:
        return jsonify({"error": "Invalid property_id"}), 400

    session = _session()
    try:
        existing = (
            session.query(UserDismissedProperty)
            .filter_by(user_id=user_id, property_id=pid)
            .first()
        )
        if existing:
            session.delete(existing)
            session.commit()
            return jsonify({"dismissed": False})
        row = UserDismissedProperty(user_id=user_id, property_id=pid)
        session.add(row)
        session.commit()
        return jsonify({"dismissed": True})
    except IntegrityError:
        session.rollback()
        return jsonify({"dismissed": True})
    finally:
        session.close()
