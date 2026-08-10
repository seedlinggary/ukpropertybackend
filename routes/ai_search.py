import json
import logging
import os

from flask import Blueprint, jsonify, request

ai_search_bp = Blueprint("ai_search", __name__)
log = logging.getLogger(__name__)

_SYSTEM_PROMPT = """You parse UK property search queries into structured filters.
Return ONLY a JSON object with these keys (use null if not mentioned):
{
  "city":          string or null,
  "min_price":     integer or null,
  "max_price":     integer or null,
  "bedrooms":      integer or null,
  "property_type": "flat" | "house" | null,
  "article4":      "yes" | "no" | "any",
  "source":        "zoopla" | "auction_houses" | null,
  "keyword":       string or null,
  "postcodeExact": string or null,
  "tenure":        "freehold" | "leasehold" | "share_of_freehold" | null,
  "summary":       string
}
Rules:
- Convert shorthand prices: "200k"→200000, "1.5m"→1500000, "half a million"→500000
- "studio" → property_type:"flat", bedrooms:1
- "auction", "auction property", "investment" → source:"auction_houses"
- "no article 4", "not article 4", "unrestricted" → article4:"no"
- "article 4", "HMO restricted" → article4:"yes"
- article4 defaults to "any" if not mentioned
- "freehold", "free hold" → tenure:"freehold"
- "leasehold", "lease hold" → tenure:"leasehold"
- "share of freehold", "share freehold" → tenure:"share_of_freehold"
- summary: short readable phrase, e.g. "3-bed flat in Leeds, max £200k, freehold"
- Return only JSON — no explanation text outside the object."""

# Keys we allow through to the frontend. Nothing else passes.
_ALLOWED = frozenset({
    "city", "min_price", "max_price", "bedrooms", "property_type",
    "article4", "source", "keyword", "postcodeExact", "tenure", "summary",
})


@ai_search_bp.route("/api/ai-search", methods=["POST"])
def ai_search():
    data = request.get_json(silent=True) or {}
    query = str(data.get("query", "")).strip()[:500]
    if not query:
        return jsonify({"error": "query required"}), 400

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return jsonify({"error": "AI search not configured on server"}), 503

    try:
        from openai import OpenAI  # imported lazily so the app starts even without the package
        client = OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user",   "content": query},
            ],
            max_tokens=250,
            temperature=0,
            response_format={"type": "json_object"},
        )
        parsed = json.loads(resp.choices[0].message.content)
    except Exception as exc:
        log.exception("ai_search: OpenAI call failed")
        return jsonify({"error": f"AI parse failed: {exc}"}), 500

    # Strict allowlist — OpenAI output only ever maps to property_listings filter params
    safe = {k: v for k, v in parsed.items() if k in _ALLOWED}
    return jsonify(safe)
