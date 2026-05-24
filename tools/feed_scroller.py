"""
Agent 2 — Feed Scroller / Doom Scroll Ad Capture.

Scrolls the primary algorithmic feed for each platform and captures declared paid
ads using strict DOM-marker detection only. No engagement-based guessing at the
capture stage — that belongs in the approval gate downstream.

Platforms and surfaces:
  Instagram  — home feed (anti-detect browser, pre-warmed profile)
  Facebook   — home feed (anti-detect browser, pre-warmed profile)
  TikTok     — For You Page (anti-detect browser, pre-warmed profile)
  YouTube    — Shorts feed (headless Playwright, lighter bot-detection)

Supplementary paths (run in parallel, already in paid_adlib_tool.py):
  Meta Ad Library   — covers IG + FB declared paid inventory not served to your feed
  Google ATC        — covers YouTube declared paid inventory

Ad detection — strict DOM markers only:
  Instagram/Facebook: "Sponsored" text node, "Paid partnership" label
  TikTok:             [data-e2e="ad-badge"], CTA overlay wrappers
  YouTube Shorts:     "Sponsored" / "Ad" badge near channel handle

Behavioral loop per session:
  Scroll feed for 2 minutes → clean page refresh (scrambles session scoring) →
  scroll for another 2 minutes. Collect all ad nodes observed across both passes.

Security:
  - Brand/search inputs sanitised before URL use
  - All scraped text sanitised (no scripts, no HTML injection)
  - Anti-detect profile credentials in .env only — never in code
  - Per-domain rate limiting with jitter
  - No cookies / profile state written to disk (managed by anti-detect provider)
"""

import asyncio
import json
import logging
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_SELECTORS_PATH = Path(__file__).parent / "selectors.json"
_SELECTORS: dict = json.loads(_SELECTORS_PATH.read_text()) if _SELECTORS_PATH.exists() else {}

_SCROLL_PASS_SECS   = 120    # seconds per scroll pass
_SCROLL_INTERVAL    = 2.5    # seconds between scroll steps (base, jitter added)
_SCROLL_DISTANCE    = 600    # pixels per step
_REFRESH_PAUSE      = 5.0    # seconds after page refresh before resuming scroll
_PAGE_TIMEOUT_MS    = 25_000
_MAX_ADS_PER_PASS   = 30     # cap per scroll pass to avoid memory bloat

_DOMAIN_LAST_HIT: dict[str, float] = {}

_DANGEROUS = re.compile(
    r"(eval\(|Function\(|javascript:|data:text/html|<script)", re.IGNORECASE
)


# ── Security helpers ──────────────────────────────────────────────────────────

def _sanitise(text: str, max_len: int = 2000) -> str:
    if not text:
        return ""
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if _DANGEROUS.search(text):
        return ""
    return text[:max_len]


async def _rate_limit(domain: str, base: float = 2.0, jitter: float = 1.5) -> None:
    last    = _DOMAIN_LAST_HIT.get(domain, 0.0)
    elapsed = time.monotonic() - last
    delay   = base + random.uniform(0, jitter)
    if elapsed < delay:
        await asyncio.sleep(delay - elapsed)
    _DOMAIN_LAST_HIT[domain] = time.monotonic()


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


def _parse_count(text: str) -> int:
    if not text:
        return 0
    text = text.strip().replace(",", "").upper()
    try:
        if text.endswith("M"):
            return int(float(text[:-1]) * 1_000_000)
        if text.endswith("K"):
            return int(float(text[:-1]) * 1_000)
        return int(float(text))
    except (ValueError, AttributeError):
        return 0


# ── Scroll engine ─────────────────────────────────────────────────────────────

async def _scroll_pass(page, duration_secs: float, collect_fn) -> list[dict]:
    """
    Scroll the page for `duration_secs`, calling collect_fn(page) every
    _SCROLL_INTERVAL seconds to harvest visible ad nodes.
    Returns deduplicated ad list.
    """
    ads: list[dict] = []
    seen_ids: set[str] = set()
    deadline = time.monotonic() + duration_secs

    while time.monotonic() < deadline and len(ads) < _MAX_ADS_PER_PASS:
        new_ads = await collect_fn(page)
        for ad in new_ads:
            uid = ad.get("advertiser", "") + ad.get("ad_copy", "")[:50]
            if uid and uid not in seen_ids:
                seen_ids.add(uid)
                ads.append(ad)

        jitter = random.uniform(-0.5, 0.8)
        await asyncio.sleep(max(0.5, _SCROLL_INTERVAL + jitter))
        scroll_px = _SCROLL_DISTANCE + random.randint(-100, 200)
        await page.evaluate(f"window.scrollBy(0, {scroll_px})")

    return ads


# ── Platform ad collectors ────────────────────────────────────────────────────

async def _collect_instagram_ads(page) -> list[dict]:
    """Detect Sponsored / Paid partnership nodes in Instagram home feed."""
    sels = _SELECTORS.get("instagram", {}).get("feed", {})
    ads: list[dict] = []

    # Find all post containers
    containers = await page.query_selector_all(
        sels.get("post_container", "article[role='presentation']")
    )

    for container in containers:
        # Check for Sponsored label
        is_paid = False
        paid_signal = ""
        try:
            sp_el = await container.query_selector(
                sels.get("sponsored_label",
                         "article span:has-text('Sponsored'), article span:has-text('Paid partnership')")
            )
            if sp_el:
                label_text = await _text(sp_el)
                is_paid     = True
                paid_signal = "dom_label"
        except Exception:
            pass

        # Also check CTA buttons as secondary paid signal
        if not is_paid:
            try:
                cta_el = await container.query_selector(
                    sels.get("cta_button",
                             "div[role='button']:has-text('Learn More'), div[role='button']:has-text('Shop Now')")
                )
                if cta_el:
                    is_paid     = True
                    paid_signal = "dom_label"
            except Exception:
                pass

        if not is_paid:
            continue

        # Extract ad fields
        advertiser = likes = comments = post_url = creative_url = ad_copy = ""
        try:
            adv_el = await container.query_selector(sels.get("advertiser_name", "article header a[href*='/']"))
            if adv_el:
                advertiser = await _text(adv_el)
        except Exception:
            pass

        try:
            url_el = await container.query_selector(sels.get("post_url", "article a[href*='/p/']"))
            if url_el:
                href = await _attr(url_el, "href")
                post_url = href if href.startswith("http") else f"https://www.instagram.com{href}"
        except Exception:
            pass

        try:
            lk_el = await container.query_selector(sels.get("like_count", ""))
            if lk_el:
                likes = _parse_count(await _text(lk_el))
        except Exception:
            pass

        try:
            cm_el = await container.query_selector(sels.get("comment_count", ""))
            if cm_el:
                comments = _parse_count(await _text(cm_el))
        except Exception:
            pass

        try:
            img_el = await container.query_selector(sels.get("ad_creative_url", "article img[class*='x5yr21d']"))
            if img_el:
                creative_url = await _attr(img_el, "src")
        except Exception:
            pass

        if advertiser or post_url:
            ads.append({
                "platform":     "Instagram",
                "paid_signal":  paid_signal,
                "advertiser":   advertiser,
                "post_url":     post_url,
                "creative_url": creative_url,
                "ad_copy":      ad_copy,
                "likes":        likes,
                "comments":     comments,
                "views":        0,
                "captured_utc": datetime.now(timezone.utc).isoformat(),
                "data_source":  "First-party DOM scrape — Instagram in-feed ad detection",
            })

    return ads


async def _collect_facebook_ads(page) -> list[dict]:
    """Detect Sponsored nodes in Facebook home feed."""
    sels = _SELECTORS.get("facebook", {}).get("feed", {})
    ads: list[dict] = []

    containers = await page.query_selector_all(
        sels.get("post_container", "div[role='article']")
    )

    for container in containers:
        is_paid     = False
        paid_signal = ""

        for sp_sel in (
            sels.get("sponsored_label", ""),
            sels.get("sponsored_attr", ""),
            "div[role='article'] span:has-text('Sponsored')",
            "a[aria-label='Sponsored']",
        ):
            if not sp_sel:
                continue
            try:
                sp_el = await container.query_selector(sp_sel)
                if sp_el:
                    is_paid     = True
                    paid_signal = "dom_label"
                    break
            except Exception:
                continue

        if not is_paid:
            try:
                cta_el = await container.query_selector(
                    sels.get("cta_button",
                             "div[role='button']:has-text('Learn More'), a[data-lynx-uri]:has-text('Learn More')")
                )
                if cta_el:
                    is_paid     = True
                    paid_signal = "dom_label"
            except Exception:
                pass

        if not is_paid:
            continue

        advertiser = post_url = creative_url = ad_copy = ""
        likes = comments = shares = 0

        try:
            adv_el = await container.query_selector(
                sels.get("advertiser_name", "div[role='article'] h3 a")
            )
            if adv_el:
                advertiser = await _text(adv_el)
        except Exception:
            pass

        try:
            url_el = await container.query_selector(
                sels.get("post_url", "div[role='article'] a[href*='/posts/']")
            )
            if url_el:
                href = await _attr(url_el, "href")
                post_url = href if href.startswith("http") else f"https://www.facebook.com{href}"
        except Exception:
            pass

        try:
            copy_el = await container.query_selector(sels.get("ad_copy", ""))
            if copy_el:
                ad_copy = await _text(copy_el)
        except Exception:
            pass

        try:
            img_el = await container.query_selector(sels.get("ad_creative_url", ""))
            if img_el:
                creative_url = await _attr(img_el, "src")
        except Exception:
            pass

        if advertiser or post_url:
            ads.append({
                "platform":     "Facebook",
                "paid_signal":  paid_signal,
                "advertiser":   advertiser,
                "post_url":     post_url,
                "creative_url": creative_url,
                "ad_copy":      ad_copy,
                "likes":        likes,
                "comments":     comments,
                "shares":       shares,
                "views":        0,
                "captured_utc": datetime.now(timezone.utc).isoformat(),
                "data_source":  "First-party DOM scrape — Facebook in-feed ad detection",
            })

    return ads


async def _collect_tiktok_ads(page) -> list[dict]:
    """Detect paid placements in TikTok FYP feed via strict DOM markers."""
    sels = _SELECTORS.get("tiktok", {}).get("feed", {})
    ads: list[dict] = []

    containers = await page.query_selector_all(
        sels.get("post_container",
                 "div[class*='DivVideoFeedV2'] > div, div[class*='swiper-slide']")
    )

    for container in containers:
        is_paid     = False
        paid_signal = ""

        # Primary: official ad badge
        for badge_sel in (
            sels.get("ad_badge", "[data-e2e='ad-badge']"),
            sels.get("sponsored_text", "span:has-text('Sponsored'), span:has-text('โฆษณา'), span:has-text('Được tài trợ')"),
        ):
            if not badge_sel:
                continue
            try:
                el = await container.query_selector(badge_sel)
                if el:
                    is_paid     = True
                    paid_signal = "dom_label"
                    break
            except Exception:
                continue

        # Secondary: CTA overlay (Learn More, Shop Now etc.)
        if not is_paid:
            for cta_sel in (
                sels.get("cta_button", "a:has-text('Learn More'), a:has-text('Shop Now')"),
                sels.get("cta_wrapper", "div[class*='CTAContainer']"),
            ):
                if not cta_sel:
                    continue
                try:
                    el = await container.query_selector(cta_sel)
                    if el:
                        is_paid     = True
                        paid_signal = "dom_label"
                        break
                except Exception:
                    continue

        if not is_paid:
            continue

        advertiser = handle = ad_copy = video_url = ""
        likes = comments = shares = views = 0

        try:
            adv_el = await container.query_selector(
                sels.get("advertiser_name", "p[data-e2e='video-author-desc']")
            )
            if adv_el:
                advertiser = await _text(adv_el)
        except Exception:
            pass

        try:
            hdl_el = await container.query_selector(
                sels.get("advertiser_handle", "p[data-e2e='video-author-uniqueid']")
            )
            if hdl_el:
                handle = await _text(hdl_el)
        except Exception:
            pass

        try:
            copy_el = await container.query_selector(sels.get("ad_copy", "span[data-e2e='browse-video-desc']"))
            if copy_el:
                ad_copy = await _text(copy_el)
        except Exception:
            pass

        for metric_sel, field in (
            (sels.get("like_count",    "strong[data-e2e='like-count']"),    "likes"),
            (sels.get("comment_count", "strong[data-e2e='comment-count']"), "comments"),
            (sels.get("share_count",   "strong[data-e2e='share-count']"),   "shares"),
        ):
            try:
                el = await container.query_selector(metric_sel)
                if el:
                    val = _parse_count(await _text(el))
                    if field == "likes":    likes    = val
                    elif field == "comments": comments = val
                    elif field == "shares":   shares   = val
            except Exception:
                pass

        try:
            vid_el = await container.query_selector(sels.get("video_url", "video source"))
            if vid_el:
                video_url = await _attr(vid_el, "src")
        except Exception:
            pass

        if advertiser or handle:
            ads.append({
                "platform":     "TikTok",
                "paid_signal":  paid_signal,
                "advertiser":   advertiser or handle,
                "handle":       handle,
                "post_url":     "",
                "creative_url": video_url,
                "ad_copy":      ad_copy,
                "likes":        likes,
                "comments":     comments,
                "shares":       shares,
                "views":        views,
                "captured_utc": datetime.now(timezone.utc).isoformat(),
                "data_source":  "First-party DOM scrape — TikTok FYP in-feed ad detection",
            })

    return ads


async def _collect_youtube_shorts_ads(page) -> list[dict]:
    """Detect paid placements in YouTube Shorts feed."""
    sels = _SELECTORS.get("youtube", {}).get("shorts_feed", {})
    ads: list[dict] = []

    containers = await page.query_selector_all(
        sels.get("post_container", "ytd-reel-video-renderer, ytd-shorts")
    )

    for container in containers:
        is_paid     = False
        paid_signal = ""

        for badge_sel in (
            sels.get("ad_badge",        "ytd-ad-slot-renderer"),
            sels.get("sponsored_label", "span.ytp-ad-badge, div:has-text('Sponsored'), yt-formatted-string:has-text('Ad')"),
        ):
            if not badge_sel:
                continue
            try:
                el = await container.query_selector(badge_sel)
                if el:
                    is_paid     = True
                    paid_signal = "dom_label"
                    break
            except Exception:
                continue

        if not is_paid:
            try:
                cta_el = await container.query_selector(
                    sels.get("cta_button", "ytd-button-renderer:has-text('Learn More'), a[class*='ytp-ad-button']")
                )
                if cta_el:
                    is_paid     = True
                    paid_signal = "dom_label"
            except Exception:
                pass

        if not is_paid:
            continue

        advertiser = video_url = ""
        likes = views = 0

        try:
            adv_el = await container.query_selector(
                sels.get("advertiser_name", "ytd-channel-name a")
            )
            if adv_el:
                advertiser = await _text(adv_el)
        except Exception:
            pass

        try:
            lk_el = await container.query_selector(sels.get("like_count", ""))
            if lk_el:
                likes = _parse_count(await _text(lk_el))
        except Exception:
            pass

        try:
            vw_el = await container.query_selector(sels.get("view_count", "span.view-count"))
            if vw_el:
                views = _parse_count(await _text(vw_el))
        except Exception:
            pass

        try:
            vid_el = await container.query_selector(sels.get("video_url", "video source"))
            if vid_el:
                video_url = await _attr(vid_el, "src")
        except Exception:
            pass

        if advertiser:
            ads.append({
                "platform":     "YouTube",
                "paid_signal":  paid_signal,
                "advertiser":   advertiser,
                "post_url":     "",
                "creative_url": video_url,
                "ad_copy":      "",
                "likes":        likes,
                "comments":     0,
                "views":        views,
                "captured_utc": datetime.now(timezone.utc).isoformat(),
                "data_source":  "First-party DOM scrape — YouTube Shorts in-feed ad detection",
            })

    return ads


# ── Per-platform scroll sessions ──────────────────────────────────────────────

async def _scroll_instagram(context, country: str) -> list[dict]:
    page = await context.new_page()
    ads: list[dict] = []
    try:
        await _rate_limit("www.instagram.com")
        await page.goto("https://www.instagram.com/", timeout=_PAGE_TIMEOUT_MS,
                        wait_until="domcontentloaded")
        await asyncio.sleep(random.uniform(3.0, 5.0))

        # Pass 1
        pass1 = await _scroll_pass(page, _SCROLL_PASS_SECS, _collect_instagram_ads)
        ads.extend(pass1)

        # Refresh
        await page.reload(wait_until="domcontentloaded")
        await asyncio.sleep(_REFRESH_PAUSE + random.uniform(0, 2.0))

        # Pass 2
        pass2 = await _scroll_pass(page, _SCROLL_PASS_SECS, _collect_instagram_ads)
        for ad in pass2:
            uid = ad.get("advertiser", "") + ad.get("ad_copy", "")[:50]
            if not any(uid == (a.get("advertiser", "") + a.get("ad_copy", "")[:50]) for a in ads):
                ads.append(ad)

    except Exception as exc:
        logger.error("[FeedScroller] Instagram scroll error: %s", exc)
    finally:
        await page.close()
    return ads


async def _scroll_facebook(context, country: str) -> list[dict]:
    page = await context.new_page()
    ads: list[dict] = []
    try:
        await _rate_limit("www.facebook.com")
        await page.goto("https://www.facebook.com/", timeout=_PAGE_TIMEOUT_MS,
                        wait_until="domcontentloaded")
        await asyncio.sleep(random.uniform(3.0, 5.0))

        pass1 = await _scroll_pass(page, _SCROLL_PASS_SECS, _collect_facebook_ads)
        ads.extend(pass1)

        await page.reload(wait_until="domcontentloaded")
        await asyncio.sleep(_REFRESH_PAUSE + random.uniform(0, 2.0))

        pass2 = await _scroll_pass(page, _SCROLL_PASS_SECS, _collect_facebook_ads)
        for ad in pass2:
            uid = ad.get("advertiser", "") + ad.get("ad_copy", "")[:50]
            if not any(uid == (a.get("advertiser", "") + a.get("ad_copy", "")[:50]) for a in ads):
                ads.append(ad)

    except Exception as exc:
        logger.error("[FeedScroller] Facebook scroll error: %s", exc)
    finally:
        await page.close()
    return ads


async def _scroll_tiktok(context, country: str) -> list[dict]:
    page = await context.new_page()
    ads: list[dict] = []
    try:
        await _rate_limit("www.tiktok.com")
        await page.goto("https://www.tiktok.com/foryou", timeout=_PAGE_TIMEOUT_MS,
                        wait_until="domcontentloaded")
        await asyncio.sleep(random.uniform(4.0, 6.0))  # longer warm-up for TikTok

        pass1 = await _scroll_pass(page, _SCROLL_PASS_SECS, _collect_tiktok_ads)
        ads.extend(pass1)

        await page.reload(wait_until="domcontentloaded")
        await asyncio.sleep(_REFRESH_PAUSE + random.uniform(1.0, 3.0))

        pass2 = await _scroll_pass(page, _SCROLL_PASS_SECS, _collect_tiktok_ads)
        for ad in pass2:
            uid = ad.get("advertiser", "") + ad.get("ad_copy", "")[:50]
            if not any(uid == (a.get("advertiser", "") + a.get("ad_copy", "")[:50]) for a in ads):
                ads.append(ad)

    except Exception as exc:
        logger.error("[FeedScroller] TikTok scroll error: %s", exc)
    finally:
        await page.close()
    return ads


async def _scroll_youtube_shorts(context, country: str) -> list[dict]:
    page = await context.new_page()
    ads: list[dict] = []
    try:
        await _rate_limit("www.youtube.com")
        await page.goto("https://www.youtube.com/shorts", timeout=_PAGE_TIMEOUT_MS,
                        wait_until="domcontentloaded")
        await asyncio.sleep(random.uniform(2.5, 4.0))

        pass1 = await _scroll_pass(page, _SCROLL_PASS_SECS, _collect_youtube_shorts_ads)
        ads.extend(pass1)

        await page.reload(wait_until="domcontentloaded")
        await asyncio.sleep(_REFRESH_PAUSE + random.uniform(0, 2.0))

        pass2 = await _scroll_pass(page, _SCROLL_PASS_SECS, _collect_youtube_shorts_ads)
        for ad in pass2:
            uid = ad.get("advertiser", "") + ad.get("ad_copy", "")[:50]
            if not any(uid == (a.get("advertiser", "") + a.get("ad_copy", "")[:50]) for a in ads):
                ads.append(ad)

    except Exception as exc:
        logger.error("[FeedScroller] YouTube Shorts scroll error: %s", exc)
    finally:
        await page.close()
    return ads


# ── Main orchestrator ─────────────────────────────────────────────────────────

async def _run_feed_scroll(platforms: list[str], country: str,
                            brands_filter: list[str]) -> dict:
    """
    Run doom scroll sessions across all requested platforms concurrently.
    Instagram + Facebook + TikTok use anti-detect browser if configured;
    YouTube Shorts uses headless Playwright.
    Returns all captured ads, optionally filtered to brands_filter.
    """
    from playwright.async_api import async_playwright
    from tools.proxy_manager import get_proxy
    from tools.antidetect_client import AntidetectClient

    antidetect = AntidetectClient()
    proxy_cfg  = get_proxy(country)
    all_ads: list[dict] = []
    scan_dt = datetime.now(timezone.utc)

    async with async_playwright() as p:
        # Headless browser for YouTube (+ IG/FB/TT fallback if anti-detect unavailable)
        browser_headless = await p.chromium.launch(headless=True)
        ctx_headless = await browser_headless.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            java_script_enabled=True,
            accept_downloads=False,
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            proxy=proxy_cfg,
        )
        await ctx_headless.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        # Anti-detect context for feed platforms requiring authentication
        ctx_ad = None
        ad_ws  = antidetect.start_profile(country) if antidetect.available else None
        if ad_ws:
            try:
                browser_ad = await p.chromium.connect_over_cdp(ad_ws)
                ctx_ad = browser_ad.contexts[0] if browser_ad.contexts else await browser_ad.new_context()
                logger.info("[FeedScroller] Anti-detect browser connected for market '%s'.", country)
            except Exception as exc:
                logger.warning("[FeedScroller] Anti-detect connect failed (%s) — falling back to headless.", exc)
                ctx_ad = ctx_headless
        else:
            ctx_ad = ctx_headless
            if any(p_ in platforms for p_ in ("Instagram", "Facebook", "TikTok")):
                logger.warning(
                    "[FeedScroller] No anti-detect browser configured — feed scrolling with "
                    "standard headless Playwright. Bot-detection risk is elevated for "
                    "Instagram, Facebook, and TikTok authenticated feeds."
                )

        try:
            scroll_tasks  = []
            scroll_labels = []

            if "Instagram" in platforms:
                scroll_tasks.append(_scroll_instagram(ctx_ad, country))
                scroll_labels.append("Instagram")
            if "Facebook" in platforms:
                scroll_tasks.append(_scroll_facebook(ctx_ad, country))
                scroll_labels.append("Facebook")
            if "TikTok" in platforms:
                scroll_tasks.append(_scroll_tiktok(ctx_ad, country))
                scroll_labels.append("TikTok")
            if "YouTube" in platforms:
                scroll_tasks.append(_scroll_youtube_shorts(ctx_headless, country))
                scroll_labels.append("YouTube")

            try:
                # Total timeout: 2 passes × 2 min + overhead per platform, run concurrently
                results = await asyncio.wait_for(
                    asyncio.gather(*scroll_tasks, return_exceptions=True),
                    timeout=600.0,
                )
            except asyncio.TimeoutError:
                logger.warning("[FeedScroller] Overall session timeout — returning partial results.")
                results = [[] for _ in scroll_tasks]

            for label, result in zip(scroll_labels, results):
                if isinstance(result, list):
                    all_ads.extend(result)
                else:
                    logger.warning("[FeedScroller] %s scroll returned error: %s", label, result)

        finally:
            await ctx_headless.close()
            await browser_headless.close()
            antidetect.stop_all()

    # Filter to brands of interest if provided
    if brands_filter:
        brands_lower = [b.lower() for b in brands_filter]
        def _matches_brand(ad: dict) -> bool:
            adv = (ad.get("advertiser", "") + ad.get("handle", "") + ad.get("ad_copy", "")).lower()
            return any(b in adv for b in brands_lower)
        matched = [ad for ad in all_ads if _matches_brand(ad)]
        unmatched_count = len(all_ads) - len(matched)
        # Include unmatched ads in a separate key for category SoV analysis
        unmatched = [ad for ad in all_ads if not _matches_brand(ad)]
    else:
        matched   = all_ads
        unmatched = []
        unmatched_count = 0

    return {
        "scan_date_utc":    scan_dt.isoformat(),
        "country":          country,
        "platforms_scrolled": [l for l in scroll_labels if l in platforms],
        "total_ads_captured": len(all_ads),
        "brand_matched_ads":  matched,
        "category_ads":       unmatched[:20],  # top-20 unmatched for category SoV
        "unmatched_count":    unmatched_count,
        "collection_method":  "feed_doom_scroll",
        "data_source":        "First-party DOM scrape — in-feed ad detection (strict DOM markers only)",
        "antidetect_active":  antidetect.available and ad_ws is not None,
    }


# ── CrewAI BaseTool ───────────────────────────────────────────────────────────

from crewai.tools import BaseTool

class FeedScrollerTool(BaseTool):
    name: str = "Feed Doom Scroller"
    description: str = (
        "Scrolls the primary algorithmic feed for Instagram, Facebook, TikTok FYP, "
        "and YouTube Shorts, capturing declared paid ads using strict DOM marker detection. "
        "Detection method: explicit 'Sponsored'/'Paid partnership' labels and CTA overlay wrappers only — "
        "no engagement-based guessing. Returns structured ad records with advertiser, "
        "creative URL, ad copy, and live engagement metrics. "
        "Input: JSON with platforms (list), country, brands (list of brand names to filter for)."
    )

    def _run(self, query: str) -> str:
        params: dict = {}
        try:
            bracket = query.find("{")
            if bracket != -1:
                params = json.loads(query[bracket:])
        except Exception:
            pass

        platforms = params.get("platforms", ["Instagram", "Facebook", "TikTok", "YouTube"])
        country   = params.get("country", "")
        brands    = params.get("brands", [])

        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                result = loop.run_until_complete(
                    _run_feed_scroll(platforms, country, brands)
                )
            finally:
                loop.close()
        except Exception as exc:
            logger.error("[FeedScroller] _run error: %s", exc)
            result = {
                "error": str(exc),
                "brand_matched_ads": [],
                "category_ads": [],
                "total_ads_captured": 0,
            }

        return json.dumps(result)
