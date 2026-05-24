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
    "static.xx.fbcdn.net",        # Meta CDN — needed for ad card rendering
    "library.tiktok.com",
    "lf16-ttcdn-tos.pstatp.com",  # TikTok static CDN
    "adstransparency.google.com",
    "fonts.gstatic.com",          # Google fonts — needed for Transparency Center render
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

        # Wait for ad cards or a "no results" indicator
        try:
            await page.wait_for_selector(
                '[data-testid="ad-library-ad-card"], [data-testid="no-results"]',
                timeout=20_000,
            )
        except Exception:
            return []

        cards = await page.query_selector_all('[data-testid="ad-library-ad-card"]')
        for card in cards[:10]:
            creative = await _text(
                await card.query_selector('[data-testid="ad-library-ad-card-body"]') or card
            )
            page_name = await _text(
                await card.query_selector('[data-testid="page-name"]')
            )
            impressions = await _text(
                await card.query_selector('[data-testid="ad-library-ad-card-impressions"]')
            )
            start_date = await _text(
                await card.query_selector('[data-testid="ad-library-ad-card-date"]')
            )
            status = await _text(
                await card.query_selector('[data-testid="ad-status"]')
            )
            ad_id = await _attr(card, "data-ad-id", "")
            ad_url = (
                f"https://www.facebook.com/ads/library/?id={ad_id}"
                if ad_id else url
            )

            content_parts = [p for p in [creative, page_name, impressions, start_date, status] if p]
            if not content_parts:
                continue

            ads.append({
                "url": ad_url,
                "title": f"{safe_brand} — Meta Ad Library",
                "content": " | ".join(content_parts),
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
    # TikTok Ad Library uses epoch ms for date range — last 30 days
    start_ms = int((time.time() - 30 * 86400) * 1000)
    url = (
        f"https://library.tiktok.com/ads"
        f"?region=ALL"
        f"&start_time={start_ms}"
        f"&keyword={quote_plus(safe_brand)}"
    )

    page = await context.new_page()
    ads: list[dict] = []
    try:
        await _rate_limit("library.tiktok.com")
        await page.goto(url, timeout=20_000, wait_until="domcontentloaded")

        try:
            await page.wait_for_selector(
                '[data-e2e="ad-card"], [data-e2e="no-result"]',
                timeout=20_000,
            )
        except Exception:
            return []

        cards = await page.query_selector_all('[data-e2e="ad-card"]')
        for card in cards[:10]:
            title     = await _text(await card.query_selector('[data-e2e="ad-title"]'))
            advertiser= await _text(await card.query_selector('[data-e2e="advertiser-name"]'))
            impression= await _text(await card.query_selector('[data-e2e="ad-impression"]'))
            region    = await _text(await card.query_selector('[data-e2e="ad-region"]'))

            content_parts = [p for p in [title, advertiser, impression, region] if p]
            if not content_parts:
                continue

            ads.append({
                "url": url,
                "title": f"{safe_brand} — TikTok Ad Library",
                "content": " | ".join(content_parts),
                "source_type": "paid",
            })
    except Exception as e:
        print(f"[PaidAdLib] TikTok scrape error for '{brand}': {e}", flush=True)
    finally:
        await page.close()

    return ads


async def _scrape_google(context, brand: str, country: str) -> list[dict]:
    """Scrape Google Ads Transparency Center for paid ads by brand."""
    safe_brand = _sanitise_brand(brand)
    url = (
        f"https://adstransparency.google.com/"
        f"?advertiser_name={quote_plus(safe_brand)}&region=anywhere"
    )

    page = await context.new_page()
    ads: list[dict] = []
    try:
        await _rate_limit("adstransparency.google.com")
        await page.goto(url, timeout=20_000, wait_until="domcontentloaded")

        try:
            await page.wait_for_selector(
                "material-creative-preview, [data-creative-id]",
                timeout=20_000,
            )
        except Exception:
            return []

        cards = await page.query_selector_all(
            "material-creative-preview, [data-creative-id]"
        )
        for card in cards[:10]:
            advertiser = await _text(await card.query_selector(".advertiser-name"))
            date_range = await _text(await card.query_selector(".date-range"))
            region     = await _text(await card.query_selector(".region"))
            ad_format  = await _text(await card.query_selector(".ad-format"))
            # Capture creative asset URL only — never download the asset
            asset_el   = await card.query_selector("img[src], video[src]")
            asset_url  = await _attr(asset_el, "src") if asset_el else ""

            content_parts = [p for p in [advertiser, date_range, region, ad_format] if p]
            if asset_url:
                content_parts.append(f"Creative: {asset_url[:200]}")
            if not content_parts:
                continue

            ads.append({
                "url": url,
                "title": f"{safe_brand} — Google Ads Transparency",
                "content": " | ".join(content_parts),
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
            accept_downloads=False,   # never download files
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
            result = asyncio.run(_scrape_all(brand, country, platforms))
        except Exception as e:
            print(f"[PaidAdLib] _run error: {e}", flush=True)
            result = {"brand": brand, "platform_data": [], "error": str(e)}

        return json.dumps(result)
