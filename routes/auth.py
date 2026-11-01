"""
Authentication endpoints.

POST /auth/register            — email+password
POST /auth/login               — email+password
POST /auth/google              — Google ID-token
POST /auth/refresh             — swap refresh token → new access+refresh
GET  /auth/me                  — current user info
POST /auth/logout              — client-side (stateless)
POST /auth/forgot-password     — send reset link via email
POST /auth/reset-password      — consume reset token, set new password
"""
import os
import logging
from datetime import datetime, timezone

import bcrypt
from flask import Blueprint, request, jsonify, g

from database import SessionLocal
from models.user import User, LoginEvent
from middleware.auth import (
    make_access_token, make_refresh_token, decode_token, require_auth,
    make_password_reset_token, decode_password_reset_token,
)

logger = logging.getLogger(__name__)
auth_bp = Blueprint("auth", __name__, url_prefix="/auth")

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")


def _record_login(session, user: User, method: str) -> None:
    user.last_login_at = datetime.now(timezone.utc)
    event = LoginEvent(
        user_id=user.id,
        ip_address=request.remote_addr,
        user_agent=request.headers.get("User-Agent", "")[:500],
        method=method,
    )
    session.add(event)


def _token_pair(user_id) -> dict:
    return {
        "access_token":  make_access_token(user_id),
        "refresh_token": make_refresh_token(user_id),
        "token_type":    "Bearer",
    }


@auth_bp.route("/register", methods=["POST"])
def register():
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    password = body.get("password", "")
    name = (body.get("name") or "").strip()

    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400

    session = SessionLocal()
    try:
        if session.query(User).filter_by(email=email).first():
            return jsonify({"error": "Email already registered"}), 409

        pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        user = User(email=email, name=name or None, password_hash=pw_hash)
        session.add(user)
        _record_login(session, user, "password")
        session.commit()
        tokens = _token_pair(user.id)
        return jsonify({"user": user.to_dict(), **tokens}), 201
    except Exception:
        logger.exception("register error")
        session.rollback()
        return jsonify({"error": "Registration failed"}), 500
    finally:
        session.close()


@auth_bp.route("/login", methods=["POST"])
def login():
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    password = body.get("password", "")

    if not email or not password:
        return jsonify({"error": "Email and password are required"}), 400

    session = SessionLocal()
    try:
        user = session.query(User).filter_by(email=email, is_active=True).first()
        if not user or not user.password_hash:
            return jsonify({"error": "Invalid credentials"}), 401
        if not bcrypt.checkpw(password.encode(), user.password_hash.encode()):
            return jsonify({"error": "Invalid credentials"}), 401

        _record_login(session, user, "password")
        session.commit()
        tokens = _token_pair(user.id)
        return jsonify({"user": user.to_dict(), **tokens})
    except Exception:
        logger.exception("login error")
        session.rollback()
        return jsonify({"error": "Login failed"}), 500
    finally:
        session.close()


@auth_bp.route("/google", methods=["POST"])
def google_login():
    body = request.get_json(silent=True) or {}
    id_token_str = body.get("id_token") or body.get("credential")

    if not id_token_str:
        return jsonify({"error": "Google ID token required"}), 400
    if not GOOGLE_CLIENT_ID:
        return jsonify({"error": "Google login not configured on server"}), 503

    try:
        from google.oauth2 import id_token as google_id_token
        from google.auth.transport import requests as google_requests
        idinfo = google_id_token.verify_oauth2_token(
            id_token_str, google_requests.Request(), GOOGLE_CLIENT_ID
        )
    except Exception as exc:
        logger.warning("Google token verification failed: %s", exc)
        return jsonify({"error": "Invalid Google token"}), 401

    google_id   = idinfo["sub"]
    email       = idinfo.get("email", "").lower()
    name        = idinfo.get("name")
    avatar_url  = idinfo.get("picture")

    session = SessionLocal()
    try:
        user = session.query(User).filter_by(google_id=google_id).first()
        if not user and email:
            user = session.query(User).filter_by(email=email).first()

        if user:
            user.google_id  = user.google_id or google_id
            user.avatar_url = avatar_url or user.avatar_url
            user.name       = user.name or name
        else:
            user = User(
                email=email, name=name, google_id=google_id,
                avatar_url=avatar_url,
            )
            session.add(user)

        _record_login(session, user, "google")
        session.commit()
        tokens = _token_pair(user.id)
        return jsonify({"user": user.to_dict(), **tokens})
    except Exception:
        logger.exception("google_login error")
        session.rollback()
        return jsonify({"error": "Login failed"}), 500
    finally:
        session.close()


@auth_bp.route("/refresh", methods=["POST"])
def refresh():
    body = request.get_json(silent=True) or {}
    refresh_token = body.get("refresh_token")
    if not refresh_token:
        return jsonify({"error": "refresh_token required"}), 400

    payload = decode_token(refresh_token, "refresh")
    if not payload:
        return jsonify({"error": "Invalid or expired refresh token"}), 401

    session = SessionLocal()
    try:
        user = session.query(User).filter_by(id=payload["sub"], is_active=True).first()
        if not user:
            return jsonify({"error": "User not found"}), 401
        tokens = _token_pair(user.id)
        return jsonify(tokens)
    finally:
        session.close()


@auth_bp.route("/me", methods=["GET"])
@require_auth()
def me():
    user = g.current_user
    session = g.db_session
    try:
        from models.tier import Tier, TierFeature
        tier = session.query(Tier).filter_by(id=user.tier_id).first() if user.tier_id else None
        features = []
        if tier:
            features = [f.feature_key for f in session.query(TierFeature).filter_by(tier_id=tier.id, enabled=True)]

        from models.tier import AdminPermission
        perms = []
        if user.role in ("admin", "super_admin"):
            if user.role == "super_admin":
                from models.tier import ADMIN_PERMISSION_KEYS
                perms = ADMIN_PERMISSION_KEYS
            else:
                perms = [p.permission_key for p in session.query(AdminPermission).filter_by(user_id=user.id)]

        return jsonify({
            "user": user.to_dict(include_sensitive=True),
            "tier": tier.to_dict() if tier else None,
            "features": features,
            "permissions": perms,
        })
    finally:
        session.close()


@auth_bp.route("/logout", methods=["POST"])
def logout():
    return jsonify({"message": "Logged out"})


@auth_bp.route("/forgot-password", methods=["POST"])
def forgot_password():
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "Email is required"}), 400

    api_key = os.environ.get("RESEND_API_KEY", "")
    if not api_key:
        return jsonify({"error": "Email service not configured"}), 503

    session = SessionLocal()
    try:
        user = session.query(User).filter_by(email=email, is_active=True).first()
        # Always return 200 so we don't reveal whether the email exists
        if not user or not user.password_hash:
            return jsonify({"message": "If that email is registered you'll receive a reset link."})

        fingerprint = (user.password_hash or "")[:8]
        token = make_password_reset_token(email, fingerprint)

        frontend_url = os.environ.get("FRONTEND_URL", "http://localhost:5173")
        reset_link = f"{frontend_url}/auth?mode=reset&token={token}"

        name_or_email = user.name or email
        html = f"""<!DOCTYPE html>
<html>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#111827;max-width:520px;margin:0 auto;padding:32px 16px">
  <h2 style="margin:0 0 8px">Reset your PropSearch password</h2>
  <p style="color:#6b7280;margin-bottom:24px">Hi {name_or_email}, we received a request to reset your password.</p>
  <a href="{reset_link}"
     style="display:inline-block;background:#2563eb;color:#fff;padding:12px 24px;border-radius:6px;text-decoration:none;font-weight:600">
    Reset password
  </a>
  <p style="font-size:13px;color:#9ca3af;margin-top:24px">
    This link expires in 1 hour. If you didn't request a reset, you can safely ignore this email.
  </p>
</body>
</html>"""

        import resend
        resend.api_key = api_key
        resend.Emails.send({
            "from": os.environ.get("RESEND_FROM", "onboarding@resend.dev"),
            "to": [email],
            "subject": "Reset your PropSearch password",
            "html": html,
        })
        logger.info("Password reset email sent to %s", email)
        return jsonify({"message": "If that email is registered you'll receive a reset link."})
    except Exception:
        logger.exception("forgot_password error")
        return jsonify({"error": "Failed to send reset email"}), 500
    finally:
        session.close()


@auth_bp.route("/reset-password", methods=["POST"])
def reset_password():
    body = request.get_json(silent=True) or {}
    token    = body.get("token", "")
    password = body.get("password", "")

    if not token or not password:
        return jsonify({"error": "token and password are required"}), 400
    if len(password) < 8:
        return jsonify({"error": "Password must be at least 8 characters"}), 400

    payload = decode_password_reset_token(token)
    if not payload:
        return jsonify({"error": "Reset link is invalid or has expired"}), 400

    email       = payload["sub"]
    fingerprint = payload.get("fp", "")

    session = SessionLocal()
    try:
        user = session.query(User).filter_by(email=email, is_active=True).first()
        if not user:
            return jsonify({"error": "User not found"}), 404

        # Verify the token fingerprint matches current hash (single-use guarantee)
        if (user.password_hash or "")[:8] != fingerprint:
            return jsonify({"error": "Reset link has already been used"}), 400

        user.password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
        _record_login(session, user, "password")
        session.commit()

        tokens = _token_pair(user.id)
        return jsonify({"message": "Password updated", "user": user.to_dict(), **tokens})
    except Exception:
        logger.exception("reset_password error")
        session.rollback()
        return jsonify({"error": "Password reset failed"}), 500
    finally:
        session.close()
