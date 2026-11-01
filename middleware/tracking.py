"""
Feature usage tracking.

Usage:
    from middleware.tracking import track_feature

    @planning_bp.route("/api/planning/search")
    @track_feature("planning_search", action="search")
    def planning_search():
        ...

Design decisions:
- Inserts happen in a background thread so they never add latency to the response.
- In-memory dedup: only one DB row per (user_id_or_ip, feature_key) per 10-minute bucket.
  This caps writes to at most 6 per user per feature per hour — safe on Nano compute.
- Anonymous users are tracked by IP so we can count reach even before login.
"""
import logging
import threading
from datetime import datetime, timezone
from functools import wraps

from flask import request, g

logger = logging.getLogger(__name__)

# In-memory dedup set: (bucket_10min, user_key, feature_key)
_seen: set = set()
_seen_lock = threading.Lock()

_MAX_SEEN = 50_000   # cap memory usage; pruned by age bucket rotation


def _bucket() -> str:
    """Current 10-minute bucket string, e.g. '2026-10-11T14:30'."""
    now = datetime.now(timezone.utc)
    return f"{now.year}-{now.month:02}-{now.day:02}T{now.hour:02}:{(now.minute // 10) * 10:02}"


def _insert(user_id, feature_key: str, action: str | None, ip: str | None) -> None:
    try:
        from database import SessionLocal
        from models.feature_usage import FeatureUsage
        session = SessionLocal()
        try:
            row = FeatureUsage(
                user_id=user_id,
                feature_key=feature_key,
                action=action,
                ip_address=ip,
                created_at=datetime.now(timezone.utc),
            )
            session.add(row)
            session.commit()
        except Exception:
            session.rollback()
            logger.debug("feature_usage insert failed", exc_info=True)
        finally:
            session.close()
    except Exception:
        logger.debug("feature_usage background thread error", exc_info=True)


def record_feature(feature_key: str, action: str | None = None) -> None:
    """Call this anywhere (route body, helper) to record a feature usage event."""
    user_id = getattr(g, "current_user", None)
    user_id = str(user_id.id) if user_id else None
    ip = request.remote_addr

    user_key = user_id or ip or "anon"
    bucket   = _bucket()
    dedup_key = (bucket, user_key, feature_key)

    with _seen_lock:
        if dedup_key in _seen:
            return
        # Prune if too large (drop everything — next request rebuilds it)
        if len(_seen) > _MAX_SEEN:
            _seen.clear()
        _seen.add(dedup_key)

    t = threading.Thread(
        target=_insert,
        args=(user_id, feature_key, action, ip),
        daemon=True,
    )
    t.start()


def track_feature(feature_key: str, action: str | None = None):
    """
    Decorator that records a feature usage event on every successful response.

    @track_feature("planning_search", action="search")
    def my_route(): ...
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            response = fn(*args, **kwargs)
            # Only track if response is not an error (status < 400)
            status = getattr(response, "status_code", 200)
            if isinstance(response, tuple):
                status = response[1] if len(response) > 1 else 200
            if status < 400:
                record_feature(feature_key, action)
            return response
        return wrapper
    return decorator
