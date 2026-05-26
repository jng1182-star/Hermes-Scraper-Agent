"""
Agent 2 — Feed Scroller (Content + Ad Capture).

Scrolls the primary algorithmic feed for each platform and collects two things:

1. All organic posts visible in the feed — url, caption, author/handle, metrics,
   publish_date. Coverage is bounded by the IP/geo of the scraper and what the
   algorithm chooses to serve; this is a known constraint and the reason the
   profile scraper (Agent 1) exists to complement it with geo-unconstrained coverage.

2. Declared paid ads — detected via strict DOM markers only (no engagement guessing).
   Ad detection method per platform:
     Instagram/Facebook: "Sponsored" text node, "Paid partnership" label
     TikTok:             [data-e2e="ad-badge"], CTA overlay wrappers
     YouTube Shorts:     "Sponsored" / "Ad" badge near channel handle

Platforms and surfaces:
  Instagram  — home feed (anti-detect browser, pre-warmed profile)
  Facebook   — home feed (anti-detect browser, pre-warmed profile)
  TikTok     — For You Page (anti-detect browser, pre-warmed profile)
  YouTube    — Shorts feed (headless Playwright, lighter bot-detection)

Supplementary paths (run in parallel, already in paid_adlib_tool.py):
  Meta Ad Library   — covers IG + FB declared paid inventory not served to your feed
  Google ATC        — covers YouTube declared paid inventory

Behavioral loop per session:
  Scroll feed for 2 minutes → clean page refresh (scrambles session scoring) →
  scroll for another 2 minutes. Collect all posts and ads observed across both passes.

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

logger = logging.getLogger(__name__)

_SELECTORS_PATH = Path(__file__).parent / "selectors.json"
if not _SELECTORS_PATH.exists():
    raise FileNotFoundError(
        f"[FeedScroller] selectors.json not found at {_SELECTORS_PATH}. "
        "Scraper cannot run — feed DOM selectors would collapse to empty strings. "
        "Restore selectors.json before starting the pipeline."
    )
_SELECTORS: dict = json.loads(_SELECTORS_PATH.read_text())

_SCROLL_PASS_SECS    = 120    # seconds per scroll pass
_SCROLL_INTERVAL     = 2.5    # seconds between scroll steps (base, jitter added)
_SCROLL_DISTANCE     = 600    # pixels per step
_REFRESH_PAUSE       = 5.0    # seconds after page refresh before resuming scroll
_PAGE_TIMEOUT_MS     = 25_000
_MAX_ADS_PER_PASS    = 30     # cap per scroll pass to avoid memory bloat
_MAX_POSTS_PER_PASS  = 100    # cap organic posts per scroll pass

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

async def _scroll_pass(page, duration_secs: float, collect_fn) -> dict:
    """
    Scroll the page for `duration_secs`, calling collect_fn(page) every
    _SCROLL_INTERVAL seconds to harvest visible post and ad nodes.
    Returns {"ads": [...], "posts": [...]} — both deduplicated.
    collect_fn must return {"ads": [...], "posts": [...]}.
    """
    ads: list[dict]   = []
    posts: list[dict] = []
    seen_ad_ids: set[str]   = set()
    seen_post_ids: set[str] = set()
    deadline = time.monotonic() + duration_secs

    while time.monotonic() < deadline:
        # Exit early once both caps are hit — no point holding the deadline
        if len(ads) >= _MAX_ADS_PER_PASS and len(posts) >= _MAX_POSTS_PER_PASS:
            break

        batch = await collect_fn(page)

        for ad in batch.get("ads", []):
            if len(ads) >= _MAX_ADS_PER_PASS:
                break
            uid = ad.get("advertiser", "") + ad.get("ad_copy", "")[:50]
            if uid and uid not in seen_ad_ids:
                seen_ad_ids.add(uid)
                ads.append(ad)

        for post in batch.get("posts", []):
            if len(posts) >= _MAX_POSTS_PER_PASS:
                break
            uid = post.get("post_url", "") or (post.get("author", "") + post.get("caption", "")[:50])
            if uid and uid not in seen_post_ids:
                seen_post_ids.add(uid)
                posts.append(post)

        jitter = random.uniform(-0.5, 0.8)
        await asyncio.sleep(max(0.5, _SCROLL_INTERVAL + jitter))
        scroll_px = _SCROLL_DISTANCE + random.randint(-100, 200)
        await page.evaluate(f"window.scrollBy(0, {scroll_px})")

    return {"ads": ads, "posts": posts}


# ── Platform ad collectors ────────────────────────────────────────────────────

async def _collect_instagram_feed(page) -> dict:
    """Collect all visible posts from Instagram home feed; flag paid ones via DOM markers."""
    sels = _SELECTORS.get("instagram", {}).get("feed", {})
    ads: list[dict]   = []
    posts: list[dict] = []
    captured_utc = datetime.now(timezone.utc).isoformat()

    containers = await page.query_selector_all(
        sels.get("post_container", "article[role='presentation']")
    )

    for container in containers:
        # Paid detection
        is_paid     = False
        paid_signal = ""
        try:
            sp_el = await container.query_selector(
                sels.get("sponsored_label",
                         "article span:has-text('Sponsored'), article span:has-text('Paid partnership')")
            )
            if sp_el:
                is_paid     = True
                paid_signal = "dom_label"
        except Exception:
            pass

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

        # Extract common fields for every post (organic or paid)
        author = post_url = creative_url = caption = ""
        likes = comments = 0
        try:
            adv_el = await container.query_selector(sels.get("advertiser_name", "article header a[href*='/']"))
            if adv_el:
                author = await _text(adv_el)
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
        try:
            cp_el = await container.query_selector(sels.get("caption", "article div[data-testid='post-comment-root'] span"))
            if cp_el:
                caption = await _text(cp_el)
        except Exception:
            pass

        if not (author or post_url):
            continue

        record = {
            "platform":      "Instagram",
            "author":        author,
            "post_url":      post_url,
            "creative_url":  creative_url,
            "caption":       caption[:300],
            "likes":         likes,
            "comments":      comments,
            "views":         0,
            "publish_date":  None,  # not available inline — requires post page visit
            "captured_utc":  captured_utc,
            "data_source":   "First-party DOM scrape — Instagram in-feed",
        }

        if is_paid:
            ads.append({**record, "paid_signal": paid_signal, "ad_copy": caption[:300]})
        else:
            posts.append(record)

    return {"ads": ads, "posts": posts}


async def _collect_facebook_feed(page) -> dict:
    """Collect all visible posts from Facebook home feed; flag paid ones via DOM markers."""
    sels = _SELECTORS.get("facebook", {}).get("feed", {})
    ads: list[dict]   = []
    posts: list[dict] = []
    captured_utc = datetime.now(timezone.utc).isoformat()

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

        author = post_url = creative_url = caption = ""
        likes = comments = shares = 0

        try:
            adv_el = await container.query_selector(sels.get("advertiser_name", "div[role='article'] h3 a"))
            if adv_el:
                author = await _text(adv_el)
        except Exception:
            pass
        try:
            url_el = await container.query_selector(sels.get("post_url", "div[role='article'] a[href*='/posts/']"))
            if url_el:
                href = await _attr(url_el, "href")
                post_url = href if href.startswith("http") else f"https://www.facebook.com{href}"
        except Exception:
            pass
        try:
            cp_el = await container.query_selector(sels.get("ad_copy", sels.get("caption", "")))
            if cp_el:
                caption = await _text(cp_el)
        except Exception:
            pass
        try:
            img_el = await container.query_selector(sels.get("ad_creative_url", ""))
            if img_el:
                creative_url = await _attr(img_el, "src")
        except Exception:
            pass

        if not (author or post_url):
            continue

        record = {
            "platform":     "Facebook",
            "author":       author,
            "post_url":     post_url,
            "creative_url": creative_url,
            "caption":      caption[:300],
            "likes":        likes,
            "comments":     comments,
            "shares":       shares,
            "views":        0,
            "publish_date": None,
            "captured_utc": captured_utc,
            "data_source":  "First-party DOM scrape — Facebook in-feed",
        }

        if is_paid:
            ads.append({**record, "paid_signal": paid_signal, "ad_copy": caption[:300]})
        else:
            posts.append(record)

    return {"ads": ads, "posts": posts}


async def _collect_tiktok_feed(page) -> dict:
    """Collect all visible posts from TikTok FYP; flag paid ones via DOM markers."""
    sels = _SELECTORS.get("tiktok", {}).get("feed", {})
    ads: list[dict]   = []
    posts: list[dict] = []
    captured_utc = datetime.now(timezone.utc).isoformat()

    containers = await page.query_selector_all(
        sels.get("post_container",
                 "div[class*='DivVideoFeedV2'] > div, div[class*='swiper-slide']")
    )

    for container in containers:
        is_paid     = False
        paid_signal = ""

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

        author = handle = caption = video_url = ""
        likes = comments = shares = views = 0

        try:
            adv_el = await container.query_selector(
                sels.get("advertiser_name", "p[data-e2e='video-author-desc']")
            )
            if adv_el:
                author = await _text(adv_el)
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
            cp_el = await container.query_selector(sels.get("ad_copy", "span[data-e2e='browse-video-desc']"))
            if cp_el:
                caption = await _text(cp_el)
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

        if not (author or handle):
            continue

        record = {
            "platform":     "TikTok",
            "author":       author or handle,
            "handle":       handle,
            "post_url":     "",
            "creative_url": video_url,
            "caption":      caption[:300],
            "likes":        likes,
            "comments":     comments,
            "shares":       shares,
            "views":        views,
            "publish_date": None,
            "captured_utc": captured_utc,
            "data_source":  "First-party DOM scrape — TikTok FYP in-feed",
        }

        if is_paid:
            ads.append({**record, "paid_signal": paid_signal, "ad_copy": caption[:300]})
        else:
            posts.append(record)

    return {"ads": ads, "posts": posts}


async def _collect_youtube_shorts_feed(page) -> dict:
    """Collect all visible posts from YouTube Shorts feed; flag paid ones via DOM markers."""
    sels = _SELECTORS.get("youtube", {}).get("shorts_feed", {})
    ads: list[dict]   = []
    posts: list[dict] = []
    captured_utc = datetime.now(timezone.utc).isoformat()

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

        author = video_url = caption = ""
        likes = views = 0

        try:
            adv_el = await container.query_selector(sels.get("advertiser_name", "ytd-channel-name a"))
            if adv_el:
                author = await _text(adv_el)
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

        if not author:
            continue

        record = {
            "platform":     "YouTube",
            "author":       author,
            "post_url":     "",
            "creative_url": video_url,
            "caption":      caption[:300],
            "likes":        likes,
            "comments":     0,
            "views":        views,
            "publish_date": None,
            "captured_utc": captured_utc,
            "data_source":  "First-party DOM scrape — YouTube Shorts in-feed",
        }

        if is_paid:
            ads.append({**record, "paid_signal": paid_signal, "ad_copy": ""})
        else:
            posts.append(record)

    return {"ads": ads, "posts": posts}


# ── Per-platform scroll sessions ──────────────────────────────────────────────

def _merge_pass(combined: dict, new_pass: dict) -> None:
    """Merge a second scroll pass into combined, deduplicating by uid."""
    for key in ("ads", "posts"):
        seen = {(r.get("author", "") + r.get("post_url", "") + r.get("caption", "")[:50])
                for r in combined[key]}
        for r in new_pass.get(key, []):
            uid = r.get("author", "") + r.get("post_url", "") + r.get("caption", "")[:50]
            if uid not in seen:
                seen.add(uid)
                combined[key].append(r)


async def _scroll_instagram(context, country: str) -> dict:
    page = await context.new_page()
    combined = {"ads": [], "posts": []}
    try:
        await _rate_limit("www.instagram.com")
        await page.goto("https://www.instagram.com/", timeout=_PAGE_TIMEOUT_MS,
                        wait_until="domcontentloaded")
        await asyncio.sleep(random.uniform(3.0, 5.0))

        pass1 = await _scroll_pass(page, _SCROLL_PASS_SECS, _collect_instagram_feed)
        _merge_pass(combined, pass1)

        await page.reload(wait_until="domcontentloaded")
        await asyncio.sleep(_REFRESH_PAUSE + random.uniform(0, 2.0))

        pass2 = await _scroll_pass(page, _SCROLL_PASS_SECS, _collect_instagram_feed)
        _merge_pass(combined, pass2)

    except Exception as exc:
        logger.error("[FeedScroller] Instagram scroll error: %s", exc)
    finally:
        await page.close()
    return combined


async def _scroll_facebook(context, country: str) -> dict:
    page = await context.new_page()
    combined = {"ads": [], "posts": []}
    try:
        await _rate_limit("www.facebook.com")
        await page.goto("https://www.facebook.com/", timeout=_PAGE_TIMEOUT_MS,
                        wait_until="domcontentloaded")
        await asyncio.sleep(random.uniform(3.0, 5.0))

        pass1 = await _scroll_pass(page, _SCROLL_PASS_SECS, _collect_facebook_feed)
        _merge_pass(combined, pass1)

        await page.reload(wait_until="domcontentloaded")
        await asyncio.sleep(_REFRESH_PAUSE + random.uniform(0, 2.0))

        pass2 = await _scroll_pass(page, _SCROLL_PASS_SECS, _collect_facebook_feed)
        _merge_pass(combined, pass2)

    except Exception as exc:
        logger.error("[FeedScroller] Facebook scroll error: %s", exc)
    finally:
        await page.close()
    return combined


async def _scroll_tiktok(context, country: str) -> dict:  # noqa: ARG001
    page = await context.new_page()
    combined = {"ads": [], "posts": []}
    try:
        await _rate_limit("www.tiktok.com")
        await page.goto("https://www.tiktok.com/foryou", timeout=_PAGE_TIMEOUT_MS,
                        wait_until="domcontentloaded")
        await asyncio.sleep(random.uniform(4.0, 6.0))

        pass1 = await _scroll_pass(page, _SCROLL_PASS_SECS, _collect_tiktok_feed)
        _merge_pass(combined, pass1)

        await page.reload(wait_until="domcontentloaded")
        await asyncio.sleep(_REFRESH_PAUSE + random.uniform(1.0, 3.0))

        pass2 = await _scroll_pass(page, _SCROLL_PASS_SECS, _collect_tiktok_feed)
        _merge_pass(combined, pass2)

    except Exception as exc:
        logger.error("[FeedScroller] TikTok scroll error: %s", exc)
    finally:
        await page.close()
    return combined


async def _scroll_youtube_shorts(context, country: str) -> dict:  # noqa: ARG001
    page = await context.new_page()
    combined = {"ads": [], "posts": []}
    try:
        await _rate_limit("www.youtube.com")
        await page.goto("https://www.youtube.com/shorts", timeout=_PAGE_TIMEOUT_MS,
                        wait_until="domcontentloaded")
        await asyncio.sleep(random.uniform(2.5, 4.0))

        pass1 = await _scroll_pass(page, _SCROLL_PASS_SECS, _collect_youtube_shorts_feed)
        _merge_pass(combined, pass1)

        await page.reload(wait_until="domcontentloaded")
        await asyncio.sleep(_REFRESH_PAUSE + random.uniform(0, 2.0))

        pass2 = await _scroll_pass(page, _SCROLL_PASS_SECS, _collect_youtube_shorts_feed)
        _merge_pass(combined, pass2)

    except Exception as exc:
        logger.error("[FeedScroller] YouTube Shorts scroll error: %s", exc)
    finally:
        await page.close()
    return combined


# ── Main orchestrator ─────────────────────────────────────────────────────────

def _score_posts_against_baselines(
    posts: list[dict], baselines: list[dict], er_threshold_multiplier: float = 3.0
) -> tuple[list[dict], list[dict]]:
    """
    Split feed posts into organic and likely-paid using baselines from the profile scraper.
    A post is flagged likely_paid if its per-post ER exceeds baseline_avg_er × multiplier.
    Posts without a matching baseline are kept as organic (insufficient data to flag).
    Returns (organic_posts, paid_posts).
    """
    def _norm_handle(h: str) -> str:
        return h.lower().lstrip("@")

    # Index baselines by (platform, handle) — strip leading @ so @nike == nike
    baseline_index: dict[tuple[str, str], dict] = {}
    for b in baselines:
        key = (b.get("platform", "").lower(), _norm_handle(b.get("handle", "")))
        baseline_index[key] = b
        # Also index by brand name as fallback
        brand_key = (b.get("platform", "").lower(), _norm_handle(b.get("brand", "")))
        if brand_key not in baseline_index:
            baseline_index[brand_key] = b

    organic: list[dict] = []
    paid:    list[dict] = []

    for post in posts:
        platform = post.get("platform", "").lower()
        author   = _norm_handle(post.get("handle") or post.get("author", ""))

        baseline = baseline_index.get((platform, author))
        if not baseline or not baseline.get("baseline_available"):
            organic.append(post)
            continue

        avg_er   = baseline.get("avg_er_pct", 0.0)
        threshold = avg_er * er_threshold_multiplier
        if threshold <= 0:
            organic.append(post)
            continue

        # Compute this post's ER using the same denominator logic as the profile scraper
        followers = baseline.get("follower_count", 0)
        view_based = platform in ("tiktok", "youtube")
        views = post.get("views", 0)
        denom = views if (view_based and views > 0) else (followers if followers > 0 else max(views, 1))

        interactions = post.get("likes", 0) + post.get("comments", 0) + post.get("shares", 0)
        post_er = round((interactions / denom) * 100, 3)

        if post_er >= threshold:
            paid.append({**post, "paid_signal": "baseline_outlier",
                         "post_er_pct": post_er, "baseline_er_pct": avg_er,
                         "threshold_multiplier": er_threshold_multiplier})
        else:
            organic.append({**post, "post_er_pct": post_er})

    return organic, paid


async def _run_feed_scroll(platforms: list[str], country: str,
                            brands_filter: list[str],
                            baselines: list[dict]) -> dict:
    """
    Run scroll sessions across all requested platforms concurrently.
    Collects all visible posts (organic + paid). DOM-labelled paid posts are flagged
    immediately; remaining posts are scored against profile scraper baselines to surface
    undeclared paid amplification.
    Instagram + Facebook + TikTok use anti-detect browser if configured;
    YouTube Shorts uses headless Playwright.
    """
    from playwright.async_api import async_playwright
    from tools.proxy_manager import get_proxy
    from tools.antidetect_client import AntidetectClient

    antidetect = AntidetectClient()
    proxy_cfg  = get_proxy(country)
    all_dom_ads:    list[dict] = []  # confirmed by DOM labels
    all_feed_posts: list[dict] = []  # all other scrolled posts
    scan_dt = datetime.now(timezone.utc)
    scroll_labels: list[str] = []

    async with async_playwright() as p:
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
            if any(pl in platforms for pl in ("Instagram", "Facebook", "TikTok")):
                logger.warning(
                    "[FeedScroller] No anti-detect browser configured — feed scrolling with "
                    "standard headless Playwright. Bot-detection risk is elevated for "
                    "Instagram, Facebook, and TikTok authenticated feeds."
                )

        try:
            scroll_tasks = []
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
                results = await asyncio.wait_for(
                    asyncio.gather(*scroll_tasks, return_exceptions=True),
                    timeout=600.0,
                )
            except asyncio.TimeoutError:
                logger.warning("[FeedScroller] Overall session timeout — returning partial results.")
                results = [{"ads": [], "posts": []} for _ in scroll_tasks]

            for label, result in zip(scroll_labels, results):
                if isinstance(result, dict):
                    all_dom_ads.extend(result.get("ads", []))
                    all_feed_posts.extend(result.get("posts", []))
                else:
                    logger.warning("[FeedScroller] %s scroll returned error: %s", label, result)

        finally:
            await ctx_headless.close()
            await browser_headless.close()
            antidetect.stop_all()

    # Score organic feed posts against baselines from the profile scraper
    organic_posts, baseline_paid_posts = _score_posts_against_baselines(
        all_feed_posts, baselines
    )

    all_paid = all_dom_ads + baseline_paid_posts

    # Filter to brands of interest if provided
    def _matches_brand(record: dict) -> bool:
        text = (record.get("author", "") + record.get("handle", "") +
                record.get("caption", "") + record.get("ad_copy", "")).lower()
        return any(b.lower() in text for b in brands_filter)

    if brands_filter:
        matched_paid     = [r for r in all_paid         if _matches_brand(r)]
        matched_organic  = [r for r in organic_posts    if _matches_brand(r)]
        category_paid    = [r for r in all_paid         if not _matches_brand(r)]
    else:
        matched_paid     = all_paid
        matched_organic  = organic_posts
        category_paid    = []

    return {
        "scan_date_utc":          scan_dt.isoformat(),
        "country":                country,
        "platforms_scrolled":     scroll_labels,
        "total_posts_scrolled":   len(all_feed_posts),
        "total_dom_ads":          len(all_dom_ads),
        "total_baseline_outliers": len(baseline_paid_posts),
        "brand_paid_posts":       matched_paid,
        "brand_organic_posts":    matched_organic,
        "category_paid_posts":    category_paid[:20],
        "collection_method":      "feed_scroll",
        "data_source":            "First-party DOM scrape — in-feed (geo-bounded by scraper IP/geo)",
        "antidetect_active":      antidetect.available and ad_ws is not None,
        "baselines_applied":      len(baselines) > 0,
    }


# ── CrewAI BaseTool ───────────────────────────────────────────────────────────

from crewai.tools import BaseTool

class FeedScrollerTool(BaseTool):
    name: str = "Feed Scroller"
    description: str = (
        "Scrolls the primary algorithmic feed for Instagram, Facebook, TikTok FYP, "
        "and YouTube Shorts. Collects all visible posts within the scroll session. "
        "Paid detection uses two layers: (1) DOM labels (Sponsored / Paid partnership / ad-badge) "
        "for explicitly declared ads; (2) baseline-threshold scoring — posts whose per-post ER "
        "exceeds the brand's organic baseline ER by 3× are flagged as likely_paid. "
        "Baselines must be provided from the Profile Scraper output. "
        "Coverage is geo-bounded by the scraper's IP/geo — the Profile Scraper provides "
        "geo-unconstrained content from public profile pages. "
        "Input: JSON with platforms (list), country, brands (list of brand names to filter for), "
        "baselines (list of baseline objects from Profile Scraper output)."
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
        baselines = params.get("baselines", [])

        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                result = loop.run_until_complete(
                    _run_feed_scroll(platforms, country, brands, baselines)
                )
            finally:
                loop.close()
        except Exception as exc:
            logger.error("[FeedScroller] _run error: %s", exc)
            result = {
                "error": str(exc),
                "brand_paid_posts": [],
                "brand_organic_posts": [],
                "category_paid_posts": [],
                "total_posts_scrolled": 0,
            }

        return json.dumps(result)
