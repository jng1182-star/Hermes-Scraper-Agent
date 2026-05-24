"""
Agent 1 — Profile Baseline Scraper.

Scrapes public brand profile pages on Instagram, Facebook, TikTok, and YouTube
within the user-specified date scope (date_from / date_to). Produces a per-brand
organic baseline: average likes, comments, views, and ER per post.

Why this matters:
  The 3× ER outlier detection in approval_gate.py is calibrated against category
  benchmarks by default. With this module, it can also compare against the brand's
  own observed organic floor — a stronger signal for detecting paid amplification
  because it accounts for brand-specific audience quality and posting cadence.

Platform access strategy:
  Instagram  — anti-detect browser (rate-limited without pre-warmed profile)
  Facebook   — headless Playwright (public Pages accessible without auth)
  TikTok     — headless Playwright (public profiles accessible without auth)
  YouTube    — headless Playwright (most scraper-friendly, stable selectors)

Date scoping (two-phase):
  Phase 1: Scroll the profile grid/feed collecting post URLs up to an estimated
           ceiling (posts_per_day × date_range_days × 1.5 buffer).
  Phase 2: Visit each post URL, read the publish date, discard posts outside the
           scope window. Async parallel fetching (up to 4 concurrent pages).

Security:
  - Input brand handles sanitised (alphanumeric + _ . - only)
  - All scraped text passed through _sanitise() to strip scripts/HTML
  - Per-domain rate limiting (2–3s jitter between requests)
  - No credentials or cookies written to disk (ephemeral contexts)
  - Anti-detect profiles managed via AntidetectClient — credentials in .env only
"""

import asyncio
import json
import logging
import os
import random
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote_plus

logger = logging.getLogger(__name__)

_SELECTORS_PATH = Path(__file__).parent / "selectors.json"
_SELECTORS: dict = json.loads(_SELECTORS_PATH.read_text()) if _SELECTORS_PATH.exists() else {}

_HANDLE_SAFE    = re.compile(r"[^a-zA-Z0-9_.\-]")
_DOMAIN_LAST_HIT: dict[str, float] = {}
_MAX_CONCURRENT_POSTS = 4     # parallel post-page visits within a profile
_SCROLL_PAUSE_MIN     = 1.8
_SCROLL_PAUSE_MAX     = 3.2
_PAGE_TIMEOUT_MS      = 20_000


# ── Security helpers ──────────────────────────────────────────────────────────

def _sanitise_handle(handle: str) -> str:
    handle = handle.lstrip("@").strip()
    return _HANDLE_SAFE.sub("", handle)[:80]


_DANGEROUS = re.compile(
    r"(eval\(|Function\(|javascript:|data:text/html|<script)", re.IGNORECASE
)

def _sanitise(text: str, max_len: int = 2000) -> str:
    if not text:
        return ""
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if _DANGEROUS.search(text):
        return ""
    return text[:max_len]


async def _rate_limit(domain: str) -> None:
    last    = _DOMAIN_LAST_HIT.get(domain, 0.0)
    elapsed = time.monotonic() - last
    delay   = random.uniform(_SCROLL_PAUSE_MIN, _SCROLL_PAUSE_MAX)
    if elapsed < delay:
        await asyncio.sleep(delay - elapsed)
    _DOMAIN_LAST_HIT[domain] = time.monotonic()


# ── Date helpers ──────────────────────────────────────────────────────────────

def _resolve_relative_date(text: str, scan_dt: datetime) -> Optional[datetime]:
    """Convert relative date strings ('3 days ago', 'yesterday') to absolute datetime."""
    text = (text or "").lower().strip()
    patterns = [
        (r"(\d+)\s+second",  lambda n: scan_dt - timedelta(seconds=n)),
        (r"(\d+)\s+minute",  lambda n: scan_dt - timedelta(minutes=n)),
        (r"(\d+)\s+hour",    lambda n: scan_dt - timedelta(hours=n)),
        (r"(\d+)\s+day",     lambda n: scan_dt - timedelta(days=n)),
        (r"(\d+)\s+week",    lambda n: scan_dt - timedelta(weeks=n)),
        (r"(\d+)\s+month",   lambda n: scan_dt - timedelta(days=n * 30)),
        (r"(\d+)\s+year",    lambda n: scan_dt - timedelta(days=n * 365)),
        (r"an?\s+hour",      lambda _: scan_dt - timedelta(hours=1)),
        (r"a\s+day",         lambda _: scan_dt - timedelta(days=1)),
        (r"a\s+week",        lambda _: scan_dt - timedelta(weeks=1)),
        (r"a\s+month",       lambda _: scan_dt - timedelta(days=30)),
        (r"yesterday",       lambda _: scan_dt - timedelta(days=1)),
        (r"just now",        lambda _: scan_dt),
    ]
    for pattern, resolver in patterns:
        m = re.search(pattern, text)
        if m:
            n = int(m.group(1)) if m.lastindex else 0
            return resolver(n)
    # Try absolute parse (e.g. "May 15, 2025", "2025-05-15")
    for fmt in ("%B %d, %Y", "%b %d, %Y", "%Y-%m-%d", "%d %B %Y", "%d %b %Y"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _in_scope(post_dt: Optional[datetime], date_from: Optional[str],
              date_to: Optional[str]) -> bool:
    if post_dt is None:
        return True  # unknown date — include rather than silently drop
    if date_from:
        try:
            df = datetime.fromisoformat(date_from).replace(tzinfo=timezone.utc)
            if post_dt < df:
                return False
        except ValueError:
            pass
    if date_to:
        try:
            dt = datetime.fromisoformat(date_to).replace(tzinfo=timezone.utc)
            if post_dt > dt:
                return False
        except ValueError:
            pass
    return True


def _parse_count(text: str) -> int:
    """Parse '2.3M', '45K', '1,234' → integer."""
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


# ── Safe element helpers ──────────────────────────────────────────────────────

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

async def _scrape_instagram_profile(context, brand: str, handle: str,
                                     date_from: str, date_to: str,
                                     scan_dt: datetime) -> dict:
    """Scrape Instagram public profile — requires pre-warmed anti-detect context."""
    safe_handle = _sanitise_handle(handle or brand)
    url  = f"https://www.instagram.com/{safe_handle}/"
    sels = _SELECTORS.get("instagram", {})
    profile_sels = sels.get("profile", {})
    post_sels    = sels.get("post_page", {})

    page = await context.new_page()
    posts_data: list[dict] = []
    collection_note = "instagram_profile_scrape"

    try:
        await _rate_limit("www.instagram.com")
        await page.goto(url, timeout=_PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
        await asyncio.sleep(random.uniform(2.5, 4.0))

        # Read follower count
        follower_count = 0
        try:
            fc_el = await page.query_selector(profile_sels.get("follower_count", ""))
            if fc_el:
                follower_count = _parse_count(await _text(fc_el))
        except Exception:
            pass

        # Collect post URLs from grid — scroll until we have enough
        post_urls: list[str] = []
        prev_count = -1
        scroll_attempts = 0
        max_scrolls = 30  # safety cap

        while len(post_urls) < 200 and scroll_attempts < max_scrolls:
            anchors = await page.query_selector_all(
                profile_sels.get("post_grid", "article a[href*='/p/']")
            )
            hrefs = []
            for a in anchors:
                href = await _attr(a, "href")
                if href and "/p/" in href:
                    hrefs.append(href if href.startswith("http") else f"https://www.instagram.com{href}")
            post_urls = list(dict.fromkeys(post_urls + hrefs))  # dedup, preserve order

            if len(post_urls) == prev_count:
                break  # no new posts — end of profile grid
            prev_count = len(post_urls)
            await page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
            await asyncio.sleep(random.uniform(1.5, 2.5))
            scroll_attempts += 1

        await page.close()

        # Phase 2: visit each post page to get date + metrics
        sem = asyncio.Semaphore(_MAX_CONCURRENT_POSTS)

        async def _fetch_post(post_url: str) -> Optional[dict]:
            async with sem:
                p = await context.new_page()
                try:
                    await _rate_limit("www.instagram.com")
                    await p.goto(post_url, timeout=_PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
                    await asyncio.sleep(random.uniform(1.5, 2.5))

                    # Publish date
                    date_el = await p.query_selector(post_sels.get("publish_date", "time[datetime]"))
                    raw_date = await _attr(date_el, "datetime") if date_el else ""
                    post_dt  = _resolve_relative_date(raw_date, scan_dt)
                    if not _in_scope(post_dt, date_from, date_to):
                        return None

                    # Metrics
                    likes = comments = views = 0
                    try:
                        lk_el = await p.query_selector(post_sels.get("like_count", ""))
                        if lk_el:
                            likes = _parse_count(await _text(lk_el))
                    except Exception:
                        pass
                    try:
                        cm_el = await p.query_selector(post_sels.get("comment_count", ""))
                        if cm_el:
                            comments = _parse_count(await _text(cm_el))
                    except Exception:
                        pass
                    try:
                        vw_el = await p.query_selector(post_sels.get("view_count", ""))
                        if vw_el:
                            views = _parse_count(await _text(vw_el))
                    except Exception:
                        pass

                    caption = ""
                    try:
                        cp_el = await p.query_selector(post_sels.get("caption", ""))
                        if cp_el:
                            caption = await _text(cp_el)
                    except Exception:
                        pass

                    return {
                        "url":          post_url,
                        "publish_date": post_dt.isoformat() if post_dt else None,
                        "likes":        likes,
                        "comments":     comments,
                        "views":        views,
                        "caption":      caption[:300],
                    }
                except Exception as exc:
                    logger.debug("[ProfileScraper] Instagram post fetch error %s: %s", post_url, exc)
                    return None
                finally:
                    await p.close()

        tasks = [_fetch_post(u) for u in post_urls[:100]]  # hard cap at 100 posts
        results = await asyncio.gather(*tasks, return_exceptions=True)
        posts_data = [r for r in results if isinstance(r, dict)]

    except Exception as exc:
        logger.error("[ProfileScraper] Instagram profile error for '%s': %s", brand, exc)

    return _build_baseline(brand, "Instagram", handle, follower_count,
                           posts_data, collection_note, date_from, date_to)


async def _scrape_facebook_profile(context, brand: str, handle: str,
                                    date_from: str, date_to: str,
                                    scan_dt: datetime) -> dict:
    """Scrape Facebook public Page — headless Playwright, no auth required."""
    safe_handle = _sanitise_handle(handle or brand)
    url  = f"https://www.facebook.com/{safe_handle}/"
    sels = _SELECTORS.get("facebook", {})
    profile_sels = sels.get("profile", {})
    post_sels    = sels.get("post_page", {})

    page = await context.new_page()
    posts_data: list[dict] = []
    collection_note = "facebook_page_scrape"
    follower_count = 0

    try:
        await _rate_limit("www.facebook.com")
        await page.goto(url, timeout=_PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
        await asyncio.sleep(random.uniform(2.5, 4.0))

        # Follower count
        try:
            fc_el = await page.query_selector(profile_sels.get("follower_count", ""))
            if fc_el:
                follower_count = _parse_count(await _text(fc_el))
        except Exception:
            pass

        # Collect post links from timeline
        post_urls: list[str] = []
        prev_count = -1
        scroll_attempts = 0

        while len(post_urls) < 150 and scroll_attempts < 25:
            for link_sel in (
                profile_sels.get("post_link", ""),
                "a[href*='/posts/']", "a[href*='/videos/']", "a[href*='/photos/']"
            ):
                if not link_sel:
                    continue
                anchors = await page.query_selector_all(link_sel)
                for a in anchors:
                    href = await _attr(a, "href")
                    if href and ("facebook.com" in href or href.startswith("/")):
                        full = href if href.startswith("http") else f"https://www.facebook.com{href}"
                        if any(k in full for k in ("/posts/", "/videos/", "/photos/")):
                            post_urls.append(full)
            post_urls = list(dict.fromkeys(post_urls))

            if len(post_urls) == prev_count:
                break
            prev_count = len(post_urls)
            await page.evaluate("window.scrollBy(0, window.innerHeight * 3)")
            await asyncio.sleep(random.uniform(1.8, 3.0))
            scroll_attempts += 1

        await page.close()

        # Phase 2: visit each post
        sem = asyncio.Semaphore(_MAX_CONCURRENT_POSTS)

        async def _fetch_fb_post(post_url: str) -> Optional[dict]:
            async with sem:
                p = await context.new_page()
                try:
                    await _rate_limit("www.facebook.com")
                    await p.goto(post_url, timeout=_PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
                    await asyncio.sleep(random.uniform(1.5, 2.5))

                    # Date — try datetime attribute first, then abbr, then relative text
                    post_dt = None
                    for date_sel in (
                        post_sels.get("publish_date", ""),
                        "abbr[data-utime]", "a[role='link'] span span"
                    ):
                        if not date_sel:
                            continue
                        try:
                            el = await p.query_selector(date_sel)
                            if el:
                                raw = await _attr(el, "data-utime") or await _attr(el, "datetime") or await _text(el)
                                if raw and raw.isdigit():
                                    post_dt = datetime.fromtimestamp(int(raw), tz=timezone.utc)
                                else:
                                    post_dt = _resolve_relative_date(raw, scan_dt)
                                if post_dt:
                                    break
                        except Exception:
                            continue

                    if not _in_scope(post_dt, date_from, date_to):
                        return None

                    likes = comments = shares = views = 0
                    for metric_sel, attr_name in (
                        (post_sels.get("like_count", ""), "likes"),
                        (post_sels.get("comment_count", ""), "comments"),
                        (post_sels.get("share_count", ""), "shares"),
                        (post_sels.get("view_count", ""), "views"),
                    ):
                        if not metric_sel:
                            continue
                        try:
                            el = await p.query_selector(metric_sel)
                            if el:
                                val = _parse_count(await _text(el))
                                if attr_name == "likes":    likes    = val
                                elif attr_name == "comments": comments = val
                                elif attr_name == "shares":   shares   = val
                                elif attr_name == "views":    views    = val
                        except Exception:
                            pass

                    caption = ""
                    try:
                        cp_el = await p.query_selector(post_sels.get("caption", ""))
                        if cp_el:
                            caption = await _text(cp_el)
                    except Exception:
                        pass

                    return {
                        "url":          post_url,
                        "publish_date": post_dt.isoformat() if post_dt else None,
                        "likes":        likes,
                        "comments":     comments,
                        "shares":       shares,
                        "views":        views,
                        "caption":      caption[:300],
                    }
                except Exception as exc:
                    logger.debug("[ProfileScraper] Facebook post fetch error %s: %s", post_url, exc)
                    return None
                finally:
                    await p.close()

        tasks = [_fetch_fb_post(u) for u in post_urls[:100]]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        posts_data = [r for r in results if isinstance(r, dict)]

    except Exception as exc:
        logger.error("[ProfileScraper] Facebook profile error for '%s': %s", brand, exc)

    return _build_baseline(brand, "Facebook", handle, follower_count,
                           posts_data, collection_note, date_from, date_to)


async def _scrape_tiktok_profile(context, brand: str, handle: str,
                                  date_from: str, date_to: str,
                                  scan_dt: datetime) -> dict:
    """Scrape TikTok public profile — headless Playwright (public endpoint, no auth)."""
    safe_handle = _sanitise_handle(handle or brand)
    url  = f"https://www.tiktok.com/@{safe_handle}"
    sels = _SELECTORS.get("tiktok", {})
    profile_sels = sels.get("profile", {})
    post_sels    = sels.get("post_page", {})

    page = await context.new_page()
    posts_data: list[dict] = []
    collection_note = "tiktok_profile_scrape"
    follower_count = 0

    try:
        await _rate_limit("www.tiktok.com")
        await page.goto(url, timeout=_PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
        await asyncio.sleep(random.uniform(3.0, 5.0))

        try:
            fc_el = await page.query_selector(profile_sels.get("follower_count", "strong[data-e2e='followers-count']"))
            if fc_el:
                follower_count = _parse_count(await _text(fc_el))
        except Exception:
            pass

        # Collect video URLs from profile grid
        post_urls: list[str] = []
        prev_count = -1
        scroll_attempts = 0

        while len(post_urls) < 200 and scroll_attempts < 30:
            anchors = await page.query_selector_all(
                profile_sels.get("post_grid", "div[class*='DivItemContainerV2'] a[href*='/video/']")
            )
            hrefs = []
            for a in anchors:
                href = await _attr(a, "href")
                if href and "/video/" in href:
                    hrefs.append(href if href.startswith("http") else f"https://www.tiktok.com{href}")
            post_urls = list(dict.fromkeys(post_urls + hrefs))

            if len(post_urls) == prev_count:
                break
            prev_count = len(post_urls)
            await page.evaluate("window.scrollBy(0, window.innerHeight * 2)")
            await asyncio.sleep(random.uniform(1.5, 2.5))
            scroll_attempts += 1

        await page.close()

        # Phase 2: visit each video page
        sem = asyncio.Semaphore(_MAX_CONCURRENT_POSTS)

        async def _fetch_tiktok_post(post_url: str) -> Optional[dict]:
            async with sem:
                p = await context.new_page()
                try:
                    await _rate_limit("www.tiktok.com")
                    await p.goto(post_url, timeout=_PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
                    await asyncio.sleep(random.uniform(2.0, 3.5))

                    # TikTok video pages don't show publish date in a clean element.
                    # Extract from the URL-embedded timestamp (video ID contains Unix ms).
                    post_dt = None
                    video_id_match = re.search(r"/video/(\d+)", post_url)
                    if video_id_match:
                        vid_id = int(video_id_match.group(1))
                        # TikTok video IDs are Snowflake-like: top 32 bits = seconds since epoch
                        unix_ts = vid_id >> 32
                        if unix_ts > 1_000_000_000:  # sanity: after year 2001
                            post_dt = datetime.fromtimestamp(unix_ts, tz=timezone.utc)

                    if not _in_scope(post_dt, date_from, date_to):
                        return None

                    likes = comments = shares = views = saves = 0
                    metric_map = {
                        "like_count":    ("likes",    post_sels.get("like_count",    "strong[data-e2e='like-count']")),
                        "comment_count": ("comments", post_sels.get("comment_count", "strong[data-e2e='comment-count']")),
                        "share_count":   ("shares",   post_sels.get("share_count",   "strong[data-e2e='share-count']")),
                        "view_count":    ("views",    post_sels.get("view_count",    "strong[data-e2e='video-views']")),
                    }
                    for _, (field, sel) in metric_map.items():
                        try:
                            el = await p.query_selector(sel)
                            if el:
                                val = _parse_count(await _text(el))
                                if field == "likes":    likes    = val
                                elif field == "comments": comments = val
                                elif field == "shares":   shares   = val
                                elif field == "views":    views    = val
                        except Exception:
                            pass

                    caption = ""
                    try:
                        cp_el = await p.query_selector(post_sels.get("caption", "span[data-e2e='browse-video-desc']"))
                        if cp_el:
                            caption = await _text(cp_el)
                    except Exception:
                        pass

                    return {
                        "url":          post_url,
                        "publish_date": post_dt.isoformat() if post_dt else None,
                        "likes":        likes,
                        "comments":     comments,
                        "shares":       shares,
                        "views":        views,
                        "saves":        saves,
                        "caption":      caption[:300],
                    }
                except Exception as exc:
                    logger.debug("[ProfileScraper] TikTok post fetch error %s: %s", post_url, exc)
                    return None
                finally:
                    await p.close()

        tasks = [_fetch_tiktok_post(u) for u in post_urls[:100]]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        posts_data = [r for r in results if isinstance(r, dict)]

    except Exception as exc:
        logger.error("[ProfileScraper] TikTok profile error for '%s': %s", brand, exc)

    return _build_baseline(brand, "TikTok", handle, follower_count,
                           posts_data, collection_note, date_from, date_to)


async def _scrape_youtube_channel(context, brand: str, handle: str,
                                   date_from: str, date_to: str,
                                   scan_dt: datetime) -> dict:
    """Scrape YouTube channel Videos tab — headless Playwright (most stable surface)."""
    safe_handle = _sanitise_handle(handle or brand)
    url  = f"https://www.youtube.com/@{safe_handle}/videos"
    sels = _SELECTORS.get("youtube", {})
    channel_sels = sels.get("channel", {})
    video_sels   = sels.get("video_page", {})

    page = await context.new_page()
    posts_data: list[dict] = []
    collection_note = "youtube_channel_scrape"
    subscriber_count = 0

    try:
        await _rate_limit("www.youtube.com")
        await page.goto(url, timeout=_PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
        await asyncio.sleep(random.uniform(2.5, 4.0))

        try:
            sub_el = await page.query_selector(channel_sels.get("subscriber_count", "yt-formatted-string#subscriber-count"))
            if sub_el:
                subscriber_count = _parse_count(await _text(sub_el))
        except Exception:
            pass

        # YouTube channel page renders absolute dates — much easier than TikTok/IG
        video_urls: list[str] = []
        prev_count = -1
        scroll_attempts = 0

        while len(video_urls) < 200 and scroll_attempts < 30:
            anchors = await page.query_selector_all(
                channel_sels.get("video_link", "a#video-title-link, a#thumbnail")
            )
            hrefs = []
            for a in anchors:
                href = await _attr(a, "href")
                if href and "/watch?v=" in href:
                    hrefs.append(href if href.startswith("http") else f"https://www.youtube.com{href}")
            video_urls = list(dict.fromkeys(video_urls + hrefs))

            if len(video_urls) == prev_count:
                break
            prev_count = len(video_urls)
            await page.evaluate("window.scrollBy(0, window.innerHeight * 3)")
            await asyncio.sleep(random.uniform(1.5, 2.5))
            scroll_attempts += 1

        # YouTube renders dates on the channel grid — read them before closing
        # to avoid per-video page visits where possible
        grid_items = await page.query_selector_all(
            channel_sels.get("video_grid", "ytd-rich-item-renderer, ytd-grid-video-renderer")
        )
        grid_meta: dict[str, dict] = {}
        for item in grid_items:
            try:
                link_el = await item.query_selector("a#video-title-link, a#thumbnail")
                href    = await _attr(link_el, "href") if link_el else ""
                if not href:
                    continue
                full_url = href if href.startswith("http") else f"https://www.youtube.com{href}"

                date_el  = await item.query_selector(channel_sels.get("publish_date", "span#metadata-line span:nth-child(2)"))
                views_el = await item.query_selector(channel_sels.get("view_count",   "span#metadata-line span:first-child"))

                raw_date = await _text(date_el) if date_el else ""
                raw_views= await _text(views_el) if views_el else ""

                post_dt  = _resolve_relative_date(raw_date, scan_dt)
                views    = _parse_count(raw_views.replace("views", "").strip())

                grid_meta[full_url] = {"publish_date": post_dt, "views": views}
            except Exception:
                continue

        await page.close()

        # Only visit individual video pages for ones missing date from grid
        sem = asyncio.Semaphore(_MAX_CONCURRENT_POSTS)

        async def _fetch_yt_video(video_url: str) -> Optional[dict]:
            meta = grid_meta.get(video_url, {})
            post_dt = meta.get("publish_date")
            grid_views = meta.get("views", 0)

            # If grid gave us the date and it's out of scope, skip the page visit
            if post_dt is not None and not _in_scope(post_dt, date_from, date_to):
                return None

            # If we have date + views from grid, skip the page visit
            if post_dt is not None and grid_views > 0:
                return {
                    "url":          video_url,
                    "publish_date": post_dt.isoformat(),
                    "likes":        0,   # not available on grid
                    "comments":     0,
                    "views":        grid_views,
                    "caption":      "",
                    "source":       "channel_grid",
                }

            async with sem:
                p = await context.new_page()
                try:
                    await _rate_limit("www.youtube.com")
                    await p.goto(video_url, timeout=_PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
                    await asyncio.sleep(random.uniform(2.0, 3.5))

                    if post_dt is None:
                        date_el = await p.query_selector(video_sels.get("publish_date", ""))
                        raw_d   = await _text(date_el) if date_el else ""
                        post_dt = _resolve_relative_date(raw_d, scan_dt)

                    if not _in_scope(post_dt, date_from, date_to):
                        return None

                    likes = comments = views = 0
                    for sel, field in (
                        (video_sels.get("like_count", ""),    "likes"),
                        (video_sels.get("view_count", ""),    "views"),
                        (video_sels.get("comment_count", ""), "comments"),
                    ):
                        if not sel:
                            continue
                        try:
                            el = await p.query_selector(sel)
                            if el:
                                val = _parse_count(await _text(el))
                                if field == "likes":    likes    = val or grid_views
                                elif field == "views":    views    = val or grid_views
                                elif field == "comments": comments = val
                        except Exception:
                            pass

                    return {
                        "url":          video_url,
                        "publish_date": post_dt.isoformat() if post_dt else None,
                        "likes":        likes,
                        "comments":     comments,
                        "views":        views or grid_views,
                        "caption":      "",
                        "source":       "video_page",
                    }
                except Exception as exc:
                    logger.debug("[ProfileScraper] YouTube video fetch error %s: %s", video_url, exc)
                    return None
                finally:
                    await p.close()

        tasks = [_fetch_yt_video(u) for u in video_urls[:100]]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        posts_data = [r for r in results if isinstance(r, dict)]

    except Exception as exc:
        logger.error("[ProfileScraper] YouTube channel error for '%s': %s", brand, exc)

    return _build_baseline(brand, "YouTube", handle, subscriber_count,
                           posts_data, collection_note, date_from, date_to)


# ── Baseline builder ──────────────────────────────────────────────────────────

def _build_baseline(brand: str, platform: str, handle: str, follower_count: int,
                    posts: list[dict], collection_note: str,
                    date_from: str, date_to: str) -> dict:
    """
    Aggregate per-post data into the organic baseline profile used by the analyst
    agent and the approval gate's outlier detection.
    """
    n = len(posts)
    if n == 0:
        return {
            "brand": brand, "platform": platform, "handle": handle,
            "follower_count": follower_count, "posts_in_scope": 0,
            "date_from": date_from, "date_to": date_to,
            "avg_likes": 0, "avg_comments": 0, "avg_views": 0, "avg_shares": 0,
            "avg_er_pct": 0.0, "baseline_available": False,
            "collection_method": collection_note,
            "posts": [],
        }

    avg_likes    = round(sum(p.get("likes", 0)    for p in posts) / n)
    avg_comments = round(sum(p.get("comments", 0) for p in posts) / n)
    avg_views    = round(sum(p.get("views", 0)    for p in posts) / n)
    avg_shares   = round(sum(p.get("shares", 0)   for p in posts) / n)

    # ER calculation: view-based for TikTok/YouTube, follower-based for IG/FB
    view_based = platform.lower() in ("tiktok", "youtube")
    if view_based and avg_views > 0:
        er_denominator = avg_views
    elif follower_count > 0:
        er_denominator = follower_count
    else:
        er_denominator = max(avg_views, 1)

    avg_interactions = avg_likes + avg_comments + avg_shares
    avg_er_pct = round((avg_interactions / er_denominator) * 100, 3)

    return {
        "brand":               brand,
        "platform":            platform,
        "handle":              handle,
        "follower_count":      follower_count,
        "posts_in_scope":      n,
        "date_from":           date_from,
        "date_to":             date_to,
        "avg_likes":           avg_likes,
        "avg_comments":        avg_comments,
        "avg_views":           avg_views,
        "avg_shares":          avg_shares,
        "avg_er_pct":          avg_er_pct,
        "er_denominator":      "views" if view_based else "followers",
        "baseline_available":  True,
        "collection_method":   collection_note,
        "data_source":         f"First-party DOM scrape — {platform} public profile page",
        "posts":               posts[:20],  # include up to 20 sample posts in output
    }


# ── Main orchestrator ─────────────────────────────────────────────────────────

async def _run_profile_scrape(brands: list[dict], platforms: list[str],
                               date_from: str, date_to: str,
                               country: str, scan_dt: datetime) -> list[dict]:
    """
    Orchestrate profile scraping across all brands × platforms.
    Instagram uses anti-detect browser if configured; others use headless Playwright.
    """
    from playwright.async_api import async_playwright
    from tools.proxy_manager import get_proxy
    from tools.antidetect_client import AntidetectClient

    antidetect = AntidetectClient()
    proxy_cfg  = get_proxy(country)
    results: list[dict] = []

    async with async_playwright() as p:
        # Standard headless context (Facebook, TikTok, YouTube)
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

        # Anti-detect context for Instagram (CDP connect if provider configured)
        ctx_instagram = None
        ig_ws_url = antidetect.start_profile(country) if antidetect.available else None
        if ig_ws_url:
            try:
                browser_ad = await p.chromium.connect_over_cdp(ig_ws_url)
                ctx_instagram = browser_ad.contexts[0] if browser_ad.contexts else await browser_ad.new_context()
                logger.info("[ProfileScraper] Anti-detect browser connected for Instagram.")
            except Exception as exc:
                logger.warning("[ProfileScraper] Anti-detect connect failed (%s) — falling back to headless for Instagram.", exc)
                ctx_instagram = ctx_headless
        else:
            ctx_instagram = ctx_headless
            if "Instagram" in platforms:
                logger.warning(
                    "[ProfileScraper] No anti-detect browser configured — scraping Instagram "
                    "with standard headless Playwright. Expect rate-limiting after ~10 profiles."
                )

        try:
            scrape_tasks = []
            for brand_info in brands:
                brand   = brand_info.get("name", "")
                handles = brand_info.get("handles", {})

                for platform in platforms:
                    handle = handles.get(platform, brand)
                    if platform == "Instagram":
                        scrape_tasks.append(
                            _scrape_instagram_profile(ctx_instagram, brand, handle, date_from, date_to, scan_dt)
                        )
                    elif platform == "Facebook":
                        scrape_tasks.append(
                            _scrape_facebook_profile(ctx_headless, brand, handle, date_from, date_to, scan_dt)
                        )
                    elif platform == "TikTok":
                        scrape_tasks.append(
                            _scrape_tiktok_profile(ctx_headless, brand, handle, date_from, date_to, scan_dt)
                        )
                    elif platform == "YouTube":
                        scrape_tasks.append(
                            _scrape_youtube_channel(ctx_headless, brand, handle, date_from, date_to, scan_dt)
                        )

            try:
                raw = await asyncio.wait_for(
                    asyncio.gather(*scrape_tasks, return_exceptions=True),
                    timeout=300.0,
                )
            except asyncio.TimeoutError:
                logger.warning("[ProfileScraper] Overall timeout — returning partial results.")
                raw = []

            results = [r for r in raw if isinstance(r, dict)]

        finally:
            await ctx_headless.close()
            await browser_headless.close()
            antidetect.stop_all()

    return results


# ── CrewAI BaseTool ───────────────────────────────────────────────────────────

from crewai.tools import BaseTool

class ProfileScraperTool(BaseTool):
    name: str = "Profile Baseline Scraper"
    description: str = (
        "Scrapes public brand profile pages on Instagram, Facebook, TikTok, and YouTube "
        "within the specified date scope. Returns per-brand organic baseline metrics: "
        "average likes, comments, views, engagement rate, and follower count per post. "
        "This baseline is used to calibrate paid signal detection. "
        "Input: JSON with brands (list of {name, handles{platform: handle}}), "
        "platforms, date_from, date_to, country."
    )

    def _run(self, query: str) -> str:
        params: dict = {}
        try:
            bracket = query.find("{")
            if bracket != -1:
                params = json.loads(query[bracket:])
        except Exception:
            pass

        brands     = params.get("brands", [])
        platforms  = params.get("platforms", ["Instagram", "Facebook", "TikTok", "YouTube"])
        date_from  = params.get("date_from", "")
        date_to    = params.get("date_to", "")
        country    = params.get("country", "")
        scan_dt    = datetime.now(timezone.utc)

        # Normalise: if brands is a list of strings, convert to expected dict format
        if brands and isinstance(brands[0], str):
            brands = [{"name": b, "handles": {p: b for p in platforms}} for b in brands]

        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                results = loop.run_until_complete(
                    _run_profile_scrape(brands, platforms, date_from, date_to, country, scan_dt)
                )
            finally:
                loop.close()
        except Exception as exc:
            logger.error("[ProfileScraper] _run error: %s", exc)
            results = []

        return json.dumps({
            "scan_date_utc": scan_dt.isoformat(),
            "date_from":     date_from,
            "date_to":       date_to,
            "country":       country,
            "baselines":     results,
            "total_brands":  len(results),
        })
