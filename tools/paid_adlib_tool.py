"""
Playwright-based paid ad library scraper.

Meta Ad Library + Google Ads Transparency → standard playwright (anonymous context)
TikTok Ad Library → patchright (patches CDP binding leaks) + stealth init scripts
  + optional cookie injection from data/tiktok_cookies.json

Security measures:
  - Ephemeral browser contexts — no profile/cookies persisted to disk
  - Network isolation — only target ad library domains allowed outbound
  - Input sanitisation — brand names stripped of special chars before URL use
  - Content sanitisation — all scraped strings stripped of HTML/scripts
  - Per-page 20s timeout, full session 90s hard cap
  - Rate limiting with jitter (2.3–3.5s per domain)
  - Cookies never written to disk by the browser — only read from data/tiktok_cookies.json

TikTok stealth stack (in order of effect):
  1. patchright — removes __playwright__binding__ and __pwInitScripts CDP artifacts
  2. navigator.webdriver undefined — removes the automation flag
  3. Full browser property spoofing — languages, platform, hardware concurrency,
     deviceMemory, plugins array, chrome object, permissions API
  4. Canvas + WebGL fingerprint noise — adds subtle per-session randomness
  5. Cookie injection — real session cookies suppress cold-session bot scoring
  6. Human-like timing — random delays between interactions
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


# ── TikTok stealth init script ────────────────────────────────────────────────
# Applied to every patchright page before any navigation.
# Covers the JS-level signals TikTok's webmssdk.js checks.
_TIKTOK_STEALTH_JS = """
// 1. Remove automation flag
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});

// 2. Spoof languages / platform
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'platform', {get: () => 'MacIntel'});

// 3. Spoof hardware signals (reduces headless fingerprint)
Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8});
Object.defineProperty(navigator, 'deviceMemory', {get: () => 8});

// 4. Spoof plugins array (headless has 0 plugins — dead giveaway)
Object.defineProperty(navigator, 'plugins', {
    get: () => {
        const makePlugin = (name, desc, filename) => {
            const p = Object.create(Plugin.prototype);
            Object.defineProperty(p, 'name',        {get: () => name});
            Object.defineProperty(p, 'description', {get: () => desc});
            Object.defineProperty(p, 'filename',    {get: () => filename});
            Object.defineProperty(p, 'length',      {get: () => 1});
            return p;
        };
        const arr = [
            makePlugin('Chrome PDF Plugin',   'Portable Document Format', 'internal-pdf-viewer'),
            makePlugin('Chrome PDF Viewer',   '', 'mhjfbmdgcfjbbpaeojofohoefgiehjai'),
            makePlugin('Native Client',       '', 'internal-nacl-plugin'),
        ];
        Object.defineProperty(arr, 'item',    {value: (i) => arr[i]});
        Object.defineProperty(arr, 'namedItem', {value: (n) => arr.find(p => p.name === n) || null});
        return arr;
    }
});

// 5. Add chrome object (missing in headless)
if (!window.chrome) {
    window.chrome = {
        app: {isInstalled: false, InstallState: {DISABLED:'d',INSTALLED:'i',NOT_INSTALLED:'n'}, RunningState: {CANNOT_RUN:'c',READY_TO_RUN:'r',RUNNING:'r'}},
        runtime: {OnInstalledReason: {}, OnRestartRequiredReason: {}, PlatformArch: {}, PlatformNaclArch: {}, PlatformOs: {}, RequestUpdateCheckStatus: {}},
        loadTimes: function() {},
        csi: function() {},
    };
}

// 6. Permissions API — headless returns 'denied' for notifications; real browser prompts
const _origQuery = window.navigator.permissions && window.navigator.permissions.query.bind(window.navigator.permissions);
if (_origQuery) {
    window.navigator.permissions.query = (params) =>
        params.name === 'notifications'
            ? Promise.resolve({state: Notification.permission})
            : _origQuery(params);
}

// 7. Canvas fingerprint noise — tiny per-session jitter so fingerprint isn't identical across runs
(function() {
    const _toDataURL = HTMLCanvasElement.prototype.toDataURL;
    HTMLCanvasElement.prototype.toDataURL = function(type) {
        const ctx = this.getContext('2d');
        if (ctx) {
            const shift = {r: Math.floor(Math.random()*3)-1, g: Math.floor(Math.random()*3)-1, b: Math.floor(Math.random()*3)-1};
            const imgData = ctx.getImageData(0, 0, this.width || 1, this.height || 1);
            for (let i = 0; i < imgData.data.length; i += 4) {
                imgData.data[i]   = Math.max(0, Math.min(255, imgData.data[i]   + shift.r));
                imgData.data[i+1] = Math.max(0, Math.min(255, imgData.data[i+1] + shift.g));
                imgData.data[i+2] = Math.max(0, Math.min(255, imgData.data[i+2] + shift.b));
            }
            ctx.putImageData(imgData, 0, 0);
        }
        return _toDataURL.apply(this, arguments);
    };
})();

// 8. WebGL vendor/renderer — headless exposes 'Google SwiftShader' which is flagged
(function() {
    const _getParam = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function(param) {
        if (param === 37445) return 'Intel Inc.';
        if (param === 37446) return 'Intel Iris OpenGL Engine';
        return _getParam.apply(this, arguments);
    };
    const _getParam2 = WebGL2RenderingContext.prototype.getParameter;
    WebGL2RenderingContext.prototype.getParameter = function(param) {
        if (param === 37445) return 'Intel Inc.';
        if (param === 37446) return 'Intel Iris OpenGL Engine';
        return _getParam2.apply(this, arguments);
    };
})();
"""


def _load_tiktok_cookies() -> list[dict]:
    """
    Load saved TikTok cookies from data/tiktok_cookies.json.
    Returns empty list if file absent — scraper still runs, just without session trust.
    Cookies are only READ here — never written by the browser.
    """
    cookie_path = os.path.join(os.path.dirname(__file__), "../data/tiktok_cookies.json")
    cookie_path = os.path.normpath(cookie_path)
    if not os.path.exists(cookie_path):
        return []
    try:
        with open(cookie_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        # Accept both a bare list and {"cookies": [...]} wrapper
        cookies = raw if isinstance(raw, list) else raw.get("cookies", [])
        # Filter to only tiktok.com domain cookies — never inject cross-domain cookies
        safe = [
            c for c in cookies
            if isinstance(c, dict)
            and "tiktok.com" in c.get("domain", "")
            and c.get("name") and c.get("value")
        ]
        print(f"[PaidAdLib] Loaded {len(safe)} TikTok cookies from file.", flush=True)
        return safe
    except Exception as e:
        print(f"[PaidAdLib] Cookie load error (non-fatal): {e}", flush=True)
        return []


async def _scrape_tiktok_patchright(brand: str, country: str) -> list[dict]:
    """
    Scrape TikTok Ad Library using patchright (CDP-patched Playwright fork).

    Stealth stack applied:
      - patchright patches __playwright__binding__ / __pwInitScripts CDP artifacts
      - Full JS fingerprint spoofing via _TIKTOK_STEALTH_JS init script
      - Optional real session cookies injected from data/tiktok_cookies.json
      - Human-like random delays between interactions
      - Network isolation: only library.tiktok.com and its CDN allowed
    """
    try:
        from patchright.async_api import async_playwright as patchright_playwright
    except ImportError:
        print("[PaidAdLib] patchright not installed — TikTok scrape skipped.", flush=True)
        return []

    safe_brand = _sanitise_brand(brand)
    cookies    = _load_tiktok_cookies()

    # TikTok Ad Library allowed domains (isolated from Meta/Google context)
    tiktok_allowed = {
        "library.tiktok.com",
        "lf16-ttcdn-tos.pstatp.com",
        "lf19-ttcdn-tos.pstatp.com",
        "sf16-scmcdn-sg.ibytedtos.com",
        "p16-ad-sg.tiktokcdn.com",
        "mon-va.tiktok.com",          # TikTok monitoring (needed for ad renders)
        "log-va.tiktok.com",          # TikTok logging endpoint
    }

    async def _tiktok_route(route, request):
        host = urlparse(request.url).netloc
        allowed = any(host == h or host.endswith("." + h) for h in tiktok_allowed)
        if allowed:
            await route.continue_()
        else:
            await route.abort("blockedbyclient")

    ads: list[dict] = []

    # US and GB have the most complete TikTok Ad Libraries
    regions_to_try = ["US", "GB", "AU", "SG"]
    if country and len(country) == 2 and country.upper() not in regions_to_try:
        regions_to_try.insert(0, country.upper())

    async with patchright_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-infobars",
            ],
        )

        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            timezone_id="America/New_York",
            java_script_enabled=True,
            accept_downloads=False,
            extra_http_headers={
                "Accept-Language":          "en-US,en;q=0.9",
                "Accept":                   "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Sec-Ch-Ua":                '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
                "Sec-Ch-Ua-Mobile":         "?0",
                "Sec-Ch-Ua-Platform":       '"macOS"',
                "Sec-Fetch-Dest":           "document",
                "Sec-Fetch-Mode":           "navigate",
                "Sec-Fetch-Site":           "none",
                "Sec-Fetch-User":           "?1",
                "Upgrade-Insecure-Requests":"1",
            },
        )

        # Inject stealth scripts before any page navigation
        await context.add_init_script(_TIKTOK_STEALTH_JS)

        # Inject real session cookies if available
        if cookies:
            try:
                await context.add_cookies(cookies)
            except Exception as e:
                print(f"[PaidAdLib] Cookie injection error (non-fatal): {e}", flush=True)

        # Network isolation — TikTok domains only
        await context.route("**/*", _tiktok_route)

        try:
            page = await context.new_page()

            # Warm up: visit TikTok homepage briefly to establish session context
            # before hitting the Ad Library — reduces cold-session scoring
            try:
                await _rate_limit("library.tiktok.com")
                await page.goto(
                    "https://library.tiktok.com/",
                    timeout=15_000,
                    wait_until="domcontentloaded",
                )
                # Human-like pause
                await asyncio.sleep(random.uniform(1.5, 2.5))
            except Exception:
                pass  # warmup failure is non-fatal

            for region in regions_to_try:
                url = (
                    f"https://library.tiktok.com/ads"
                    f"?region={region}"
                    f"&keyword={quote_plus(safe_brand)}"
                )
                try:
                    await _rate_limit("library.tiktok.com")
                    await page.goto(url, timeout=20_000, wait_until="domcontentloaded")
                    # Human-like render wait with jitter
                    await asyncio.sleep(random.uniform(3.5, 5.0))

                    # Check for bot challenge page
                    body_text = await page.inner_text("body")
                    if any(s in body_text for s in ("verify", "captcha", "robot", "challenge")):
                        print(f"[PaidAdLib] TikTok bot challenge detected (region={region})", flush=True)
                        continue

                    # Wait for results or no-data indicator
                    try:
                        await page.wait_for_selector(
                            ".ad_card_wrapper, .nodata_container, .total_ads",
                            timeout=12_000,
                        )
                    except Exception:
                        continue

                    # Check result count
                    total_el   = await page.query_selector(".total_ads")
                    total_text = (await _text(total_el)) if total_el else ""
                    # total_ads text format: "Total ads:\n42" — skip region if 0
                    if re.search(r":\s*\n?\s*0\b", total_text):
                        continue

                    cards = await page.query_selector_all(".ad_card_wrapper")
                    for card in cards[:10]:
                        raw_text = await _text(card)
                        if not raw_text:
                            continue
                        ads.append({
                            "url":         url,
                            "title":       f"{safe_brand} — TikTok Ad Library ({region})",
                            "content":     raw_text[:1500],
                            "source_type": "paid",
                        })

                    if ads:
                        print(f"[PaidAdLib] TikTok: {len(ads)} ads found (region={region})", flush=True)
                        break

                except Exception as e:
                    print(f"[PaidAdLib] TikTok region={region} error: {e}", flush=True)
                    continue

            await page.close()

        except Exception as e:
            print(f"[PaidAdLib] TikTok patchright error for '{brand}': {e}", flush=True)
        finally:
            await context.close()
            await browser.close()

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

async def _scrape_meta_google(brand: str, country: str, platforms: list[str]) -> dict:
    """
    Scrapes Meta Ad Library and/or Google Ads Transparency using standard playwright.
    Returns {meta: [...], google: [...]} keyed by label.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return {}

    results = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            java_script_enabled=True,
            accept_downloads=False,
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            timezone_id="America/New_York",
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        await context.route("**/*", _block_non_target)

        scrape_tasks = []
        task_labels  = []

        if any(pl in platforms for pl in ("Facebook", "Instagram")):
            scrape_tasks.append(_scrape_meta(context, brand, country))
            task_labels.append("meta")

        if "YouTube" in platforms:
            scrape_tasks.append(_scrape_google(context, brand, country))
            task_labels.append("google")

        try:
            raw = await asyncio.wait_for(
                asyncio.gather(*scrape_tasks, return_exceptions=True),
                timeout=60.0,
            )
        except asyncio.TimeoutError:
            raw = [[] for _ in scrape_tasks]
        finally:
            await context.close()
            await browser.close()

        for label, res in zip(task_labels, raw):
            results[label] = res if isinstance(res, list) else []

    return results


async def _scrape_all(brand: str, country: str, platforms: list[str]) -> dict:
    """
    Orchestrates all three platform scrapers.
    Meta + Google share a playwright context.
    TikTok runs in a separate patchright context with full stealth stack.
    Both run concurrently.
    """
    platform_map = {
        "meta":   ["Facebook", "Instagram"],
        "tiktok": ["TikTok"],
        "google": ["YouTube"],
    }

    need_meta_google = any(pl in platforms for pl in ("Facebook", "Instagram", "YouTube"))
    need_tiktok      = "TikTok" in platforms

    tasks  = []
    labels = []

    if need_meta_google:
        tasks.append(_scrape_meta_google(brand, country, platforms))
        labels.append("meta_google")

    if need_tiktok:
        tasks.append(_scrape_tiktok_patchright(brand, country))
        labels.append("tiktok")

    try:
        raw_results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=90.0,
        )
    except asyncio.TimeoutError:
        raw_results = [{} if l == "meta_google" else [] for l in labels]

    # Flatten into platform_data list
    platform_data: list[dict] = []

    for label, result in zip(labels, raw_results):
        if label == "meta_google":
            mg = result if isinstance(result, dict) else {}
            for sub_label in ("meta", "google"):
                ads = mg.get(sub_label, [])
                if not ads:
                    continue
                for plat_name in platform_map[sub_label]:
                    if plat_name in platforms:
                        platform_data.append({
                            "platform":    plat_name,
                            "raw_results": ads,
                        })
        elif label == "tiktok":
            ads = result if isinstance(result, list) else []
            if ads:
                platform_data.append({
                    "platform":    "TikTok",
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
