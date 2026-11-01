from flask import Blueprint, jsonify, request, g
from sqlalchemy import text

from database import SessionLocal
from middleware.auth import require_auth
from models.site_settings import SiteSetting

site_settings_bp = Blueprint("site_settings", __name__)


def _session():
    s = SessionLocal()
    s.execute(text("SET statement_timeout = '5s'"))
    return s


@site_settings_bp.route("/site-settings/announcement", methods=["GET"])
def get_announcement():
    session = _session()
    try:
        row = session.query(SiteSetting).filter_by(key="announcement").first()
        if not row or not row.value_text:
            return jsonify({"announcement": None})
        return jsonify({"announcement": row.value_text, "updated_at": row.updated_at.isoformat()})
    finally:
        session.close()


@site_settings_bp.route("/admin/announcement", methods=["PUT"])
@require_auth(roles=["super_admin"])
def set_announcement():
    data = request.get_json(silent=True) or {}
    text_val = (data.get("text") or "").strip() or None

    session = _session()
    try:
        row = session.query(SiteSetting).filter_by(key="announcement").first()
        if row:
            row.value_text = text_val
            row.updated_by_id = g.current_user.id
        else:
            row = SiteSetting(key="announcement", value_text=text_val, updated_by_id=g.current_user.id)
            session.add(row)
        session.commit()
        return jsonify({"announcement": text_val})
    finally:
        session.close()
