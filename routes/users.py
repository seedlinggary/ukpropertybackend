"""
User profile endpoints.

GET  /users/me/tier          — current user's tier + features
PUT  /users/me               — update name/avatar
POST /users/me/subscription  — create/change Stripe subscription
GET  /users/me/billing       — Stripe customer portal URL
"""
import os
import logging

from flask import Blueprint, request, jsonify, g

from middleware.auth import require_auth
from database import SessionLocal
from models.user import User
from models.tier import Tier, TierFeature

logger = logging.getLogger(__name__)
users_bp = Blueprint("users", __name__, url_prefix="/users")

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")


@users_bp.route("/me", methods=["PUT"])
@require_auth()
def update_profile():
    user = g.current_user
    session = g.db_session
    try:
        body = request.get_json(silent=True) or {}
        if "name" in body:
            user.name = (body["name"] or "").strip() or None
        if "avatar_url" in body:
            user.avatar_url = body["avatar_url"] or None
        session.commit()
        return jsonify({"user": user.to_dict()})
    except Exception:
        logger.exception("update_profile error")
        session.rollback()
        return jsonify({"error": "Update failed"}), 500
    finally:
        session.close()


@users_bp.route("/me/tier", methods=["GET"])
@require_auth()
def my_tier():
    user = g.current_user
    session = g.db_session
    try:
        tier = session.query(Tier).filter_by(id=user.tier_id).first() if user.tier_id else None
        features = []
        if tier:
            features = [
                f.to_dict()
                for f in session.query(TierFeature).filter_by(tier_id=tier.id, enabled=True)
            ]
        return jsonify({"tier": tier.to_dict() if tier else None, "features": features})
    finally:
        session.close()


@users_bp.route("/tiers", methods=["GET"])
def list_tiers():
    """Public — list active tiers with features (for pricing page)."""
    session = SessionLocal()
    try:
        tiers = session.query(Tier).filter_by(is_active=True).order_by(Tier.sort_order).all()
        result = []
        for t in tiers:
            features = [f.to_dict() for f in session.query(TierFeature).filter_by(tier_id=t.id, enabled=True)]
            d = t.to_dict()
            d["features"] = features
            result.append(d)
        return jsonify({"tiers": result})
    finally:
        session.close()


@users_bp.route("/me/checkout", methods=["POST"])
@require_auth()
def create_checkout():
    """Create a Stripe Checkout session to subscribe/upgrade."""
    if not STRIPE_SECRET_KEY:
        return jsonify({"error": "Payments not yet configured", "coming_soon": True}), 503

    user = g.current_user
    session = g.db_session
    try:
        import stripe
        stripe.api_key = STRIPE_SECRET_KEY

        body = request.get_json(silent=True) or {}
        tier_id   = body.get("tier_id")
        interval  = body.get("interval", "monthly")  # monthly | yearly

        tier = session.query(Tier).filter_by(id=tier_id, is_active=True).first()
        if not tier:
            return jsonify({"error": "Tier not found"}), 404

        price_id = tier.stripe_price_id_yearly if interval == "yearly" else tier.stripe_price_id_monthly
        if not price_id:
            return jsonify({"error": "No Stripe price configured for this tier", "coming_soon": True}), 503

        # Get or create Stripe customer
        customer_id = user.stripe_customer_id
        if not customer_id:
            customer = stripe.Customer.create(email=user.email, name=user.name or user.email)
            customer_id = customer.id
            user.stripe_customer_id = customer_id
            session.commit()

        frontend_url = os.environ.get("FRONTEND_URL", "http://localhost:5173")
        checkout = stripe.checkout.Session.create(
            customer=customer_id,
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=f"{frontend_url}/profile?checkout=success",
            cancel_url=f"{frontend_url}/profile?checkout=cancel",
            metadata={"user_id": str(user.id), "tier_id": str(tier.id)},
        )
        return jsonify({"checkout_url": checkout.url})
    except Exception:
        logger.exception("create_checkout error")
        session.rollback()
        return jsonify({"error": "Checkout creation failed"}), 500
    finally:
        session.close()


@users_bp.route("/me/billing", methods=["GET"])
@require_auth()
def billing_portal():
    """Return Stripe customer portal URL."""
    if not STRIPE_SECRET_KEY:
        return jsonify({"error": "Payments not yet configured", "coming_soon": True}), 503

    user = g.current_user
    session = g.db_session
    try:
        import stripe
        stripe.api_key = STRIPE_SECRET_KEY

        if not user.stripe_customer_id:
            return jsonify({"error": "No active subscription"}), 404

        frontend_url = os.environ.get("FRONTEND_URL", "http://localhost:5173")
        portal = stripe.billing_portal.Session.create(
            customer=user.stripe_customer_id,
            return_url=f"{frontend_url}/profile",
        )
        return jsonify({"portal_url": portal.url})
    except Exception:
        logger.exception("billing_portal error")
        return jsonify({"error": "Portal creation failed"}), 500
    finally:
        session.close()
