"""
Post-scrape email report via Resend.

Sends one email per scraper run containing:
  • HTML summary — source, per-city stats, per-tier fetch breakdown, errors
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

# Tier display config: (key, label, credits_per_call)
_TIERS = [
    ("curl",          "Direct curl",   0),
    ("browser",       "Browser (UC)",  0),
    ("scraperapi_dc", "ScraperAPI DC", 1),
    ("scraperapi_gb", "ScraperAPI GB", 5),
]


def send_scrape_report(
    source: str,
    started_at: datetime,
    completed_at: datetime,
    city_stats: List[Dict[str, Any]],   # [{city, added, checked, stop_reason, tier_stats}]
    total_added: int,
    total_seen: int,
    new_properties: List[Dict[str, Any]],
    error: Optional[str] = None,
    section_label: str = "By City",
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
            section_label=section_label,
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
    section_label: str = "By City",
) -> str:
    duration_s = int((completed_at - started_at).total_seconds())
    duration   = f"{duration_s // 60}m {duration_s % 60}s"

    status_color = "#dc2626" if error else "#16a34a"
    status_label = "Failed" if error else "Completed"

    # Aggregate ScraperAPI credits across all cities
    total_dc_attempts = 0
    total_gb_attempts = 0
    for stat in city_stats:
        ts = stat.get("tier_stats") or {}
        for ctx in ("search", "detail"):
            ctx_ts = ts.get(ctx) or {}
            total_dc_attempts += (ctx_ts.get("scraperapi_dc") or {}).get("attempts", 0)
            total_gb_attempts += (ctx_ts.get("scraperapi_gb") or {}).get("attempts", 0)
    total_credits = total_dc_attempts * 1 + total_gb_attempts * 5

    city_sections = ""
    for stat in city_stats:
        city_sections += _city_section(stat)

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
             color:#111827;max-width:720px;margin:0 auto;padding:32px 16px">

  <div style="margin-bottom:24px">
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
    {_stat_box(section_label.split()[-1], str(len(city_stats)), "#7c3aed")}
    {_stat_box("ScraperAPI Credits", str(total_credits), "#d97706")}
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

  <!-- Per-city/source breakdown -->
  <h2 style="font-size:16px;margin:0 0 16px">{section_label}</h2>
  {city_sections}

  {error_block}

  <p style="margin-top:32px;font-size:13px;color:#9ca3af">
    Full property data attached as <code>properties_*.json</code><br>
    PropSearch auto-scraper · runs every 12 hours
  </p>

</body>
</html>"""


def _city_section(stat: Dict[str, Any]) -> str:
    city     = stat.get("city", "?").title()
    added    = stat.get("added", 0)
    checked  = stat.get("checked", 0)
    skipped  = checked - added
    stop     = stat.get("stop_reason", "—")
    ts       = stat.get("tier_stats") or {}

    tier_rows = ""
    for ctx_key, ctx_label in (("search", "Search pages"), ("detail", "Detail pages")):
        ctx_ts = ts.get(ctx_key) or {}
        cells = ""
        for tier_key, _, credits in _TIERS:
            t = ctx_ts.get(tier_key) or {}
            attempts  = t.get("attempts", 0)
            successes = t.get("successes", 0)
            if attempts == 0:
                cell_text  = '<span style="color:#9ca3af">—</span>'
            else:
                ok    = successes == attempts
                color = "#16a34a" if ok else ("#d97706" if successes > 0 else "#dc2626")
                icon  = "✅" if ok else ("⚠️" if successes > 0 else "❌")
                cr_note = f" <span style='color:#d97706;font-size:11px'>({attempts * credits}cr)</span>" if credits > 0 and attempts > 0 else ""
                cell_text = f'<span style="color:{color}">{icon} {successes}/{attempts}</span>{cr_note}'
            cells += f'<td style="padding:6px 10px;text-align:center;border-bottom:1px solid #f3f4f6;font-size:13px">{cell_text}</td>'

        tier_rows += f"""
        <tr>
          <td style="padding:6px 10px;border-bottom:1px solid #f3f4f6;font-size:13px;color:#374151;font-weight:500">{ctx_label}</td>
          {cells}
        </tr>"""

    tier_header_cells = "".join(
        f'<th style="padding:6px 10px;text-align:center;border-bottom:1px solid #e5e7eb;font-weight:600;font-size:12px;color:#6b7280">{label}</th>'
        for _, label, _ in _TIERS
    )

    return f"""
  <div style="margin-bottom:24px;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden">
    <!-- City header -->
    <div style="padding:10px 14px;background:#f9fafb;border-bottom:1px solid #e5e7eb;display:flex;gap:20px;align-items:center">
      <span style="font-weight:700;font-size:15px">{city}</span>
      <span style="font-size:13px;color:#374151">Added: <strong style="color:#2563eb">{added}</strong></span>
      <span style="font-size:13px;color:#374151">Skipped: {skipped}</span>
      <span style="font-size:13px;color:#374151">Checked: {checked}</span>
      <span style="font-size:13px;color:#6b7280">Stop: {stop}</span>
    </div>
    <!-- Tier breakdown -->
    <table style="width:100%;border-collapse:collapse">
      <thead>
        <tr style="background:#fafafa">
          <th style="padding:6px 10px;text-align:left;border-bottom:1px solid #e5e7eb;font-weight:600;font-size:12px;color:#6b7280">Context</th>
          {tier_header_cells}
        </tr>
      </thead>
      <tbody>
        {tier_rows}
      </tbody>
    </table>
  </div>"""


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
