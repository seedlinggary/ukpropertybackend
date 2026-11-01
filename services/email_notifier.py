"""
Daily property match email service.

Builds and sends each user their personalised daily/weekly digest.
The scheduler job is wired in scheduler.py but left DISABLED (commented out)
until you are ready to start sending real emails.

Usage (from scheduler):
    from services.email_notifier import send_daily_match_emails
    send_daily_match_emails()
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

RESEND_API_KEY  = os.environ.get("RESEND_API_KEY", "")
FROM_EMAIL      = os.environ.get("EMAIL_FROM", "noreply@propcompass.co.uk")
FRONTEND_URL    = os.environ.get("FRONTEND_URL", "http://localhost:5173")
MAX_PER_EMAIL   = 10   # maximum properties shown in the email digest


def _build_html(user_name: str, matches: list[dict], for_you_url: str) -> str:
    name = user_name or "there"
    items_html = ""
    for m in matches[:MAX_PER_EMAIL]:
        address   = m.get("address") or m.get("street") or "Unknown address"
        postcode  = m.get("postcode") or ""
        price_str = f"£{m['price']:,}" if m.get("price") else "Price on request"
        score     = m.get("match_score", 0)
        reasons   = m.get("match_reasons") or []
        url       = m.get("listing_url") or for_you_url
        beds_str  = f"{m['bedrooms']} bed · " if m.get("bedrooms") else ""
        type_str  = (m.get("property_type") or "").capitalize()

        reason_html = "".join(
            f'<li style="color:#6b7280;font-size:13px;">{r}</li>'
            for r in reasons[:3]
        )

        items_html += f"""
        <div style="border:1px solid #e5e7eb;border-radius:8px;padding:16px;margin-bottom:16px;">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;">
            <div>
              <p style="margin:0;font-weight:600;font-size:15px;">{address}</p>
              <p style="margin:4px 0 0;color:#6b7280;font-size:13px;">{postcode} &nbsp;·&nbsp; {beds_str}{type_str}</p>
            </div>
            <div style="text-align:right;">
              <p style="margin:0;font-weight:700;color:#2563eb;font-size:17px;">{price_str}</p>
              <span style="background:#dbeafe;color:#1d4ed8;padding:2px 8px;border-radius:999px;font-size:12px;font-weight:600;">
                {score:.0f}% match
              </span>
            </div>
          </div>
          {"<ul style='margin:8px 0 0;padding-left:20px;'>" + reason_html + "</ul>" if reasons else ""}
          <a href="{url}" style="display:inline-block;margin-top:12px;padding:8px 16px;background:#2563eb;color:#fff;border-radius:6px;text-decoration:none;font-size:13px;font-weight:600;">
            View Property →
          </a>
        </div>
        """

    return f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f9fafb;margin:0;padding:32px 16px;">
  <div style="max-width:560px;margin:0 auto;background:#fff;border-radius:12px;overflow:hidden;border:1px solid #e5e7eb;">
    <div style="background:#2563eb;padding:24px 32px;">
      <h1 style="color:#fff;margin:0;font-size:22px;">Your daily property matches</h1>
      <p style="color:#bfdbfe;margin:6px 0 0;font-size:14px;">{datetime.utcnow().strftime("%A, %d %B %Y")}</p>
    </div>
    <div style="padding:24px 32px;">
      <p style="margin:0 0 20px;color:#374151;">Hi {name}, here are today's properties that match your criteria:</p>
      {items_html}
      <div style="text-align:center;margin-top:24px;">
        <a href="{for_you_url}" style="display:inline-block;padding:12px 28px;background:#f3f4f6;color:#374151;border-radius:8px;text-decoration:none;font-size:14px;font-weight:600;">
          View all matches →
        </a>
      </div>
      <hr style="margin:24px 0;border:none;border-top:1px solid #e5e7eb;">
      <p style="color:#9ca3af;font-size:12px;text-align:center;margin:0;">
        You're receiving this because you enabled email notifications in your preferences.<br>
        <a href="{FRONTEND_URL}/preferences" style="color:#6b7280;">Update preferences</a>
      </p>
    </div>
  </div>
</body>
</html>"""


def _send_via_resend(to_email: str, subject: str, html: str) -> bool:
    if not RESEND_API_KEY:
        logger.warning("RESEND_API_KEY not set — email not sent to %s", to_email)
        return False
    try:
        import resend
        resend.api_key = RESEND_API_KEY
        resend.Emails.send({
            "from": FROM_EMAIL,
            "to": [to_email],
            "subject": subject,
            "html": html,
        })
        logger.info("Digest email sent to %s", to_email)
        return True
    except Exception:
        logger.exception("Failed to send digest to %s", to_email)
        return False


def send_daily_match_emails() -> None:
    """
    Intended to be called by the scheduler once per day (after the scrape completes).
    Finds all users with email_notifications enabled, runs matching for each,
    and sends a digest if there are new matches since their last email.
    """
    from database import SessionLocal
    from models.user import User
    from models.preferences import UserPropertyPrefs
    from models.property_listing import PropertyListing
    from sqlalchemy import text
    from services.matching import run_matching

    logger.info("[email_notifier] Starting daily digest run")
    session = SessionLocal()
    try:
        session.execute(text("SET statement_timeout = '10s'"))

        # Fetch all users who want email notifications
        prefs_rows = (
            session.query(UserPropertyPrefs)
            .filter(
                UserPropertyPrefs.prefs_json["email_notifications"].astext.cast(
                    __import__("sqlalchemy").Boolean
                ) == True
            )
            .all()
        )

        if not prefs_rows:
            logger.info("[email_notifier] No users have email notifications enabled")
            return

        # Properties added in the last 2 days (fresh listings only)
        cutoff = datetime.utcnow() - timedelta(days=2)
        fresh_props = (
            session.query(PropertyListing)
            .filter(PropertyListing.created_at >= cutoff)
            .limit(500)  # cap to protect Nano compute
            .all()
        )
        prop_dicts = [p.to_dict() for p in fresh_props]
        logger.info("[email_notifier] %d fresh properties to match against", len(prop_dicts))

        for row in prefs_rows:
            try:
                user = session.query(User).filter_by(id=row.user_id, is_active=True).first()
                if not user:
                    continue
                detail = row.to_dict()
                prefs = detail["prefs"]

                matches = run_matching(prefs, prop_dicts, min_score=30.0)
                if not matches:
                    logger.debug("[email_notifier] No matches for %s", user.email)
                    continue

                for_you_url = f"{FRONTEND_URL}/for-you"
                html = _build_html(user.name or "", matches, for_you_url)
                count = len(matches)
                subject = f"{count} new propert{'y' if count == 1 else 'ies'} match your criteria"
                _send_via_resend(user.email, subject, html)
            except Exception:
                logger.exception("[email_notifier] Failed for user %s", row.user_id)
    finally:
        session.close()
    logger.info("[email_notifier] Daily digest run complete")
