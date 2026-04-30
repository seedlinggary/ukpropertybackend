"""
Local test for the Zoopla scraper.

Runs the same dual-tier logic as production (curl first, UC browser fallback)
but with verbose diagnostics and the browser window open so you can watch.

Usage:
  python test_scraper.py                      # London, 1 page
  python test_scraper.py --city manchester
  python test_scraper.py --pages 2
  python test_scraper.py --save results.json
"""

import argparse
import json
import logging
import os
import re
import sys
import time
import random

sys.path.insert(0, os.path.dirname(__file__))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)
logger = logging.getLogger("test_scraper")

import scrapers.zoopla as _z


def _divider(label=""):
    w = 64
    if label:
        pad = (w - len(label) - 2) // 2
        print(f"\n{'─'*pad} {label} {'─'*pad}")
    else:
        print("─" * w)


# ─────────────────────────────────────────────────────────────
# Diagnostics: print everything useful about what's on the page
# ─────────────────────────────────────────────────────────────

def _diag_curl(html: str, page: int):
    _divider(f"CURL DIAGNOSTICS  page={page}")

    # Check for CF block
    lower = html.lower()
    if any(s in lower for s in ("just a moment", "security verification", "cf-browser-verification")):
        print("  ❌ Cloudflare challenge detected in curl response")
    else:
        print(f"  ✅ curl response looks clean  ({len(html):,} chars)")

    # lsrp-schema present?
    if '"id":"lsrp-schema"' in html:
        print("  ✅ lsrp-schema found in raw HTML")
    else:
        print("  ❌ lsrp-schema NOT found in raw HTML")

    # All £ amounts in the raw HTML
    prices = list(dict.fromkeys(re.findall(r'£[\d,]+', html)))
    print(f"\n  £ prices in raw HTML ({len(prices)} unique):")
    if prices:
        for p in prices[:40]:
            print(f"    {p}")
        if len(prices) > 40:
            print(f"    … and {len(prices)-40} more")
    else:
        print("    (none — likely a block page)")

    print()


def _diag_browser(driver, page: int):
    _divider(f"BROWSER DIAGNOSTICS  page={page}")
    print(f"  Page title : {driver.title!r}")
    print(f"  URL        : {driver.current_url}")

    # lsrp-schema in DOM?
    schema_raw = driver.execute_script(
        "var el=document.getElementById('lsrp-schema'); return el ? el.textContent : null;"
    )
    if schema_raw:
        print(f"  ✅ lsrp-schema in DOM  ({len(schema_raw):,} chars)")
        try:
            data = json.loads(schema_raw)
            for node in data.get("@graph", []):
                if node.get("@type") == "SearchResultsPage":
                    n = len(node.get("mainEntity", {}).get("itemListElement", []))
                    print(f"     → {n} listings in JSON-LD")
        except Exception as e:
            print(f"     → parse error: {e}")
    else:
        print("  ❌ lsrp-schema NOT in DOM")

    # £ prices visible in DOM
    try:
        els = driver.find_elements("xpath", "//*[contains(text(),'£')]")
        found = list(dict.fromkeys(
            m for el in els
            for m in re.findall(r'£[\d,]+', el.text or "")
        ))
        print(f"\n  £ prices visible in DOM ({len(found)} unique):")
        for p in found[:40]:
            print(f"    {p}")
        if len(found) > 40:
            print(f"    … and {len(found)-40} more")
        if not found:
            print("    (none — likely CAPTCHA or block page)")
    except Exception as e:
        print(f"    Error: {e}")

    # Listing row count
    try:
        rows = driver.find_elements("css selector", "[id^='listing_']")
        print(f"\n  Listing rows [id^='listing_'] : {len(rows)}")
    except Exception:
        pass
    print()


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--city",       default="london")
    parser.add_argument("--pages",      type=int, default=1)
    parser.add_argument("--save",       metavar="FILE")
    parser.add_argument("--no-details", action="store_true",
                        help="Skip fetching full description from each listing page")
    args = parser.parse_args()

    _z.MAX_PAGES = args.pages
    city_slug = args.city.lower().replace(" ", "-")
    results = []
    uc_driver = None

    print(f"\n{'='*64}")
    print(f"  Zoopla scraper test  —  city={args.city!r}  pages={args.pages}")
    print(f"{'='*64}")

    try:
        for page in range(1, args.pages + 1):
            url = f"https://www.zoopla.co.uk/for-sale/property/{city_slug}/?pn={page}"
            print(f"\n[Page {page}] {url}")

            items = []
            has_next = False

            # ── Tier 1: curl ──────────────────────────────────────────
            print("  Trying curl_cffi (Chrome TLS impersonation)…")
            html = _z._curl_get(url)

            if html:
                _diag_curl(html, page)
                items = _z._schema_items_from_html(html)
                if items:
                    has_next = _z._html_has_next_page(html, page)
                    print(f"  ✅ curl extracted {len(items)} items")
                else:
                    print("  ⚠️  curl got HTML but found 0 listing items")
            else:
                print("  ❌ curl returned nothing (blocked or error)")

            # ── Tier 2: UC browser ────────────────────────────────────
            if not items:
                print("\n  Launching undetected Chrome browser (visible)…")
                if uc_driver is None:
                    uc_driver = _z._make_uc_driver(headless=False)

                uc_driver.get(url)
                print("  Waiting for page to load (3 s)…")
                time.sleep(3)

                cf_ok = _z._wait_for_cloudflare(uc_driver, timeout=30)
                _diag_browser(uc_driver, page)

                if not cf_ok:
                    print("  ❌ Cloudflare challenge not resolved — stopping")
                    break

                items = _z._dom_schema_items(uc_driver)
                has_next = _z._dom_has_next_page(uc_driver)
                print(f"  {'✅' if items else '❌'} browser extracted {len(items)} items")

            # ── Normalise ─────────────────────────────────────────────
            if not items:
                print(f"  No items on page {page} — stopping")
                break

            page_listings = []
            for item in items:
                listing = _z._normalize(item, args.city)
                if listing:
                    page_listings.append(listing)

            # ── Enrich with full description ───────────────────────────
            fetch_details = not args.no_details
            if fetch_details and page_listings:
                print(f"\n  Fetching full descriptions for {len(page_listings)} listings…")
                for idx, listing in enumerate(page_listings, 1):
                    detail_url = listing.get("listing_url", "")
                    sys.stdout.write(f"\r    [{idx}/{len(page_listings)}] {detail_url[:70]:<70}")
                    sys.stdout.flush()
                    full_desc = _z._fetch_full_description(detail_url)
                    if full_desc:
                        listing["description"] = full_desc
                    time.sleep(random.uniform(0.5, 1.5))
                print()  # newline after progress line

            results.extend(page_listings)
            print(f"\n  Page {page} done: {len(page_listings)} listings → {len(results)} total")

            if not has_next:
                print("  No next page found — finished")
                break

            if page < args.pages:
                delay = random.uniform(2.0, 3.5)
                print(f"  Waiting {delay:.1f}s before next page…")
                time.sleep(delay)

    except KeyboardInterrupt:
        print("\n  Interrupted.")
    finally:
        if uc_driver:
            input("\n  Press ENTER to close the browser…")
            try:
                uc_driver.quit()
            except Exception:
                pass

    # ─── Results ───────────────────────────────────────────────
    _divider("RESULTS")
    print(f"  {len(results)} listings extracted\n")

    if not results:
        print("  Nothing extracted. Check diagnostics above.")
        print("  Common causes:")
        print("    • Cloudflare blocked both curl AND browser")
        print("    • lsrp-schema not in HTML (try again; it's sometimes absent on first load)")
        print("    • City slug wrong — use lowercase, hyphens: 'east-london'")
        sys.exit(0)

    for i, r in enumerate(results, 1):
        price     = f"£{r['price']:,}" if r.get("price") else "—"
        size      = f"{r['size_m2']} m²" if r.get("size_m2") else "—"
        full_desc = r.get("description") or ""
        # Wrap description at 80 chars for readability
        desc_lines = []
        for chunk in [full_desc[j:j+80] for j in range(0, len(full_desc), 80)]:
            desc_lines.append(f"               {chunk}")

        print(f"[{i:>3}] {r.get('address') or '(no address)'}")
        print(f"       Price     : {price}   Beds: {r.get('bedrooms') or '?'}   Baths: {r.get('bathrooms') or '?'}   Size: {size}")
        print(f"       Type      : {r.get('property_type') or '—'}")
        print(f"       Lat/Lng   : {r.get('lat')}, {r.get('lng')}")
        print(f"       URL       : {r.get('listing_url')}")
        print(f"       Image     : {r.get('image_url') or '—'}")
        if full_desc:
            print(f"       About     ({len(full_desc)} chars):")
            for line in desc_lines[:6]:     # print up to ~480 chars
                print(line)
            if len(desc_lines) > 6:
                print(f"               … ({len(full_desc) - 480} more chars — use --save to see all)")
        else:
            print("       About     : —")
        print()

    if args.save:
        with open(args.save, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print(f"  Saved to {args.save}")


if __name__ == "__main__":
    main()
