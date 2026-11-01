"""
JWT authentication middleware.

Access tokens:  2-hour lifetime, signed with JWT_SECRET
Refresh tokens: 30-day lifetime, stored as payload type='refresh'
"""
import os
import logging
from datetime import datetime, timedelta, timezone
from functools import wraps

import jwt
from flask import request, jsonify, g

from database import SessionLocal
from models.user import User
from models.tier import AdminPermission

logger = logging.getLogger(__name__)

JWT_SECRET = os.environ.get("JWT_SECRET", "dev-secret-change-in-prod")
ACCESS_TTL  = timedelta(hours=2)
REFRESH_TTL = timedelta(days=30)


def _make_token(user_id: str, token_type: str, ttl: timedelta) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user_id,
        "type": token_type,
        "iat": now,
        "exp": now + ttl,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


RESET_TTL = timedelta(hours=1)


def make_access_token(user_id: str) -> str:
    return _make_token(str(user_id), "access", ACCESS_TTL)


def make_refresh_token(user_id: str) -> str:
    return _make_token(str(user_id), "refresh", REFRESH_TTL)


def make_password_reset_token(email: str, pw_fingerprint: str) -> str:
    """Signed 1-hour token. pw_fingerprint invalidates it once the password changes."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub":  email,
        "type": "reset",
        "fp":   pw_fingerprint,
        "iat":  now,
        "exp":  now + RESET_TTL,
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def decode_password_reset_token(token: str) -> dict | None:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        if payload.get("type") != "reset":
            return None
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def decode_token(token: str, expected_type: str = "access") -> dict | None:
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        if payload.get("type") != expected_type:
            return None
        return payload
    except jwt.ExpiredSignatureError:
        return None
    except jwt.InvalidTokenError:
        return None


def _get_bearer() -> str | None:
    header = request.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header[7:]
    return None


def require_auth(roles: list[str] | None = None, permissions: list[str] | None = None):
    """
    Decorator factory.

    @require_auth()                        — any authenticated user
    @require_auth(roles=["admin","super_admin"])
    @require_auth(permissions=["view_login_stats"])
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            token = _get_bearer()
            if not token:
                return jsonify({"error": "Authentication required"}), 401

            payload = decode_token(token, "access")
            if not payload:
                return jsonify({"error": "Invalid or expired token"}), 401

            session = SessionLocal()
            try:
                user = session.query(User).filter_by(id=payload["sub"], is_active=True).first()
                if not user:
                    return jsonify({"error": "User not found"}), 401

                if roles and user.role not in roles and user.role != "super_admin":
                    return jsonify({"error": "Insufficient permissions"}), 403

                if permissions:
                    if user.role == "super_admin":
                        pass  # super_admin has everything
                    elif user.role == "admin":
                        granted = {
                            p.permission_key
                            for p in session.query(AdminPermission).filter_by(user_id=user.id)
                        }
                        missing = set(permissions) - granted
                        if missing:
                            return jsonify({"error": "Insufficient permissions"}), 403
                    else:
                        return jsonify({"error": "Insufficient permissions"}), 403

                g.current_user = user
                g.db_session = session
                return fn(*args, **kwargs)
            except Exception:
                logger.exception("require_auth middleware error")
                session.close()
                return jsonify({"error": "Internal server error"}), 500
            finally:
                # Only close if not already done inside the route (caller must close g.db_session)
                pass
        return wrapper
    return decorator
