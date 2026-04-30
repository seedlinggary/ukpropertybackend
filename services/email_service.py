"""
Post-scrape email report via Resend.

Sends one email per scraper run containing:
  • HTML summary — source, per-city stats, errors
  • JSON attachment — every newly-added property (full data)

Required env vars:
  RESEND_API_KEY   — from resend.com dashboard
  RESEND_FROM      — verified sender, e.g. "Scraper <you@yourdomain.com>"
                     (defaults to onboarding@resend.dev for dev/testing)
  REPORT_EMAIL     — recipient address
"""

import base64
import json
import logging
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def send_scrape_report(
    source: str,
    started_at: datetime,
    completed_at: datetime,
    city_stats: List[Dict[str, Any]],   # [{city, added, checked, stop_reason}]
    total_added: int,
    total_seen: int,
    new_properties: List[Dict[str, Any]],
    error: Optional[str] = None,
) -> None:
    """
    Build and dispatch the scrape-completion email.
    Logs a warning and returns silently if anything fails so it never
    blocks the main scraper flow.
    """
    api_key = os.getenv("RESEND_API_KEY", "")
    if not api_key:
        logger.warning("[email] RESEND_API_KEY not set — skipping report email")
        return

    try:
        import resend
        resend.api_key = api_key

        subject = (
            f"{'✅' if not error else '❌'} {source.title()} scrape — "
            f"{total_added} new properties added"
        )

        html = _build_html(
            source, started_at, completed_at,
            city_stats, total_added, total_seen, error,
        )

        attachment_content = _build_attachment(new_properties, source, completed_at)

        params: Dict[str, Any] = {
            "from": os.getenv("RESEND_FROM", "onboarding@resend.dev"),
            "to": [os.getenv("REPORT_EMAIL", "gary.s.schwartz617@gmail.com")],
            "subject": subject,
            "html": html,
        }

        if attachment_content:
            params["attachments"] = [attachment_content]

        result = resend.Emails.send(params)
        logger.info("[email] Report sent — id=%s", result.get("id") if isinstance(result, dict) else result)

    except Exception:
        logger.warning("[email] Failed to send report", exc_info=True)


# ─────────────────────────────────────────────────────────────
# HTML builder
# ─────────────────────────────────────────────────────────────

def _build_html(
    source: str,
    started_at: datetime,
    completed_at: datetime,
    city_stats: List[Dict[str, Any]],
    total_added: int,
    total_seen: int,
    error: Optional[str],
) -> str:
    duration_s = int((completed_at - started_at).total_seconds())
    duration   = f"{duration_s // 60}m {duration_s % 60}s"

    status_color = "#dc2626" if error else "#16a34a"
    status_label = "Failed" if error else "Completed"

    city_rows = ""
    for stat in city_stats:
        stop = stat.get("stop_reason", "—")
        added   = stat.get("added", 0)
        checked = stat.get("checked", 0)
        city_rows += f"""
        <tr>
          <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb">{stat['city'].title()}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;text-align:center">{added}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;text-align:center">{checked - added}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;text-align:center">{checked}</td>
          <td style="padding:8px 12px;border-bottom:1px solid #e5e7eb;color:#6b7280;font-size:13px">{stop}</td>
        </tr>"""

    error_block = ""
    if error:
        error_block = f"""
        <div style="margin-top:24px;padding:12px 16px;background:#fef2f2;border:1px solid #fecaca;border-radius:6px">
          <strong style="color:#dc2626">Error:</strong>
          <pre style="margin:8px 0 0;white-space:pre-wrap;font-size:13px;color:#7f1d1d">{error}</pre>
        </div>"""

    return f"""<!DOCTYPE html>
<html>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
             color:#111827;max-width:680px;margin:0 auto;padding:32px 16px">

  <div style="display:flex;align-items:center;gap:12px;margin-bottom:24px">
    <h1 style="margin:0;font-size:22px">🏠 PropSearch — Scrape Report</h1>
  </div>

  <!-- Status banner -->
  <div style="padding:12px 16px;background:{status_color}1a;border:1px solid {status_color}40;
              border-radius:8px;margin-bottom:24px">
    <span style="color:{status_color};font-weight:600">{status_label}</span>
    &nbsp;·&nbsp; Source: <strong>{source.title()}</strong>
    &nbsp;·&nbsp; Duration: {duration}
  </div>

  <!-- Summary numbers -->
  <div style="display:flex;gap:16px;margin-bottom:28px">
    {_stat_box("New Listings Added", str(total_added), "#2563eb")}
    {_stat_box("Total Checked", str(total_seen), "#6b7280")}
    {_stat_box("Cities", str(len(city_stats)), "#7c3aed")}
  </div>

  <!-- Timeline -->
  <table style="width:100%;border-collapse:collapse;font-size:14px;margin-bottom:28px">
    <tr>
      <td style="padding:6px 0;color:#6b7280;width:130px">Started</td>
      <td>{started_at.strftime('%d %b %Y, %H:%M:%S')} UTC</td>
    </tr>
    <tr>
      <td style="padding:6px 0;color:#6b7280">Completed</td>
      <td>{completed_at.strftime('%d %b %Y, %H:%M:%S')} UTC</td>
    </tr>
  </table>

  <!-- Per-city breakdown -->
  <h2 style="font-size:16px;margin:0 0 12px">By City</h2>
  <table style="width:100%;border-collapse:collapse;font-size:14px">
    <thead>
      <tr style="background:#f9fafb">
        <th style="padding:8px 12px;text-align:left;border-bottom:2px solid #e5e7eb">City</th>
        <th style="padding:8px 12px;text-align:center;border-bottom:2px solid #e5e7eb">Added</th>
        <th style="padding:8px 12px;text-align:center;border-bottom:2px solid #e5e7eb">Skipped</th>
        <th style="padding:8px 12px;text-align:center;border-bottom:2px solid #e5e7eb">Checked</th>
        <th style="padding:8px 12px;text-align:left;border-bottom:2px solid #e5e7eb">Stop reason</th>
      </tr>
    </thead>
    <tbody>
      {city_rows}
    </tbody>
  </table>

  {error_block}

  <p style="margin-top:32px;font-size:13px;color:#9ca3af">
    Full property data attached as <code>properties_*.json</code><br>
    PropSearch auto-scraper · runs every 12 hours
  </p>

</body>
</html>"""


def _stat_box(label: str, value: str, color: str) -> str:
    return f"""
    <div style="flex:1;padding:16px;background:#f9fafb;border:1px solid #e5e7eb;border-radius:8px;text-align:center">
      <div style="font-size:28px;font-weight:700;color:{color}">{value}</div>
      <div style="font-size:12px;color:#6b7280;margin-top:4px">{label}</div>
    </div>"""


# ─────────────────────────────────────────────────────────────
# JSON attachment builder
# ─────────────────────────────────────────────────────────────

def _build_attachment(
    properties: List[Dict[str, Any]],
    source: str,
    completed_at: datetime,
) -> Optional[Dict[str, Any]]:
    if not properties:
        return None
    try:
        # Make everything JSON-serialisable (e.g. UUID objects)
        safe = json.loads(json.dumps(properties, default=str))
        json_bytes = json.dumps(safe, indent=2, ensure_ascii=False).encode("utf-8")
        b64 = base64.b64encode(json_bytes).decode("utf-8")
        filename = f"{source}_properties_{completed_at.strftime('%Y%m%d_%H%M%S')}.json"
        return {"filename": filename, "content": b64}
    except Exception:
        logger.warning("[email] Could not build JSON attachment", exc_info=True)
        return None
