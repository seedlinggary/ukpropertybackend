from flask import Blueprint, jsonify, request, g
from sqlalchemy import text

from database import SessionLocal
from middleware.auth import require_auth
from models.saved_search import UserSavedSearch

saved_searches_bp = Blueprint("saved_searches", __name__)


def _session():
    s = SessionLocal()
    s.execute(text("SET statement_timeout = '5s'"))
    return s


@saved_searches_bp.route("/saved-searches", methods=["GET"])
@require_auth()
def list_saved_searches():
    user_id = str(g.current_user.id)
    session = _session()
    try:
        rows = (session.query(UserSavedSearch)
                .filter_by(user_id=user_id)
                .order_by(UserSavedSearch.created_at.desc())
                .limit(50)
                .all())
        return jsonify({"searches": [r.to_dict() for r in rows]})
    finally:
        session.close()


@saved_searches_bp.route("/saved-searches", methods=["POST"])
@require_auth()
def create_saved_search():
    user_id = str(g.current_user.id)
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    filters = data.get("filters") or {}
    if not name:
        return jsonify({"error": "name required"}), 400

    session = _session()
    try:
        row = UserSavedSearch(user_id=user_id, name=name, filters_json=filters)
        session.add(row)
        session.commit()
        return jsonify({"search": row.to_dict()}), 201
    finally:
        session.close()


@saved_searches_bp.route("/saved-searches/<search_id>", methods=["DELETE"])
@require_auth()
def delete_saved_search(search_id: str):
    user_id = str(g.current_user.id)
    session = _session()
    try:
        row = session.query(UserSavedSearch).filter_by(id=search_id, user_id=user_id).first()
        if not row:
            return jsonify({"error": "Not found"}), 404
        session.delete(row)
        session.commit()
        return jsonify({"message": "Deleted"})
    finally:
        session.close()
