import json
import os
import re
from crewai.tools import BaseTool
from duckduckgo_search import DDGS


# Native ad library URLs — primary sources for paid ad intelligence
_AD_LIBRARY_QUERIES = {
    "TikTok":      "site:library.tiktok.com OR \"TikTok Creative Center\" OR \"TikTok Ad Library\"",
    "Instagram":   "site:facebook.com/ads/library OR \"Meta Ad Library\" instagram",
    "Facebook":    "site:facebook.com/ads/library OR \"Meta Ad Library\" facebook",
    "YouTube":     "site:adstransparency.google.com OR \"Google Ads Transparency\" youtube",
    "X / Twitter": "site:ads.twitter.com/transparency OR \"Twitter Ad Transparency\"",
    "LinkedIn":    "site:linkedin.com/ad-library OR \"LinkedIn Ad Library\"",
}

_PLATFORM_QUERIES = {
    "TikTok":      ["TikTok views likes duet", "TikTok viral post hashtag"],
    "Instagram":   ["Instagram Reels likes comments", "Instagram post engagement"],
    "YouTube":     ["YouTube views subscribers likes", "YouTube video comments"],
    "Facebook":    ["Facebook post shares reactions", "Facebook page followers"],
    "X / Twitter": ["Twitter tweet likes retweets", "X.com tweet impressions"],
    "LinkedIn":    ["LinkedIn post impressions reactions", "LinkedIn engagement"],
}
_DEFAULT_PLATFORMS = list(_PLATFORM_QUERIES.keys())


# ── Tavily ────────────────────────────────────────────────────────────────────

def _tavily_search(query: str, max_results: int = 5) -> list[str]:
    """Search via Tavily. Returns list of content snippets. Raises if key missing/invalid."""
    from tavily import TavilyClient
    key = os.getenv("TAVILY_API_KEY", "")
    if not key:
        raise EnvironmentError("TAVILY_API_KEY not set")
    client = TavilyClient(api_key=key)
    resp = client.search(
        query,
        search_depth="advanced",
        topic="general",
        max_results=max_results,
        include_answer=False,
    )
    snippets = []
    for r in resp.get("results", []):
        content = r.get("content") or r.get("snippet") or ""
        if content:
            snippets.append(content)
    return snippets


# ── DuckDuckGo fallback ───────────────────────────────────────────────────────

def _ddg_search(query: str, max_results: int = 4) -> list[str]:
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
            return [r.get("body", "") for r in results if r.get("body")]
    except Exception:
        return []


# ── Unified search (Tavily → DDG fallback) ────────────────────────────────────

def _search(query: str, max_results: int = 5) -> list[str]:
    try:
        results = _tavily_search(query, max_results=max_results)
        if results:
            return results
    except Exception:
        pass
    return _ddg_search(query, max_results=max_results)


# ── Query builder helpers ──────────────────────────────────────────────────────

def _extract_brands_and_platforms(query: str) -> tuple[list[str], list[str]]:
    """Parse 'Nike vs Adidas, Puma in Singapore on TikTok, Instagram (Last 30 days)'."""
    brands: list[str] = []
    platforms: list[str] = []

    plat_match = re.search(r'\bon\s+([\w/,\s]+?)(?:\s*\(|$)', query, re.IGNORECASE)
    if plat_match:
        raw = plat_match.group(1)
        for p in re.split(r'[,\s]+', raw):
            p = p.strip()
            for known in _PLATFORM_QUERIES:
                if p.lower() in known.lower():
                    platforms.append(known)

    brand_part = re.split(r'\s+in\s+|\s+on\s+', query, flags=re.IGNORECASE)[0]
    brand_part = re.sub(r'\bvs\.?\b', ',', brand_part, flags=re.IGNORECASE)
    for b in brand_part.split(','):
        b = b.strip().strip('[]')
        if b and len(b) > 1:
            brands.append(b)

    if not platforms:
        platforms = ["TikTok", "Instagram"]
    if not brands:
        brands = [query.split()[0]] if query.split() else ["brand"]

    return brands, platforms


# ── Main tool ─────────────────────────────────────────────────────────────────

class SocialSearchTool(BaseTool):
    name: str = "Social Media Intelligence Search"
    description: str = (
        "Searches for social media content and engagement metrics for one or more brands "
        "across platforms. Input: a query string describing the brand(s), platform(s), "
        "country and date range. Returns post content snippets, hashtags, and engagement "
        "numbers found across TikTok, Instagram, YouTube, and other platforms."
    )

    def _run(self, query: str) -> str:
        brands, platforms = _extract_brands_and_platforms(query)
        results: list[dict] = []

        for brand in brands:
            brand_results: dict = {"brand": brand, "platform_data": []}

            for platform in platforms:
                plat_queries = _PLATFORM_QUERIES.get(platform, [f"{platform} post engagement"])
                snippets: list[str] = []

                # ── Priority 1: Native ad library ─────────────────────────
                ad_lib_q = _AD_LIBRARY_QUERIES.get(platform, "")
                if ad_lib_q:
                    snippets.extend(_search(
                        f"{brand} {ad_lib_q} ads campaigns", max_results=4
                    ))

                # ── Priority 2: Platform-specific engagement queries ───────
                for pq in plat_queries:
                    snippets.extend(_search(f"{brand} {pq}", max_results=3))

                # Post content / captions
                snippets.extend(_search(
                    f"{brand} {platform} post caption hashtag 2025 campaign", max_results=3
                ))

                # Trending hashtags
                snippets.extend(_search(
                    f"#{brand.replace(' ', '')} {platform} trending viral", max_results=2
                ))

                # Competitor comparison
                snippets.extend(_search(
                    f"{brand} vs competitors {platform} engagement followers", max_results=2
                ))

                if snippets:
                    brand_results["platform_data"].append({
                        "platform": platform,
                        "raw_snippets": snippets,
                    })

            # General brand social presence
            general = _search(
                f"{brand} social media strategy followers growth {' '.join(platforms)} 2025",
                max_results=4,
            )
            if general:
                brand_results["general_presence"] = general

            results.append(brand_results)

        if not any(b["platform_data"] for b in results):
            golden_path = os.path.join(os.path.dirname(__file__), "../data/golden_dataset.json")
            if os.path.exists(golden_path):
                with open(golden_path, "r") as f:
                    data = json.load(f)
                return (
                    "SEARCH FAILED — using seed data fallback:\n"
                    + json.dumps(data, indent=2)
                )
            return "Search failed and no fallback data available."

        return json.dumps(results, indent=2)
