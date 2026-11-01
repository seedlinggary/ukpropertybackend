"""
Stripe webhook handler.
All business logic for subscription state is driven by events here.

POST /stripe/webhook   — Stripe sends events here
GET  /stripe/prices    — public price list (requires Stripe to be configured)
"""
import os
import logging

from flask import Blueprint, request, jsonify

from database import SessionLocal
from models.user import User

logger = logging.getLogger(__name__)
stripe_bp = Blueprint("stripe", __name__, url_prefix="/stripe")

STRIPE_SECRET_KEY    = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")


def _stripe():
    if not STRIPE_SECRET_KEY:
        return None
    import stripe as _stripe
    _stripe.api_key = STRIPE_SECRET_KEY
    return _stripe


@stripe_bp.route("/prices", methods=["GET"])
def public_prices():
    """Public endpoint — returns tier pricing info from DB (no Stripe required)."""
    from models.tier import Tier, TierFeature
    session = SessionLocal()
    try:
        tiers = session.query(Tier).filter_by(is_active=True).order_by(Tier.sort_order).all()
        result = []
        for t in tiers:
            features = [f.to_dict() for f in session.query(TierFeature).filter_by(tier_id=t.id, enabled=True)]
            d = t.to_dict()
            d["features"] = features
            result.append(d)
        return jsonify({
            "tiers": result,
            "stripe_configured": bool(STRIPE_SECRET_KEY),
        })
    finally:
        session.close()


@stripe_bp.route("/webhook", methods=["POST"])
def webhook():
    s = _stripe()
    if not s:
        return jsonify({"error": "Stripe not configured"}), 503

    payload   = request.get_data()
    sig_header = request.headers.get("Stripe-Signature", "")

    if STRIPE_WEBHOOK_SECRET:
        try:
            event = s.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
        except Exception as exc:
            logger.warning("Stripe webhook signature error: %s", exc)
            return jsonify({"error": "Invalid signature"}), 400
    else:
        import json
        try:
            event = json.loads(payload)
        except Exception:
            return jsonify({"error": "Bad payload"}), 400

    event_type = event.get("type", "")
    data_obj   = event.get("data", {}).get("object", {})

    session = SessionLocal()
    try:
        if event_type in (
            "customer.subscription.created",
            "customer.subscription.updated",
            "customer.subscription.deleted",
        ):
            customer_id = data_obj.get("customer")
            status      = data_obj.get("status")
            sub_id      = data_obj.get("id")

            user = session.query(User).filter_by(stripe_customer_id=customer_id).first()
            if user:
                user.stripe_subscription_id = sub_id
                user.subscription_status    = status
                if event_type == "customer.subscription.deleted":
                    user.tier_id = None
                else:
                    # Map Stripe metadata tier_id set at checkout
                    meta_tier = data_obj.get("metadata", {}).get("tier_id")
                    if meta_tier:
                        user.tier_id = meta_tier
                session.commit()

        elif event_type == "checkout.session.completed":
            meta_user = data_obj.get("metadata", {}).get("user_id")
            meta_tier = data_obj.get("metadata", {}).get("tier_id")
            customer_id = data_obj.get("customer")
            if meta_user and meta_tier:
                user = session.query(User).filter_by(id=meta_user).first()
                if user:
                    user.stripe_customer_id     = customer_id or user.stripe_customer_id
                    user.tier_id                = meta_tier
                    user.subscription_status    = "active"
                    user.stripe_subscription_id = data_obj.get("subscription")
                    session.commit()

        return jsonify({"received": True})
    except Exception:
        logger.exception("webhook processing error")
        session.rollback()
        return jsonify({"error": "Processing error"}), 500
    finally:
        session.close()
