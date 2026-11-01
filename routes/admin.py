"""
Admin endpoints.

GET    /admin/users                  — list users (admin + super_admin)
GET    /admin/users/:id              — user detail
PUT    /admin/users/:id              — update role / is_active / tier
DELETE /admin/users/:id              — deactivate user

GET    /admin/tiers                  — list all tiers
POST   /admin/tiers                  — create tier
PUT    /admin/tiers/:id              — update tier
DELETE /admin/tiers/:id              — deactivate tier

GET    /admin/tiers/:id/features     — list features for a tier
POST   /admin/tiers/:id/features     — add feature
PUT    /admin/tiers/:id/features/:fid
DELETE /admin/tiers/:id/features/:fid

GET    /admin/permissions/:user_id   — list permissions for an admin user
POST   /admin/permissions/:user_id   — grant permission
DELETE /admin/permissions/:user_id/:key — revoke permission

GET    /admin/login-stats            — login frequency report
"""
import logging

from flask import Blueprint, request, jsonify, g

from middleware.auth import require_auth
from database import SessionLocal
from models.user import User, LoginEvent
from models.tier import Tier, TierFeature, AdminPermission, KNOWN_FEATURES, ADMIN_PERMISSION_KEYS
from models.notes import UserNote, UserFavorite

logger = logging.getLogger(__name__)
admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


# ── Users ─────────────────────────────────────────────────────────────────────

@admin_bp.route("/users", methods=["GET"])
@require_auth(roles=["admin", "super_admin"])
def list_users():
    session = g.db_session
    try:
        page = int(request.args.get("page", 1))
        per  = min(int(request.args.get("per", 50)), 200)
        q    = (request.args.get("q") or "").strip()

        query = session.query(User)
        if q:
            like = f"%{q}%"
            query = query.filter(
                User.email.ilike(like) | User.name.ilike(like)
            )
        total = query.count()
        users = query.order_by(User.created_at.desc()).offset((page - 1) * per).limit(per).all()
        return jsonify({
            "users": [u.to_dict() for u in users],
            "total": total,
            "page": page,
            "per": per,
        })
    finally:
        session.close()


@admin_bp.route("/users/<user_id>", methods=["GET"])
@require_auth(roles=["admin", "super_admin"])
def get_user(user_id):
    session = g.db_session
    try:
        user = session.query(User).filter_by(id=user_id).first()
        if not user:
            return jsonify({"error": "User not found"}), 404

        tier = session.query(Tier).filter_by(id=user.tier_id).first() if user.tier_id else None
        perms = [p.to_dict() for p in session.query(AdminPermission).filter_by(user_id=user.id)]
        recent_logins = (
            session.query(LoginEvent)
            .filter_by(user_id=user.id)
            .order_by(LoginEvent.created_at.desc())
            .limit(20)
            .all()
        )
        notes_count = session.query(UserNote).filter_by(user_id=user.id).count()
        favs_count = session.query(UserFavorite).filter_by(user_id=user.id).count()
        recent_notes = (
            session.query(UserNote)
            .filter_by(user_id=user.id)
            .order_by(UserNote.updated_at.desc())
            .limit(10)
            .all()
        )
        recent_favs = (
            session.query(UserFavorite)
            .filter_by(user_id=user.id)
            .order_by(UserFavorite.created_at.desc())
            .limit(10)
            .all()
        )
        return jsonify({
            "user": user.to_dict(include_sensitive=True),
            "tier": tier.to_dict() if tier else None,
            "permissions": perms,
            "recent_logins": [
                {"method": e.method, "ip": e.ip_address, "at": e.created_at.isoformat()}
                for e in recent_logins
            ],
            "notes_count": notes_count,
            "favorites_count": favs_count,
            "recent_notes": [n.to_dict() for n in recent_notes],
            "recent_favorites": [f.to_dict() for f in recent_favs],
        })
    finally:
        session.close()


@admin_bp.route("/users/<user_id>", methods=["PUT"])
@require_auth(roles=["super_admin"])
def update_user(user_id):
    session = g.db_session
    try:
        user = session.query(User).filter_by(id=user_id).first()
        if not user:
            return jsonify({"error": "User not found"}), 404

        body = request.get_json(silent=True) or {}
        if "role" in body and body["role"] in ("user", "admin", "super_admin"):
            user.role = body["role"]
        if "is_active" in body:
            user.is_active = bool(body["is_active"])
        if "tier_id" in body:
            user.tier_id = body["tier_id"] or None
        session.commit()
        return jsonify({"user": user.to_dict()})
    except Exception:
        logger.exception("update_user error")
        session.rollback()
        return jsonify({"error": "Update failed"}), 500
    finally:
        session.close()


@admin_bp.route("/users/<user_id>", methods=["DELETE"])
@require_auth(roles=["super_admin"])
def deactivate_user(user_id):
    session = g.db_session
    try:
        user = session.query(User).filter_by(id=user_id).first()
        if not user:
            return jsonify({"error": "User not found"}), 404
        user.is_active = False
        session.commit()
        return jsonify({"message": "User deactivated"})
    finally:
        session.close()


# ── Tiers ─────────────────────────────────────────────────────────────────────

@admin_bp.route("/tiers", methods=["GET"])
@require_auth(roles=["admin", "super_admin"])
def admin_list_tiers():
    session = g.db_session
    try:
        tiers = session.query(Tier).order_by(Tier.sort_order).all()
        result = []
        for t in tiers:
            features = [f.to_dict() for f in session.query(TierFeature).filter_by(tier_id=t.id)]
            d = t.to_dict()
            d["features"] = features
            d["user_count"] = session.query(User).filter_by(tier_id=t.id).count()
            result.append(d)
        return jsonify({"tiers": result, "known_features": KNOWN_FEATURES})
    finally:
        session.close()


@admin_bp.route("/tiers", methods=["POST"])
@require_auth(roles=["super_admin"])
def create_tier():
    session = g.db_session
    try:
        body = request.get_json(silent=True) or {}
        if not body.get("name") or not body.get("display_name"):
            return jsonify({"error": "name and display_name are required"}), 400

        tier = Tier(
            name=body["name"].strip().lower(),
            display_name=body["display_name"].strip(),
            description=body.get("description"),
            price_monthly_cents=int(body.get("price_monthly_cents", 0)),
            price_yearly_cents=int(body.get("price_yearly_cents", 0)),
            stripe_price_id_monthly=body.get("stripe_price_id_monthly"),
            stripe_price_id_yearly=body.get("stripe_price_id_yearly"),
            sort_order=int(body.get("sort_order", 0)),
        )
        session.add(tier)
        session.commit()
        return jsonify({"tier": tier.to_dict()}), 201
    except Exception:
        logger.exception("create_tier error")
        session.rollback()
        return jsonify({"error": "Create failed"}), 500
    finally:
        session.close()


@admin_bp.route("/tiers/<tier_id>", methods=["PUT"])
@require_auth(roles=["super_admin"])
def update_tier(tier_id):
    session = g.db_session
    try:
        tier = session.query(Tier).filter_by(id=tier_id).first()
        if not tier:
            return jsonify({"error": "Tier not found"}), 404

        body = request.get_json(silent=True) or {}
        for field in ("display_name", "description", "stripe_price_id_monthly", "stripe_price_id_yearly"):
            if field in body:
                setattr(tier, field, body[field])
        for field in ("price_monthly_cents", "price_yearly_cents", "sort_order"):
            if field in body:
                setattr(tier, field, int(body[field]))
        if "is_active" in body:
            tier.is_active = bool(body["is_active"])
        session.commit()
        return jsonify({"tier": tier.to_dict()})
    except Exception:
        logger.exception("update_tier error")
        session.rollback()
        return jsonify({"error": "Update failed"}), 500
    finally:
        session.close()


@admin_bp.route("/tiers/<tier_id>/features", methods=["GET"])
@require_auth(roles=["admin", "super_admin"])
def list_tier_features(tier_id):
    session = g.db_session
    try:
        features = session.query(TierFeature).filter_by(tier_id=tier_id).all()
        return jsonify({"features": [f.to_dict() for f in features]})
    finally:
        session.close()


@admin_bp.route("/tiers/<tier_id>/features", methods=["POST"])
@require_auth(roles=["super_admin"])
def add_tier_feature(tier_id):
    session = g.db_session
    try:
        tier = session.query(Tier).filter_by(id=tier_id).first()
        if not tier:
            return jsonify({"error": "Tier not found"}), 404

        body = request.get_json(silent=True) or {}
        key = (body.get("feature_key") or "").strip()
        if not key:
            return jsonify({"error": "feature_key required"}), 400

        existing = session.query(TierFeature).filter_by(tier_id=tier_id, feature_key=key).first()
        if existing:
            for f in ("enabled", "limit_value", "unit", "description"):
                if f in body:
                    setattr(existing, f, body[f])
            session.commit()
            return jsonify({"feature": existing.to_dict()})

        feat = TierFeature(
            tier_id=tier_id,
            feature_key=key,
            enabled=bool(body.get("enabled", True)),
            limit_value=body.get("limit_value"),
            unit=body.get("unit"),
            description=body.get("description"),
        )
        session.add(feat)
        session.commit()
        return jsonify({"feature": feat.to_dict()}), 201
    except Exception:
        logger.exception("add_tier_feature error")
        session.rollback()
        return jsonify({"error": "Operation failed"}), 500
    finally:
        session.close()


@admin_bp.route("/tiers/<tier_id>/features/<feature_id>", methods=["PUT"])
@require_auth(roles=["super_admin"])
def update_tier_feature(tier_id, feature_id):
    session = g.db_session
    try:
        feat = session.query(TierFeature).filter_by(id=feature_id, tier_id=tier_id).first()
        if not feat:
            return jsonify({"error": "Feature not found"}), 404

        body = request.get_json(silent=True) or {}
        for f in ("enabled", "limit_value", "unit", "description"):
            if f in body:
                setattr(feat, f, body[f])
        session.commit()
        return jsonify({"feature": feat.to_dict()})
    except Exception:
        logger.exception("update_tier_feature error")
        session.rollback()
        return jsonify({"error": "Update failed"}), 500
    finally:
        session.close()


@admin_bp.route("/tiers/<tier_id>/features/<feature_id>", methods=["DELETE"])
@require_auth(roles=["super_admin"])
def delete_tier_feature(tier_id, feature_id):
    session = g.db_session
    try:
        feat = session.query(TierFeature).filter_by(id=feature_id, tier_id=tier_id).first()
        if not feat:
            return jsonify({"error": "Feature not found"}), 404
        session.delete(feat)
        session.commit()
        return jsonify({"message": "Deleted"})
    finally:
        session.close()


# ── Admin permissions ──────────────────────────────────────────────────────────

@admin_bp.route("/permissions/<user_id>", methods=["GET"])
@require_auth(roles=["super_admin"])
def list_permissions(user_id):
    session = g.db_session
    try:
        perms = session.query(AdminPermission).filter_by(user_id=user_id).all()
        return jsonify({
            "permissions": [p.to_dict() for p in perms],
            "available_keys": ADMIN_PERMISSION_KEYS,
        })
    finally:
        session.close()


@admin_bp.route("/permissions/<user_id>", methods=["POST"])
@require_auth(roles=["super_admin"])
def grant_permission(user_id):
    session = g.db_session
    try:
        body = request.get_json(silent=True) or {}
        key = (body.get("permission_key") or "").strip()
        if not key:
            return jsonify({"error": "permission_key required"}), 400

        target = session.query(User).filter_by(id=user_id).first()
        if not target:
            return jsonify({"error": "User not found"}), 404
        if target.role not in ("admin", "super_admin"):
            return jsonify({"error": "Permissions can only be granted to admin-role users"}), 400

        existing = session.query(AdminPermission).filter_by(user_id=user_id, permission_key=key).first()
        if existing:
            return jsonify({"permission": existing.to_dict()})

        perm = AdminPermission(
            user_id=user_id,
            permission_key=key,
            granted_by_id=str(g.current_user.id),
        )
        session.add(perm)
        session.commit()
        return jsonify({"permission": perm.to_dict()}), 201
    except Exception:
        logger.exception("grant_permission error")
        session.rollback()
        return jsonify({"error": "Grant failed"}), 500
    finally:
        session.close()


@admin_bp.route("/permissions/<user_id>/<key>", methods=["DELETE"])
@require_auth(roles=["super_admin"])
def revoke_permission(user_id, key):
    session = g.db_session
    try:
        perm = session.query(AdminPermission).filter_by(user_id=user_id, permission_key=key).first()
        if not perm:
            return jsonify({"error": "Permission not found"}), 404
        session.delete(perm)
        session.commit()
        return jsonify({"message": "Revoked"})
    finally:
        session.close()


# ── Login stats ────────────────────────────────────────────────────────────────

@admin_bp.route("/login-stats", methods=["GET"])
@require_auth(permissions=["view_login_stats"])
def login_stats():
    from sqlalchemy import func
    session = g.db_session
    try:
        days = int(request.args.get("days", 30))
        from datetime import datetime, timezone, timedelta
        since = datetime.now(timezone.utc) - timedelta(days=days)

        rows = (
            session.query(LoginEvent.user_id, func.count(LoginEvent.id).label("count"))
            .filter(LoginEvent.created_at >= since)
            .group_by(LoginEvent.user_id)
            .order_by(func.count(LoginEvent.id).desc())
            .limit(100)
            .all()
        )

        user_ids = [str(r.user_id) for r in rows]
        users_map = {
            str(u.id): u
            for u in session.query(User).filter(User.id.in_(user_ids))
        }

        stats = []
        for row in rows:
            u = users_map.get(str(row.user_id))
            stats.append({
                "user_id": str(row.user_id),
                "email": u.email if u else None,
                "name": u.name if u else None,
                "login_count": row.count,
            })

        total_logins = session.query(func.count(LoginEvent.id)).filter(LoginEvent.created_at >= since).scalar()
        unique_users  = session.query(func.count(func.distinct(LoginEvent.user_id))).filter(LoginEvent.created_at >= since).scalar()

        return jsonify({
            "period_days": days,
            "total_logins": total_logins,
            "unique_users": unique_users,
            "top_users": stats,
        })
    finally:
        session.close()


# ── Feature usage analytics ────────────────────────────────────────────────────

@admin_bp.route("/usage/summary", methods=["GET"])
@require_auth(roles=["super_admin"])
def usage_summary():
    """
    Per-feature totals over a window.
    Returns: [ { feature_key, total_uses, unique_users, uses_7d, uses_30d, last_used } ]
    """
    from sqlalchemy import func, case
    from datetime import datetime, timezone, timedelta
    from models.feature_usage import FeatureUsage

    session = g.db_session
    try:
        now   = datetime.now(timezone.utc)
        d7    = now - timedelta(days=7)
        d30   = now - timedelta(days=30)
        d90   = now - timedelta(days=90)

        rows = (
            session.query(
                FeatureUsage.feature_key,
                func.count(FeatureUsage.id).label("total"),
                func.count(func.distinct(FeatureUsage.user_id)).label("unique_users"),
                func.sum(case((FeatureUsage.created_at >= d7,  1), else_=0)).label("uses_7d"),
                func.sum(case((FeatureUsage.created_at >= d30, 1), else_=0)).label("uses_30d"),
                func.max(FeatureUsage.created_at).label("last_used"),
            )
            .filter(FeatureUsage.created_at >= d90)
            .group_by(FeatureUsage.feature_key)
            .order_by(func.count(FeatureUsage.id).desc())
            .all()
        )

        return jsonify({
            "features": [
                {
                    "feature_key":  r.feature_key,
                    "total_uses":   r.total,
                    "unique_users": r.unique_users,
                    "uses_7d":      int(r.uses_7d or 0),
                    "uses_30d":     int(r.uses_30d or 0),
                    "last_used":    r.last_used.isoformat() if r.last_used else None,
                }
                for r in rows
            ]
        })
    finally:
        session.close()


@admin_bp.route("/usage/by-feature/<feature_key>", methods=["GET"])
@require_auth(roles=["super_admin"])
def usage_by_feature(feature_key: str):
    """Top users for a specific feature, with per-action breakdown."""
    from sqlalchemy import func
    from datetime import datetime, timezone, timedelta
    from models.feature_usage import FeatureUsage

    session = g.db_session
    try:
        days  = int(request.args.get("days", 30))
        since = datetime.now(timezone.utc) - timedelta(days=days)

        # Per-user totals
        rows = (
            session.query(
                FeatureUsage.user_id,
                func.count(FeatureUsage.id).label("count"),
                func.max(FeatureUsage.created_at).label("last_used"),
            )
            .filter(FeatureUsage.feature_key == feature_key, FeatureUsage.created_at >= since)
            .group_by(FeatureUsage.user_id)
            .order_by(func.count(FeatureUsage.id).desc())
            .limit(100)
            .all()
        )

        user_ids = [str(r.user_id) for r in rows if r.user_id]
        users_map = {
            str(u.id): u
            for u in session.query(User).filter(User.id.in_(user_ids))
        } if user_ids else {}

        # Per-action breakdown
        action_rows = (
            session.query(FeatureUsage.action, func.count(FeatureUsage.id).label("count"))
            .filter(FeatureUsage.feature_key == feature_key, FeatureUsage.created_at >= since)
            .group_by(FeatureUsage.action)
            .all()
        )

        return jsonify({
            "feature_key": feature_key,
            "period_days": days,
            "by_action": [{"action": r.action, "count": r.count} for r in action_rows],
            "top_users": [
                {
                    "user_id":   str(r.user_id) if r.user_id else None,
                    "email":     users_map.get(str(r.user_id), None) and users_map[str(r.user_id)].email,
                    "name":      users_map.get(str(r.user_id), None) and users_map[str(r.user_id)].name,
                    "count":     r.count,
                    "last_used": r.last_used.isoformat() if r.last_used else None,
                }
                for r in rows
            ],
        })
    finally:
        session.close()


@admin_bp.route("/usage/by-user/<user_id>", methods=["GET"])
@require_auth(roles=["super_admin"])
def usage_by_user(user_id: str):
    """All features used by a specific user with counts."""
    from sqlalchemy import func
    from datetime import datetime, timezone, timedelta
    from models.feature_usage import FeatureUsage

    session = g.db_session
    try:
        days  = int(request.args.get("days", 90))
        since = datetime.now(timezone.utc) - timedelta(days=days)

        rows = (
            session.query(
                FeatureUsage.feature_key,
                FeatureUsage.action,
                func.count(FeatureUsage.id).label("count"),
                func.max(FeatureUsage.created_at).label("last_used"),
            )
            .filter(FeatureUsage.user_id == user_id, FeatureUsage.created_at >= since)
            .group_by(FeatureUsage.feature_key, FeatureUsage.action)
            .order_by(func.count(FeatureUsage.id).desc())
            .all()
        )

        user = session.query(User).filter_by(id=user_id).first()

        return jsonify({
            "user": user.to_dict() if user else None,
            "period_days": days,
            "features": [
                {
                    "feature_key": r.feature_key,
                    "action":      r.action,
                    "count":       r.count,
                    "last_used":   r.last_used.isoformat() if r.last_used else None,
                }
                for r in rows
            ],
        })
    finally:
        session.close()


@admin_bp.route("/usage/timeline", methods=["GET"])
@require_auth(roles=["super_admin"])
def usage_timeline():
    """
    Daily usage counts per feature for the last N days.
    Used to power the line chart.
    Returns: { dates: [...], series: { feature_key: [count_per_day, ...] } }
    """
    from sqlalchemy import func, cast, Date
    from datetime import datetime, timezone, timedelta, date
    from models.feature_usage import FeatureUsage

    session = g.db_session
    try:
        days = min(int(request.args.get("days", 30)), 90)
        since = datetime.now(timezone.utc) - timedelta(days=days)

        rows = (
            session.query(
                cast(FeatureUsage.created_at, Date).label("day"),
                FeatureUsage.feature_key,
                func.count(FeatureUsage.id).label("count"),
            )
            .filter(FeatureUsage.created_at >= since)
            .group_by(cast(FeatureUsage.created_at, Date), FeatureUsage.feature_key)
            .order_by(cast(FeatureUsage.created_at, Date))
            .all()
        )

        # Build date spine
        today = date.today()
        dates = [(today - timedelta(days=days - i)).isoformat() for i in range(days + 1)]

        # Aggregate into series
        from collections import defaultdict
        data: dict = defaultdict(lambda: defaultdict(int))
        for r in rows:
            data[r.feature_key][r.day.isoformat()] = r.count

        # Get feature set from rows
        features = list({r.feature_key for r in rows})

        series = {
            fk: [data[fk].get(d, 0) for d in dates]
            for fk in features
        }

        return jsonify({"dates": dates, "series": series})
    finally:
        session.close()
