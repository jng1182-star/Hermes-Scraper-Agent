"""
TikTok paid ad data via the official Commercial Content API.

Paid ads  → POST https://open.tiktokapis.com/v2/research/adlib/ad/query/
            Auth: client credentials (TIKTOK_APP_ID + TIKTOK_APP_SECRET → 2hr Bearer token)
            No run limit. Docs: developers.tiktok.com/doc/commercial-content-api-query-ads

Organic   → handled by social_search_tool.py (Tavily/DDG) — nothing to do here.

Required env vars:
  TIKTOK_APP_ID      — client_key from TikTok for Developers app
  TIKTOK_APP_SECRET  — client_secret from TikTok for Developers app

Both optional — absent keys skip gracefully with a log message.
"""

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

import requests

# ── Token cache (in-process, thread-safe) ─────────────────────────────────────

_token_lock  = threading.Lock()
_token_cache: dict = {"token": None, "expires_at": 0.0}


def _get_access_token() -> Optional[str]:
    """Fetch or return cached client-credentials Bearer token (2hr TTL)."""
    app_id     = os.getenv("TIKTOK_APP_ID", "")
    app_secret = os.getenv("TIKTOK_APP_SECRET", "")
    if not app_id or not app_secret:
        return None

    with _token_lock:
        now = time.monotonic()
        if _token_cache["token"] and now < _token_cache["expires_at"]:
            return _token_cache["token"]

        try:
            resp = requests.post(
                "https://open.tiktokapis.com/v2/oauth/token/",
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "client_key":    app_id,
                    "client_secret": app_secret,
                    "grant_type":    "client_credentials",
                },
                timeout=15,
            )
            resp.raise_for_status()
            data  = resp.json()
            token = data.get("access_token") or data.get("data", {}).get("access_token")
            if not token:
                print(f"[TikTokAPI] Token response missing access_token: {data}", flush=True)
                return None
            expires_in = int(data.get("expires_in", 7200))
            _token_cache["token"]      = token
            _token_cache["expires_at"] = now + expires_in - 60  # 60s safety buffer
            return token
        except Exception as e:
            print(f"[TikTokAPI] Token fetch failed: {e}", flush=True)
            return None


# ── Input sanitisation ────────────────────────────────────────────────────────

_BRAND_SAFE = re.compile(r"[^a-zA-Z0-9 _\-\.]")

def _sanitise_brand(brand: str) -> str:
    return _BRAND_SAFE.sub("", brand).strip()[:50]


# ── Paid: TikTok Commercial Content API ───────────────────────────────────────

def fetch_tiktok_paid_ads(brand: str, country: str = "", max_results: int = 20) -> list[dict]:
    """
    Query TikTok Ad Library via the official Commercial Content API.

    Returns list of dicts with keys: url, title, content, source_type="paid"
    Returns empty list if TIKTOK_APP_ID/SECRET are absent or API call fails.
    """
    token = _get_access_token()
    if not token:
        print("[TikTokAPI] No access token — paid ads skipped. Set TIKTOK_APP_ID + TIKTOK_APP_SECRET.", flush=True)
        return []

    safe_brand = _sanitise_brand(brand)
    if not safe_brand:
        return []

    end_date   = datetime.utcnow()
    start_date = end_date - timedelta(days=90)

    payload: dict = {
        "filters": {
            "ad_published_date_range": {
                "min": start_date.strftime("%Y%m%d"),
                "max": end_date.strftime("%Y%m%d"),
            },
            "search_term": safe_brand,
            "search_type": "fuzzy_phrase",
        },
        "max_count":        min(max_results, 50),
        "fields":           "id,first_shown_date,last_shown_date,status,reach,videos,image_urls",
        "advertiser_fields": "business_id,business_name",
    }

    if country and len(country) == 2:
        payload["filters"]["country_code"] = [country.upper()]

    ads: list[dict] = []

    try:
        resp = requests.post(
            "https://open.tiktokapis.com/v2/research/adlib/ad/query/",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type":  "application/json",
            },
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        error = data.get("error", {})
        if error.get("code") and error["code"] != "ok":
            print(f"[TikTokAPI] Ad query error: {error}", flush=True)
            return []

        raw_ads = data.get("data", {}).get("ads", [])
        print(f"[TikTokAPI] Paid: {len(raw_ads)} ads for '{safe_brand}'", flush=True)

        for ad in raw_ads:
            ad_info  = ad.get("ad", {})
            adv_info = ad.get("advertiser", {})

            advertiser = adv_info.get("business_name", brand)
            status     = ad_info.get("status", "")
            first_seen = ad_info.get("first_shown_date", "")
            last_seen  = ad_info.get("last_shown_date", "")
            reach      = ad_info.get("reach", "")

            content_parts = [f"Advertiser: {advertiser}"]
            if status:     content_parts.append(f"Status: {status}")
            if first_seen: content_parts.append(f"First seen: {first_seen}")
            if last_seen:  content_parts.append(f"Last seen: {last_seen}")
            if reach:      content_parts.append(f"Reach: {reach}")

            videos = ad_info.get("videos", [])
            if videos and isinstance(videos, list):
                video_url = videos[0].get("video_url", "") if isinstance(videos[0], dict) else ""
                if video_url:
                    content_parts.append(f"Video: {video_url}")

            ads.append({
                "url":         f"https://library.tiktok.com/ads?keyword={safe_brand}",
                "title":       f"{advertiser} — TikTok Paid Ad",
                "content":     " | ".join(content_parts),
                "source_type": "paid",
            })

    except requests.HTTPError as e:
        status_code = e.response.status_code if e.response is not None else "?"
        body        = e.response.text[:300] if e.response is not None else ""
        print(f"[TikTokAPI] HTTP {status_code} on ad query for '{brand}': {body}", flush=True)
    except Exception as e:
        print(f"[TikTokAPI] Paid ad fetch error for '{brand}': {e}", flush=True)

    return ads


# ── Unified entry point ───────────────────────────────────────────────────────

def fetch_tiktok(brand: str, country: str = "", post_type: str = "both") -> list[dict]:
    """
    Fetch TikTok data without a browser.

    post_type: "paid" | "organic" | "both"
    Paid  → TikTok Commercial Content API (native, no run limit)
    Organic → returns [] — handled upstream by Tavily/DDG in social_search_tool.py
    """
    if post_type in ("paid", "both"):
        return fetch_tiktok_paid_ads(brand, country)
    return []
