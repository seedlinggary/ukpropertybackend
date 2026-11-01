"""
Mortgage rates — public read, admin CRUD + WhatsApp setting.

GET    /mortgage-rates           — list active rates + contact_whatsapp (public)
POST   /mortgage-rates           — create rate (admin)
PUT    /mortgage-rates/settings  — update WhatsApp number (admin)
PUT    /mortgage-rates/<id>      — update rate (admin)
DELETE /mortgage-rates/<id>      — delete rate (admin)
"""
import uuid

from flask import Blueprint, request, jsonify, g
from sqlalchemy import text

from database import SessionLocal
from middleware.auth import require_auth
from models.mortgage_rate import MortgageRate
from models.site_settings import SiteSetting

mortgage_rates_bp = Blueprint("mortgage_rates", __name__)

_WHATSAPP_KEY = "mortgage_advisor_whatsapp"


def _session():
    s = SessionLocal()
    s.execute(text("SET statement_timeout = '5s'"))
    return s


def _require_admin():
    user = getattr(g, "current_user", None)
    if not user:
        return None, (jsonify({"error": "Authentication required"}), 401)
    if user.role not in ("admin", "super_admin"):
        return None, (jsonify({"error": "Admin access required"}), 403)
    return user, None


def _get_whatsapp(session) -> str | None:
    row = session.query(SiteSetting).filter_by(key=_WHATSAPP_KEY).first()
    return row.value_text if row else None


# ── Public ────────────────────────────────────────────────────────────────────

@mortgage_rates_bp.route("/mortgage-rates", methods=["GET"])
def list_rates():
    session = _session()
    try:
        rows = (
            session.query(MortgageRate)
            .filter_by(is_active=True)
            .order_by(MortgageRate.sort_order, MortgageRate.rate)
            .all()
        )
        return jsonify({
            "rates":             [r.to_dict() for r in rows],
            "contact_whatsapp":  _get_whatsapp(session),
        })
    finally:
        session.close()


# ── Admin: WhatsApp setting ───────────────────────────────────────────────────

@mortgage_rates_bp.route("/mortgage-rates/settings", methods=["PUT"])
@require_auth()
def update_settings():
    _, err = _require_admin()
    if err:
        return err

    body    = request.get_json(silent=True) or {}
    wa_num  = (body.get("contact_whatsapp") or "").strip() or None

    session = _session()
    try:
        row = session.query(SiteSetting).filter_by(key=_WHATSAPP_KEY).first()
        if row:
            row.value_text    = wa_num
            row.updated_by_id = g.current_user.id
        else:
            row = SiteSetting(key=_WHATSAPP_KEY, value_text=wa_num,
                              updated_by_id=g.current_user.id)
            session.add(row)
        session.commit()
        return jsonify({"contact_whatsapp": wa_num})
    finally:
        session.close()


# ── Admin: rates CRUD ─────────────────────────────────────────────────────────

_ALLOWED_FIELDS = {
    "lender", "rate", "product_fee", "true_cost", "example",
    "product_type", "ltv_tier", "repayment_type", "is_active", "sort_order",
}


@mortgage_rates_bp.route("/mortgage-rates", methods=["POST"])
@require_auth()
def create_rate():
    _, err = _require_admin()
    if err:
        return err

    body = request.get_json(silent=True) or {}
    for required in ("lender", "rate", "product_type", "ltv_tier"):
        if not body.get(required) and body.get(required) != 0:
            return jsonify({"error": f"Missing field: {required}"}), 400

    session = _session()
    try:
        mr = MortgageRate(
            lender         = str(body["lender"]).strip(),
            rate           = float(body["rate"]),
            product_fee    = int(body.get("product_fee") or 0),
            true_cost      = str(body.get("true_cost") or "").strip(),
            example        = str(body.get("example") or "").strip(),
            product_type   = str(body["product_type"]).strip(),
            ltv_tier       = int(body["ltv_tier"]),
            repayment_type = str(body.get("repayment_type") or "both"),
            is_active      = bool(body.get("is_active", True)),
            sort_order     = int(body.get("sort_order") or 0),
        )
        session.add(mr)
        session.commit()
        return jsonify(mr.to_dict()), 201
    finally:
        session.close()


@mortgage_rates_bp.route("/mortgage-rates/<rate_id>", methods=["PUT"])
@require_auth()
def update_rate(rate_id: str):
    _, err = _require_admin()
    if err:
        return err

    try:
        rid = uuid.UUID(rate_id)
    except ValueError:
        return jsonify({"error": "Invalid id"}), 400

    body    = request.get_json(silent=True) or {}
    session = _session()
    try:
        mr = session.query(MortgageRate).filter_by(id=rid).first()
        if not mr:
            return jsonify({"error": "Not found"}), 404

        for field in _ALLOWED_FIELDS:
            if field not in body:
                continue
            val = body[field]
            if field == "rate":
                val = float(val)
            elif field in ("product_fee", "ltv_tier", "sort_order"):
                val = int(val)
            elif field == "is_active":
                val = bool(val)
            else:
                val = str(val).strip() if val is not None else ""
            setattr(mr, field, val)

        session.commit()
        return jsonify(mr.to_dict())
    finally:
        session.close()


@mortgage_rates_bp.route("/mortgage-rates/<rate_id>", methods=["DELETE"])
@require_auth()
def delete_rate(rate_id: str):
    _, err = _require_admin()
    if err:
        return err

    try:
        rid = uuid.UUID(rate_id)
    except ValueError:
        return jsonify({"error": "Invalid id"}), 400

    session = _session()
    try:
        mr = session.query(MortgageRate).filter_by(id=rid).first()
        if not mr:
            return jsonify({"error": "Not found"}), 404
        session.delete(mr)
        session.commit()
        return jsonify({"deleted": True})
    finally:
        session.close()
