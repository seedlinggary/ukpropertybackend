from datetime import datetime

from flask import Blueprint, jsonify, request, g
from sqlalchemy import text

from database import SessionLocal
from middleware.auth import require_auth
from models.notes import UserNote, UserFavorite

notes_bp = Blueprint("notes", __name__)


def _new_session():
    session = SessionLocal()
    session.execute(text("SET statement_timeout = '5s'"))
    return session


# ── Notes ─────────────────────────────────────────────────────────────────────

@notes_bp.route("/notes", methods=["GET"])
@require_auth()
def list_notes():
    user_id = str(g.current_user.id)
    note_type = request.args.get("type")          # "general" | "property"
    property_id = request.args.get("property_id") # filter by property
    search = request.args.get("q", "").strip()     # full-text search in title/content/address

    session = _new_session()
    try:
        q = session.query(UserNote).filter(UserNote.user_id == user_id)

        if note_type:
            q = q.filter(UserNote.note_type == note_type)
        if property_id:
            q = q.filter(UserNote.property_id == property_id)
        if search:
            like = f"%{search}%"
            from sqlalchemy import or_
            q = q.filter(or_(
                UserNote.title.ilike(like),
                UserNote.content.ilike(like),
                UserNote.property_address.ilike(like),
            ))

        notes = q.order_by(UserNote.updated_at.desc()).limit(200).all()
        return jsonify({"notes": [n.to_dict() for n in notes]})
    finally:
        session.close()
        if hasattr(g, "db_session") and g.db_session is not session:
            pass  # main session closed by middleware


@notes_bp.route("/notes", methods=["POST"])
@require_auth()
def create_note():
    user_id = str(g.current_user.id)
    data = request.get_json(silent=True) or {}

    note_type = data.get("note_type", "general")
    if note_type not in ("general", "property"):
        return jsonify({"error": "note_type must be 'general' or 'property'"}), 400

    content = (data.get("content") or "").strip()
    title = (data.get("title") or "").strip() or None
    property_id = data.get("property_id") or None
    property_address = (data.get("property_address") or "").strip() or None

    if note_type == "property" and not property_id:
        return jsonify({"error": "property_id required for property notes"}), 400

    session = _new_session()
    try:
        note = UserNote(
            user_id=user_id,
            note_type=note_type,
            property_id=property_id,
            property_address=property_address,
            title=title,
            content=content,
        )
        session.add(note)
        session.commit()
        return jsonify({"note": note.to_dict()}), 201
    finally:
        session.close()


@notes_bp.route("/notes/<note_id>", methods=["PUT"])
@require_auth()
def update_note(note_id: str):
    user_id = str(g.current_user.id)
    data = request.get_json(silent=True) or {}

    session = _new_session()
    try:
        note = session.query(UserNote).filter_by(id=note_id, user_id=user_id).first()
        if not note:
            return jsonify({"error": "Note not found"}), 404

        if "title" in data:
            note.title = (data["title"] or "").strip() or None
        if "content" in data:
            note.content = (data["content"] or "").strip()
        if "property_address" in data:
            note.property_address = (data["property_address"] or "").strip() or None
        note.updated_at = datetime.utcnow()

        session.commit()
        return jsonify({"note": note.to_dict()})
    finally:
        session.close()


@notes_bp.route("/notes/<note_id>", methods=["DELETE"])
@require_auth()
def delete_note(note_id: str):
    user_id = str(g.current_user.id)

    session = _new_session()
    try:
        note = session.query(UserNote).filter_by(id=note_id, user_id=user_id).first()
        if not note:
            return jsonify({"error": "Note not found"}), 404
        session.delete(note)
        session.commit()
        return jsonify({"message": "Deleted"})
    finally:
        session.close()


@notes_bp.route("/notes/property-ids", methods=["GET"])
@require_auth()
def notes_property_ids():
    """Returns property_ids that have at least one note for this user."""
    user_id = str(g.current_user.id)
    session = _new_session()
    try:
        rows = (session.query(UserNote.property_id)
                .filter(UserNote.user_id == user_id, UserNote.property_id.isnot(None))
                .distinct()
                .all())
        return jsonify({"ids": [r[0] for r in rows]})
    finally:
        session.close()


# ── Favorites ─────────────────────────────────────────────────────────────────

@notes_bp.route("/favorites", methods=["GET"])
@require_auth()
def list_favorites():
    user_id = str(g.current_user.id)

    session = _new_session()
    try:
        favs = (session.query(UserFavorite)
                .filter_by(user_id=user_id)
                .order_by(UserFavorite.created_at.desc())
                .limit(500)
                .all())
        return jsonify({"favorites": [f.to_dict() for f in favs]})
    finally:
        session.close()


@notes_bp.route("/favorites/ids", methods=["GET"])
@require_auth()
def list_favorite_ids():
    """Lightweight endpoint — returns just property_id strings for fast UI sync."""
    user_id = str(g.current_user.id)

    session = _new_session()
    try:
        rows = (session.query(UserFavorite.property_id)
                .filter_by(user_id=user_id)
                .all())
        return jsonify({"ids": [r[0] for r in rows]})
    finally:
        session.close()


@notes_bp.route("/favorites/<property_id>", methods=["POST"])
@require_auth()
def toggle_favorite(property_id: str):
    """Toggle: creates if missing, deletes if already favorited. Returns {favorited: bool}."""
    user_id = str(g.current_user.id)
    data = request.get_json(silent=True) or {}

    session = _new_session()
    try:
        existing = (session.query(UserFavorite)
                    .filter_by(user_id=user_id, property_id=property_id)
                    .first())
        if existing:
            session.delete(existing)
            session.commit()
            return jsonify({"favorited": False, "property_id": property_id})

        snapshot = data.get("snapshot") or None
        address = (data.get("address") or "").strip() or None
        fav = UserFavorite(
            user_id=user_id,
            property_id=property_id,
            property_address=address,
            property_snapshot=snapshot,
        )
        session.add(fav)
        session.commit()
        return jsonify({"favorited": True, "property_id": property_id, "favorite": fav.to_dict()})
    finally:
        session.close()
