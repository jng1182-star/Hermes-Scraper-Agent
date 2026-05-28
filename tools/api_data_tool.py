"""
API Data Tool — structured public data via official APIs.

Priority chain per platform:
  YouTube  : YouTube Data API v3 (channel search + statistics + recent videos)
             → SocialBlade public HTML fallback (no key needed)
  Facebook : Meta Ad Library API (declared ads + impression ranges)
             → Facebook Graph public page info (no token, limited)

Why APIs instead of Playwright:
  Playwright scraping of Facebook/YouTube fails on Railway because pages
  serve login walls or bot-detection challenges to headless browsers with
  no cookies. Official APIs bypass this entirely — they are designed for
  programmatic access, return structured JSON, and work from any IP.

Required env vars (optional — tool degrades gracefully without them):
  YOUTUBE_API_KEY        — Google Cloud project with YouTube Data API v3 enabled
  META_AD_LIBRARY_TOKEN  — Meta developer token (free, from developers.facebook.com)
"""

import json
import logging
import os
import re
import ssl
import time
import urllib.parse
import urllib.request
from crewai.tools import BaseTool

# macOS system Python lacks the CA bundle that certifi provides;
# use an unverified context so API calls don't silently fail locally.
_SSL_CTX = ssl._create_unverified_context()

logger = logging.getLogger(__name__)

_YT_KEY   = os.getenv("YOUTUBE_API_KEY", "")
_META_TOK = os.getenv("META_AD_LIBRARY_TOKEN", "")

_HTTP_TIMEOUT = 12  # seconds

# Preflight: verify Meta token has ads_read permission before any agent call.
# A single cheap /me request tells us whether the token is valid — avoids N×brands
# HTTP 400 warnings when the token lacks ads_read.
_META_TOK_INVALID = False

def _preflight_meta_token() -> bool:
    """Return True if token appears valid (has ads_read). Sets _META_TOK_INVALID on failure."""
    global _META_TOK_INVALID
    if not _META_TOK:
        return False
    url = f"https://graph.facebook.com/v19.0/ads_archive?access_token={_META_TOK}&search_terms=test&ad_reached_countries=US&ad_active_status=ALL&fields=id&limit=1"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8, context=_SSL_CTX) as r:
            return True
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="ignore")[:300]
        try:
            err = json.loads(body)
            if e.code == 400 and err.get("error", {}).get("code") == 10:
                _META_TOK_INVALID = True
                logger.error(
                    "[APIDataTool] Meta Ad Library token lacks 'ads_read' permission "
                    "(OAuthException code 10). All Meta API calls disabled for this run. "
                    "Fix: developers.facebook.com → your app → Permissions → request 'ads_read'."
                )
                return False
        except Exception:
            pass
    except Exception:
        pass
    return True

if _META_TOK:
    _preflight_meta_token()


def _get(url: str, headers: dict = None) -> dict | None:
    try:
        req = urllib.request.Request(url, headers=headers or {
            "User-Agent": "Mozilla/5.0 (compatible; HermesBot/1.0)"
        })
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT, context=_SSL_CTX) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="ignore")[:300]
        logger.warning("[APIDataTool] HTTP %d for %s — %s", e.code, url[:120], body)
        # Detect Meta Ad Library permission error (OAuthException code 10) and
        # disable all subsequent Meta API calls — retrying with the same token
        # will always fail (requires Ad Library API access to be granted at
        # developers.facebook.com/apps → Permissions → ads_read).
        if e.code == 400 and "graph.facebook.com" in url:
            try:
                err_body = json.loads(body)
                if err_body.get("error", {}).get("code") == 10 and not _META_TOK_INVALID:
                    global _META_TOK_INVALID
                    _META_TOK_INVALID = True
                    logger.error(
                        "[APIDataTool] Meta Ad Library token lacks 'ads_read' permission "
                        "(OAuthException code 10). Disabling Meta API for this run. "
                        "Fix: go to developers.facebook.com → your app → Permissions → "
                        "request 'ads_read', then regenerate the token."
                    )
            except Exception:
                pass
        return None
    except Exception as e:
        logger.warning("[APIDataTool] GET failed: %s — %s", url[:120], e)
        return None


# ── YouTube Data API v3 ───────────────────────────────────────────────────────

def _yt_search_channel(brand: str) -> str | None:
    """Find YouTube channelId for a brand name."""
    if not _YT_KEY:
        return None
    q = urllib.parse.quote_plus(f"{brand} official")
    url = (
        f"https://www.googleapis.com/youtube/v3/search"
        f"?part=snippet&type=channel&q={q}&maxResults=3&key={_YT_KEY}"
    )
    data = _get(url)
    if not data:
        return None
    items = data.get("items", [])
    if not items:
        return None
    # Prefer exact name match
    for item in items:
        title = item.get("snippet", {}).get("channelTitle", "").lower()
        if brand.lower() in title:
            return item["id"]["channelId"]
    return items[0]["id"]["channelId"]


def _yt_channel_stats(channel_id: str) -> dict:
    """Fetch subscriber count and total view/video counts."""
    if not _YT_KEY or not channel_id:
        return {}
    url = (
        f"https://www.googleapis.com/youtube/v3/channels"
        f"?part=statistics,snippet&id={channel_id}&key={_YT_KEY}"
    )
    data = _get(url)
    if not data:
        return {}
    items = data.get("items", [])
    if not items:
        return {}
    stats = items[0].get("statistics", {})
    return {
        "subscribers":   int(stats.get("subscriberCount", 0)),
        "total_views":   int(stats.get("viewCount", 0)),
        "video_count":   int(stats.get("videoCount", 0)),
        "channel_title": items[0].get("snippet", {}).get("title", ""),
    }


def _yt_recent_videos(channel_id: str, max_results: int = 10) -> list[dict]:
    """Fetch recent video IDs."""
    if not _YT_KEY or not channel_id:
        return []
    url = (
        f"https://www.googleapis.com/youtube/v3/search"
        f"?part=snippet&channelId={channel_id}&order=date"
        f"&type=video&maxResults={max_results}&key={_YT_KEY}"
    )
    data = _get(url)
    if not data:
        return []
    return [
        {
            "video_id": item["id"].get("videoId", ""),
            "title":    item["snippet"].get("title", ""),
            "published": item["snippet"].get("publishedAt", ""),
        }
        for item in data.get("items", [])
        if item.get("id", {}).get("videoId")
    ]


def _yt_video_stats(video_ids: list[str]) -> list[dict]:
    """Fetch likes + view counts for up to 50 video IDs."""
    if not _YT_KEY or not video_ids:
        return []
    ids_str = ",".join(video_ids[:50])
    url = (
        f"https://www.googleapis.com/youtube/v3/videos"
        f"?part=statistics,snippet&id={ids_str}&key={_YT_KEY}"
    )
    data = _get(url)
    if not data:
        return []
    results = []
    for item in data.get("items", []):
        stats = item.get("statistics", {})
        results.append({
            "video_id":  item["id"],
            "title":     item["snippet"].get("title", ""),
            "views":     int(stats.get("viewCount", 0)),
            "likes":     int(stats.get("likeCount", 0)),
            "comments":  int(stats.get("commentCount", 0)),
            "published": item["snippet"].get("publishedAt", ""),
        })
    return results


def _scrape_youtube_brand(brand: str) -> dict:
    """Full YouTube brand profile via API. Returns structured metrics or {} on failure."""
    channel_id = _yt_search_channel(brand)
    if not channel_id:
        logger.info("[APIDataTool] No YouTube channel found for '%s'", brand)
        return {}

    stats   = _yt_channel_stats(channel_id)
    videos  = _yt_recent_videos(channel_id, max_results=10)
    v_ids   = [v["video_id"] for v in videos if v["video_id"]]
    v_stats = _yt_video_stats(v_ids) if v_ids else []

    avg_views    = int(sum(v["views"]   for v in v_stats) / len(v_stats)) if v_stats else 0
    avg_likes    = int(sum(v["likes"]   for v in v_stats) / len(v_stats)) if v_stats else 0
    avg_comments = int(sum(v["comments"] for v in v_stats) / len(v_stats)) if v_stats else 0
    total_interactions = avg_likes + avg_comments

    # ER for YouTube = (likes + comments) / views
    er_pct = round((total_interactions / avg_views * 100), 2) if avg_views > 0 else 0.0

    top_posts = []
    for v in sorted(v_stats, key=lambda x: x["views"], reverse=True)[:5]:
        top_posts.append({
            "caption":   v["title"],
            "url":       f"https://www.youtube.com/watch?v={v['video_id']}",
            "post_type": "organic",
            "likes":     v["likes"],
            "views":     v["views"],
        })

    return {
        "platform":     "YouTube",
        "data_source":  "youtube_data_api_v3",
        "channel_id":   channel_id,
        "followers":    stats.get("subscribers", 0),
        "total_views":  stats.get("total_views", 0),
        "video_count":  stats.get("video_count", 0),
        "avg_views":    avg_views,
        "avg_likes":    avg_likes,
        "avg_comments": avg_comments,
        "er_pct":       er_pct,
        "top_posts":    top_posts,
        "videos_sampled": len(v_stats),
        "confidence":   "high" if v_stats else "medium",
    }


# ── Meta Ad Library API ───────────────────────────────────────────────────────

# Full country name → ISO-2 code (mirrors proxy_manager.py)
_COUNTRY_TO_CODE = {
    "Thailand":    "TH",
    "Philippines": "PH",
    "Vietnam":     "VN",
    "Indonesia":   "ID",
    "Malaysia":    "MY",
    "Singapore":   "SG",
}

def _resolve_country_code(country: str) -> str:
    """Accept full name ('Philippines') or ISO code ('PH'), always return ISO-2 uppercase."""
    if not country:
        return "PH"
    return _COUNTRY_TO_CODE.get(country, country.upper()[:2])


def _meta_ad_library(brand: str, country: str = "PH") -> dict:
    """
    Query Meta Ad Library API for active/recent ads in the specified market.
    country: full name ('Philippines') or ISO-2 code ('PH').
    Token: free from developers.facebook.com/tools/explorer
    """
    if not _META_TOK or _META_TOK_INVALID:
        return {}
    country_code = _resolve_country_code(country)
    q = urllib.parse.quote_plus(brand)
    # ad_reached_countries filters to ads that ran in this specific market
    url = (
        f"https://graph.facebook.com/v19.0/ads_archive"
        f"?access_token={_META_TOK}"
        f"&search_terms={q}"
        f"&ad_reached_countries={country_code}"
        f"&ad_active_status=ALL"
        f"&fields=id,ad_creative_bodies,ad_creative_link_captions,ad_creative_link_titles,"
        f"ad_delivery_start_time,ad_delivery_stop_time,impressions,spend,"
        f"page_name,publisher_platforms"
        f"&limit=100"
    )
    data = _get(url)
    if not data or "data" not in data:
        return {}

    ads = data.get("data", [])
    if not ads:
        return {}

    # Aggregate impression + spend ranges; collect start dates for time-series bucketing
    total_imp_min = total_imp_max = 0
    total_spend_min = total_spend_max = 0
    captions = []
    start_dates = []

    for ad in ads:
        imp = ad.get("impressions", {})
        if imp:
            total_imp_min += int(str(imp.get("lower_bound", 0)).replace(",", "") or 0)
            total_imp_max += int(str(imp.get("upper_bound", 0)).replace(",", "") or 0)
        spend = ad.get("spend", {})
        if spend:
            total_spend_min += int(str(spend.get("lower_bound", 0)).replace(",", "") or 0)
            total_spend_max += int(str(spend.get("upper_bound", 0)).replace(",", "") or 0)
        bodies = ad.get("ad_creative_bodies", [])
        if bodies:
            captions.extend(bodies[:2])
        # Capture delivery start date (ISO-8601 date string)
        s = ad.get("ad_delivery_start_time", "")
        if s:
            start_dates.append(s[:10])  # trim to YYYY-MM-DD

    # Build time-series buckets from start dates
    from collections import defaultdict
    import datetime as _dt
    _by_day: dict = defaultdict(int)
    _by_week: dict = defaultdict(int)
    _by_month: dict = defaultdict(int)
    for d in start_dates:
        try:
            dt = _dt.date.fromisoformat(d)
            _by_day[d] += 1
            iso_year, iso_week, _ = dt.isocalendar()
            _by_week[f"{iso_year}-W{iso_week:02d}"] += 1
            _by_month[f"{dt.year}-{dt.month:02d}"] += 1
        except Exception:
            pass

    by_day   = [{"period": k, "ad_count": v} for k, v in sorted(_by_day.items())]
    by_week  = [{"period": k, "ad_count": v} for k, v in sorted(_by_week.items())]
    by_month = [{"period": k, "ad_count": v} for k, v in sorted(_by_month.items())]

    return {
        "platform":         "Facebook",
        "data_source":      "meta_ad_library_api",
        "active_ads_found": len(ads),
        "impressions_min":  total_imp_min,
        "impressions_max":  total_imp_max,
        "spend_min_usd":    total_spend_min,
        "spend_max_usd":    total_spend_max,
        "ad_captions":      captions[:5],
        "ad_start_dates":   start_dates,
        "by_day":           by_day,
        "by_week":          by_week,
        "by_month":         by_month,
        "confidence":       "high" if ads else "low",
    }


# ── Facebook public page info (no token, limited) ────────────────────────────

def _fb_public_page(brand: str) -> dict:
    """
    Open Graph scrape of fb.com/{brand} — returns likes/followers count
    from og:description or page meta. Works without auth on public pages.
    Only returns follower count — no post-level data.
    """
    slug = re.sub(r"[^a-zA-Z0-9]", "", brand.lower().replace(" ", ""))
    url  = f"https://www.facebook.com/{slug}"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)"
        })
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
            html = r.read().decode("utf-8", errors="ignore")
        # Extract follower count from page source
        m = re.search(r'"follower_count"\s*:\s*(\d+)', html)
        if not m:
            m = re.search(r'([\d,]+)\s+(?:people follow|followers)', html)
        followers = int(m.group(1).replace(",", "")) if m else 0
        return {
            "platform":    "Facebook",
            "data_source": "facebook_public_page",
            "followers":   followers,
            "confidence":  "medium" if followers > 0 else "low",
        }
    except Exception as e:
        logger.debug("[APIDataTool] FB public page failed for '%s': %s", brand, e)
        return {}


# ── SocialBlade fallback ──────────────────────────────────────────────────────

def _socialblade_yt(brand: str) -> dict:
    """
    SocialBlade public page — no auth, gives subscriber/view estimates.
    Used when YOUTUBE_API_KEY is not set.
    """
    slug = urllib.parse.quote_plus(brand.lower().replace(" ", ""))
    url  = f"https://socialblade.com/youtube/user/{slug}"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as r:
            html = r.read().decode("utf-8", errors="ignore")
        # Extract subscriber count
        m_subs = re.search(r'Total Subscribers[^<]*<[^>]+>([0-9,\.KMB]+)', html)
        m_views = re.search(r'Total Video Views[^<]*<[^>]+>([0-9,\.KMB]+)', html)

        def _parse(s: str) -> int:
            if not s:
                return 0
            s = s.replace(",", "").strip()
            if s.endswith("B"):
                return int(float(s[:-1]) * 1_000_000_000)
            if s.endswith("M"):
                return int(float(s[:-1]) * 1_000_000)
            if s.endswith("K"):
                return int(float(s[:-1]) * 1_000)
            try:
                return int(s)
            except Exception:
                return 0

        subs  = _parse(m_subs.group(1))  if m_subs  else 0
        views = _parse(m_views.group(1)) if m_views else 0
        if subs == 0 and views == 0:
            return {}
        return {
            "platform":    "YouTube",
            "data_source": "socialblade_public",
            "followers":   subs,
            "total_views": views,
            "confidence":  "medium",
        }
    except Exception as e:
        logger.debug("[APIDataTool] SocialBlade failed for '%s': %s", brand, e)
        return {}


# ── Main tool ─────────────────────────────────────────────────────────────────

class APIDataTool(BaseTool):
    name: str = "Brand API Data Fetcher"
    description: str = (
        "Fetches real structured social media metrics for brands using official APIs "
        "(YouTube Data API v3, Meta Ad Library API) with public fallbacks. "
        "Returns exact subscriber counts, view counts, likes, comment counts, "
        "and declared ad impression ranges — all from authoritative sources, not scraped HTML. "
        "Input: JSON with brand name, platforms list, and country code."
    )

    def _run(self, query: str) -> str:
        params: dict = {}
        try:
            bracket = query.find("{")
            if bracket != -1:
                params = json.loads(query[bracket:])
        except Exception:
            pass

        brands    = params.get("brands", [query.split("{")[0].strip()])
        platforms = params.get("platforms", ["YouTube", "Facebook"])
        country   = params.get("country", "PH")
        # Accept either a 'markets' list (multi-market) or fall back to single 'country'
        markets   = params.get("markets") or ([country] if country else ["PH"])

        results = []
        for brand in brands:
            brand_result = {"brand": brand, "platform_data": []}

            if "YouTube" in platforms:
                yt = _scrape_youtube_brand(brand)
                if not yt and not _YT_KEY:
                    yt = _socialblade_yt(brand)
                if yt:
                    brand_result["platform_data"].append(yt)
                    logger.info(
                        "[APIDataTool] YouTube '%s': %d subs, %d avg views (source: %s)",
                        brand, yt.get("followers", 0), yt.get("avg_views", 0), yt.get("data_source")
                    )

            if "Facebook" in platforms or "Instagram" in platforms:
                # Query Meta Ad Library once per market so country filter is applied correctly.
                # Results are tagged with their market so the analyst can split per market.
                any_fb = False
                for mkt in markets:
                    fb = _meta_ad_library(brand, country=mkt)
                    if fb:
                        fb["market"] = mkt
                        brand_result["platform_data"].append(fb)
                        logger.info(
                            "[APIDataTool] Facebook '%s' [%s]: %d ads found (source: %s)",
                            brand, mkt, fb.get("active_ads_found", 0), fb.get("data_source")
                        )
                        any_fb = True
                if not any_fb:
                    # Fallback: public page info (no market filter available)
                    fb = _fb_public_page(brand)
                    if fb:
                        brand_result["platform_data"].append(fb)

            if not brand_result["platform_data"]:
                brand_result["note"] = (
                    "No API data available. Set YOUTUBE_API_KEY and META_AD_LIBRARY_TOKEN "
                    "in Railway environment variables for real data."
                )

            results.append(brand_result)

        return json.dumps({
            "api_data": results,
            "sources_used": {
                "youtube": "youtube_data_api_v3" if _YT_KEY else "socialblade_public_fallback",
                "facebook": "meta_ad_library_api" if _META_TOK else "facebook_public_page",
            },
            "missing_keys": [
                k for k, v in {
                    "YOUTUBE_API_KEY": _YT_KEY,
                    "META_AD_LIBRARY_TOKEN": _META_TOK,
                }.items() if not v
            ],
        }, indent=2)
