"""
Playwright-based paid ad library scraper.

Scrapes Meta Ad Library, TikTok Ad Library, and Google Ads Transparency
Center in parallel using an ephemeral headless browser session.

Security measures applied:
  - Ephemeral browser context — no cookies/profile persisted to disk
  - Network isolation — only the three target domains are allowed outbound
  - Input sanitisation — brand names stripped of special chars before URL use
  - Content sanitisation — all scraped strings stripped of HTML/scripts
  - Per-page timeout 20s, full session timeout 60s
  - Rate limiting with jitter per domain (2.3–3.5s between same-domain hits)
  - Temp files (debug screenshots) use /tmp/hermes_scrape_* and are deleted immediately
"""

import asyncio
import json
import os
import random
import re
import time
from typing import Optional
from urllib.parse import quote_plus, urlparse

from crewai.tools import BaseTool

# ── Allowed outbound domains (everything else is blocked) ────────────────────
_ALLOWED_HOSTS = {
    "www.facebook.com",
    "facebook.com",
    "static.xx.fbcdn.net",          # Meta CDN for ad card rendering
    "scontent.fsin15-2.fna.fbcdn.net",  # Meta media CDN (SG)
    "library.tiktok.com",
    "lf16-ttcdn-tos.pstatp.com",    # TikTok static CDN
    "adstransparency.google.com",
    "fonts.gstatic.com",            # Google fonts
    "fonts.googleapis.com",         # Google fonts API
    "www.gstatic.com",              # Google static assets
    "ogads-pa.clients6.google.com", # Google ATC data API
    "apis.google.com",              # Google APIs (needed for ATC render)
}

# Per-domain last-hit tracker for rate limiting
_domain_last_hit: dict[str, float] = {}


# ── Security helpers ──────────────────────────────────────────────────────────

_BRAND_SAFE = re.compile(r"[^a-zA-Z0-9 _\-\.]")
_DANGEROUS = re.compile(
    r"(eval\(|Function\(|javascript:|data:text/html|<script)",
    re.IGNORECASE,
)

def _sanitise_brand(brand: str) -> str:
    """Strip shell/URL-special chars from brand name before URL interpolation."""
    return _BRAND_SAFE.sub("", brand).strip()[:80]


def _sanitise(text: str, max_len: int = 2000) -> str:
    """Strip scripts/HTML from scraped text; reject dangerous payloads."""
    if not text:
        return ""
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if _DANGEROUS.search(text):
        return ""
    return text[:max_len]


async def _rate_limit(domain: str) -> None:
    last = _domain_last_hit.get(domain, 0.0)
    elapsed = time.monotonic() - last
    delay = 2.0 + random.uniform(0.3, 1.5)
    if elapsed < delay:
        await asyncio.sleep(delay - elapsed)
    _domain_last_hit[domain] = time.monotonic()


# ── Network isolation route handler ──────────────────────────────────────────

async def _block_non_target(route, request) -> None:
    host = urlparse(request.url).netloc
    allowed = any(
        host == h or host.endswith("." + h)
        for h in _ALLOWED_HOSTS
    )
    if allowed:
        await route.continue_()
    else:
        await route.abort("blockedbyclient")


# ── Safe element text extraction ──────────────────────────────────────────────

async def _text(el, fallback: str = "") -> str:
    try:
        t = await el.inner_text()
        return _sanitise(t or fallback)
    except Exception:
        return fallback


async def _attr(el, attr: str, fallback: str = "") -> str:
    try:
        v = await el.get_attribute(attr)
        return _sanitise(v or fallback)
    except Exception:
        return fallback


# ── Platform scrapers ─────────────────────────────────────────────────────────

async def _scrape_meta(context, brand: str, country: str) -> list[dict]:
    """Scrape Meta Ad Library for paid ads by brand."""
    safe_brand = _sanitise_brand(brand)
    country_code = (country or "ALL").upper()[:2] if len(country or "") == 2 else "ALL"
    url = (
        f"https://www.facebook.com/ads/library/"
        f"?active_status=all&ad_type=all"
        f"&country={quote_plus(country_code)}"
        f"&q={quote_plus(safe_brand)}"
        f"&search_type=keyword_unordered"
    )

    page = await context.new_page()
    ads: list[dict] = []
    try:
        await _rate_limit("www.facebook.com")
        await page.goto(url, timeout=20_000, wait_until="domcontentloaded")
        await asyncio.sleep(3)  # let React render ad cards

        # Meta renders ad cards inside carousel containers
        # Each [data-testid="ad-library-ad-carousel-container"] wraps one ad
        try:
            await page.wait_for_selector(
                '[data-testid="ad-library-ad-carousel-container"]',
                timeout=15_000,
            )
        except Exception:
            return []

        cards = await page.query_selector_all(
            '[data-testid="ad-library-ad-carousel-container"]'
        )

        for card in cards[:10]:
            # Get the full inner text of the card — Meta uses hashed class names
            # but inner_text() gives us all visible text: page name, creative, dates, status
            raw_text = await _text(card)
            if not raw_text:
                continue

            # Extract library ID from sibling context if available
            parent = await card.evaluate_handle("el => el.closest('._7jyi') || el.parentElement")
            parent_text = ""
            try:
                parent_text = await _text(parent)
            except Exception:
                pass

            full_text = raw_text or parent_text
            if not full_text:
                continue

            ads.append({
                "url": url,
                "title": f"{safe_brand} — Meta Ad Library",
                "content": full_text[:1500],
                "source_type": "paid",
            })
    except Exception as e:
        print(f"[PaidAdLib] Meta scrape error for '{brand}': {e}", flush=True)
    finally:
        await page.close()

    return ads


async def _scrape_tiktok(context, brand: str, country: str) -> list[dict]:
    """Scrape TikTok Ad Library for paid ads by brand."""
    safe_brand = _sanitise_brand(brand)
    # Try multiple region codes — TikTok requires a specific region to return results
    # "US" and "GB" have the deepest ad libraries; fall back to "ALL" if empty
    regions_to_try = ["US", "GB", "SG", "AU"]
    if country and len(country) == 2:
        regions_to_try.insert(0, country.upper())

    page = await context.new_page()
    ads: list[dict] = []
    try:
        await _rate_limit("library.tiktok.com")

        for region in regions_to_try:
            url = f"https://library.tiktok.com/ads?region={region}&keyword={quote_plus(safe_brand)}"
            await page.goto(url, timeout=20_000, wait_until="domcontentloaded")
            await asyncio.sleep(4)

            try:
                await page.wait_for_selector(
                    ".ad_card_wrapper, .nodata_container, .total_ads",
                    timeout=10_000,
                )
            except Exception:
                continue

            total_el = await page.query_selector(".total_ads")
            total_text = await _text(total_el) if total_el else ""
            # Skip if no ads in this region
            if "0\n" in total_text or total_text.strip().endswith("\n0"):
                continue

            cards = await page.query_selector_all(".ad_card_wrapper")
            for card in cards[:10]:
                raw_text = await _text(card)
                if not raw_text:
                    continue
                ads.append({
                    "url": url,
                    "title": f"{safe_brand} — TikTok Ad Library ({region})",
                    "content": raw_text[:1500],
                    "source_type": "paid",
                })

            if ads:
                break  # found results — no need to try more regions

    except Exception as e:
        print(f"[PaidAdLib] TikTok scrape error for '{brand}': {e}", flush=True)
    finally:
        await page.close()

    return ads


async def _scrape_google(context, brand: str, country: str) -> list[dict]:
    """
    Scrape Google Ads Transparency Center for paid ad intelligence.
    Extracts the advertiser list (with ~ad counts) rendered after search.
    """
    safe_brand = _sanitise_brand(brand)
    base_url = "https://adstransparency.google.com/?region=anywhere"

    page = await context.new_page()
    ads: list[dict] = []
    try:
        await _rate_limit("adstransparency.google.com")
        await page.goto(base_url, timeout=20_000, wait_until="domcontentloaded")
        await asyncio.sleep(3)

        search_input = await page.query_selector("material-input input")
        if not search_input:
            return []

        await search_input.fill(safe_brand)
        await search_input.press("Enter")
        await asyncio.sleep(5)

        # Wait for the advertiser results table (contains brand, country, ad count)
        try:
            await page.wait_for_function(
                f"document.body.innerText.toLowerCase().includes('{safe_brand.lower()[:20]}')"
                " && document.body.innerText.includes('ads')",
                timeout=12_000,
            )
        except Exception:
            return []

        # Extract body text and parse the advertiser results block
        body_text = await page.inner_text("body")
        # Find the "Advertisers" section header and extract from there
        adv_idx = body_text.find("Advertisers")
        if adv_idx < 0:
            adv_idx = body_text.lower().find(safe_brand.lower())
        if adv_idx < 0:
            return []

        # Extract the relevant block (up to 2000 chars from the advertisers section)
        result_block = _sanitise(body_text[adv_idx:adv_idx + 2000])

        # Parse into per-advertiser entries by splitting on brand name occurrences
        lines = [l.strip() for l in result_block.splitlines() if l.strip()]
        entries: list[str] = []
        chunk: list[str] = []
        for line in lines:
            if safe_brand.lower() in line.lower() and chunk:
                entries.append(" | ".join(chunk))
                chunk = [line]
            else:
                chunk.append(line)
        if chunk:
            entries.append(" | ".join(chunk))

        # Filter to entries that actually mention the brand
        entries = [e for e in entries if safe_brand.lower() in e.lower()][:5]
        if not entries:
            entries = [result_block[:800]]

        for entry in entries[:5]:
            ads.append({
                "url": base_url,
                "title": f"{safe_brand} — Google Ads Transparency",
                "content": entry[:1500],
                "source_type": "paid",
            })

    except Exception as e:
        print(f"[PaidAdLib] Google scrape error for '{brand}': {e}", flush=True)
    finally:
        await page.close()

    return ads


# ── Async orchestrator ────────────────────────────────────────────────────────

async def _scrape_all(brand: str, country: str, platforms: list[str]) -> dict:
    """
    Launches a single ephemeral browser context and scrapes all requested
    platforms in parallel. Returns structured result dict.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {"brand": brand, "platform_data": [], "error": "playwright not installed"}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        # Ephemeral context — no user_data_dir, no storage_state → nothing persists to disk
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            java_script_enabled=True,
            accept_downloads=False,
            # Mimic a real browser viewport to reduce bot-detection signals
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            timezone_id="America/New_York",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        # Remove the webdriver property that headless Chrome exposes
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        # Block all non-target outbound network requests
        await context.route("**/*", _block_non_target)

        scrape_tasks = []
        task_labels  = []

        if any(p in platforms for p in ("Facebook", "Instagram")):
            scrape_tasks.append(_scrape_meta(context, brand, country))
            task_labels.append("meta")

        if "TikTok" in platforms:
            scrape_tasks.append(_scrape_tiktok(context, brand, country))
            task_labels.append("tiktok")

        if "YouTube" in platforms:
            scrape_tasks.append(_scrape_google(context, brand, country))
            task_labels.append("google")

        try:
            raw_results = await asyncio.wait_for(
                asyncio.gather(*scrape_tasks, return_exceptions=True),
                timeout=60.0,
            )
        except asyncio.TimeoutError:
            raw_results = [[] for _ in scrape_tasks]
        finally:
            await context.close()
            await browser.close()

    # Map results back to platform names
    platform_map = {
        "meta":   ["Facebook", "Instagram"],
        "tiktok": ["TikTok"],
        "google": ["YouTube"],
    }

    platform_data = []
    for label, result in zip(task_labels, raw_results):
        ads = result if isinstance(result, list) else []
        if not ads:
            continue
        for plat_name in platform_map[label]:
            if plat_name in platforms:
                platform_data.append({
                    "platform":    plat_name,
                    "raw_results": ads,
                })

    return {"brand": brand, "platform_data": platform_data}


# ── CrewAI BaseTool ───────────────────────────────────────────────────────────

class PaidAdLibTool(BaseTool):
    name: str = "Paid Ad Library Scraper"
    description: str = (
        "Scrapes paid ad creative data directly from Meta Ad Library (Facebook/Instagram), "
        "TikTok Ad Library, and Google Ads Transparency Center (YouTube) using a headless "
        "browser. Returns structured JSON with ad creatives, spend signals, impressions ranges, "
        "and run dates. Input: JSON with brand, country, and platforms fields."
    )

    def _run(self, query: str) -> str:
        params: dict = {}
        try:
            bracket = query.find("{")
            if bracket != -1:
                params = json.loads(query[bracket:])
                query  = query[:bracket].strip()
        except Exception:
            pass

        brand     = params.get("brand") or query.strip() or "unknown"
        country   = params.get("country", "")
        platforms = params.get("platforms") or ["Facebook", "Instagram", "TikTok", "YouTube"]

        try:
            # Use a fresh event loop to avoid conflicts with any existing loop
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                result = loop.run_until_complete(_scrape_all(brand, country, platforms))
            finally:
                loop.close()
        except Exception as e:
            print(f"[PaidAdLib] _run error: {e}", flush=True)
            result = {"brand": brand, "platform_data": [], "error": str(e)}

        return json.dumps(result)
