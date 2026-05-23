import json
import os
import re
from crewai.tools import BaseTool
from duckduckgo_search import DDGS

# ── Supported platforms (only these four) ────────────────────────────────────
SUPPORTED_PLATFORMS = ["TikTok", "Instagram", "YouTube", "Facebook"]

# ── Ad library / paid intelligence sources per platform ──────────────────────
_AD_LIBRARY_QUERIES = {
    "TikTok":    (
        'site:library.tiktok.com OR "TikTok Creative Center" OR "TikTok Ad Library" '
        'OR "TikTok Ads" campaign sponsored'
    ),
    "Instagram": (
        'site:facebook.com/ads/library OR "Meta Ad Library" instagram '
        'OR "sponsored post" instagram campaign'
    ),
    "YouTube":   (
        'site:adstransparency.google.com OR "Google Ads Transparency" youtube '
        'OR "YouTube ads" pre-roll bumper campaign'
    ),
    "Facebook":  (
        'site:facebook.com/ads/library OR "Meta Ad Library" facebook '
        'OR "Facebook Ads Manager" campaign sponsored'
    ),
}

# ── Organic engagement query templates per platform ──────────────────────────
_ORGANIC_QUERIES = {
    "TikTok":    [
        "{brand} TikTok organic views likes comments duet stitch 2025",
        "{brand} TikTok viral video hashtag trending creator",
        "{brand} TikTok UGC user-generated content follower growth",
    ],
    "Instagram": [
        "{brand} Instagram Reels organic likes comments saves reach 2025",
        "{brand} Instagram story highlights engagement impressions",
        "{brand} Instagram UGC influencer collab organic post",
    ],
    "YouTube":   [
        "{brand} YouTube organic views subscribers watch-time 2025",
        "{brand} YouTube channel video likes comments community",
        "{brand} YouTube Shorts organic reach engagement",
    ],
    "Facebook":  [
        "{brand} Facebook page organic reach likes shares reactions 2025",
        "{brand} Facebook post engagement comments followers",
        "{brand} Facebook group community organic discussion",
    ],
}

# ── Paid campaign query templates per platform ────────────────────────────────
_PAID_QUERIES = {
    "TikTok":    [
        "{brand} TikTok paid ad campaign TopView In-Feed spark ads 2025",
        "{brand} TikTok advertising spend media buy influencer paid",
        "{brand} TikTok branded hashtag challenge paid promotion",
    ],
    "Instagram": [
        "{brand} Instagram paid ad campaign story ad reel ad 2025",
        "{brand} Meta ads Instagram sponsored reach impressions CPM",
        "{brand} Instagram influencer paid partnership disclosure",
    ],
    "YouTube":   [
        "{brand} YouTube paid ad campaign TrueView bumper CPM 2025",
        "{brand} YouTube advertising spend media buy pre-roll",
        "{brand} YouTube brand takeover paid promotion",
    ],
    "Facebook":  [
        "{brand} Facebook paid ad campaign boosted post reach 2025",
        "{brand} Facebook ads spend CPM conversion campaign",
        "{brand} Facebook paid media buy audience targeting",
    ],
}


# ── Tavily ────────────────────────────────────────────────────────────────────

def _tavily_search(query: str, max_results: int = 6) -> list[dict]:
    """Returns list of {url, title, content} dicts. Raises if key missing."""
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
    results = []
    for r in resp.get("results", []):
        content = r.get("content") or r.get("snippet") or ""
        if content:
            results.append({
                "url":     r.get("url", ""),
                "title":   r.get("title", ""),
                "content": content,
            })
    return results


# ── DuckDuckGo fallback ───────────────────────────────────────────────────────

def _ddg_search(query: str, max_results: int = 5) -> list[dict]:
    try:
        with DDGS() as ddgs:
            raw = list(ddgs.text(query, max_results=max_results))
            return [
                {"url": r.get("href", ""), "title": r.get("title", ""), "content": r.get("body", "")}
                for r in raw if r.get("body")
            ]
    except Exception:
        return []


# ── Unified search (Tavily → DDG fallback) ────────────────────────────────────

def _search(query: str, max_results: int = 6) -> list[dict]:
    try:
        results = _tavily_search(query, max_results=max_results)
        if results:
            return results
    except Exception:
        pass
    return _ddg_search(query, max_results=max_results)


# ── Query builder helpers ──────────────────────────────────────────────────────

def _extract_brands_and_platforms(query: str, params: dict = None) -> tuple[list[str], list[str], str]:
    """
    Returns (brands, platforms, post_type).
    post_type: 'paid' | 'organic' | 'both'
    Platforms restricted to SUPPORTED_PLATFORMS only.
    """
    params = params or {}
    post_type = params.get("post_type", "both") or "both"

    # Platforms — from params first, then parse query
    raw_platforms = params.get("platforms") or []
    platforms = [p for p in raw_platforms if p in SUPPORTED_PLATFORMS]

    if not platforms:
        plat_match = re.search(r'\bon\s+([\w/,\s]+?)(?:\s*\(|$)', query, re.IGNORECASE)
        if plat_match:
            for tok in re.split(r'[,\s/]+', plat_match.group(1)):
                tok = tok.strip().lower()
                for sp in SUPPORTED_PLATFORMS:
                    if tok in sp.lower():
                        platforms.append(sp)

    # Always restrict to supported set
    platforms = [p for p in platforms if p in SUPPORTED_PLATFORMS]
    if not platforms:
        platforms = list(SUPPORTED_PLATFORMS)  # default: all four

    # Brands — from params (advertisers + competitors), then query
    brands = []
    advertisers = params.get("advertisers") or []
    competitors = params.get("competitors") or []
    if isinstance(advertisers, str):
        advertisers = [a.strip() for a in advertisers.split(",") if a.strip()]
    if isinstance(competitors, str):
        competitors = [c.strip() for c in competitors.split(",") if c.strip()]

    for b in list(advertisers) + list(competitors):
        if b and b not in brands:
            brands.append(b)

    if not brands:
        brand_part = re.split(r'\s+in\s+|\s+on\s+', query, flags=re.IGNORECASE)[0]
        brand_part = re.sub(r'\bvs\.?\b', ',', brand_part, flags=re.IGNORECASE)
        for b in brand_part.split(','):
            b = b.strip().strip('[]')
            if b and len(b) > 1:
                brands.append(b)

    if not brands:
        brands = [query.split()[0]] if query.split() else ["brand"]

    return brands, platforms, post_type


# ── Snippet enrichment ────────────────────────────────────────────────────────

def _enrich_snippets(results: list[dict], source_type: str) -> list[dict]:
    """Tag each result with source_type ('paid' or 'organic')."""
    return [
        {**r, "source_type": source_type}
        for r in results
        if r.get("content")
    ]


# ── Main tool ─────────────────────────────────────────────────────────────────

class SocialSearchTool(BaseTool):
    name: str = "Social Media Intelligence Search"
    description: str = (
        "Searches for PAID and ORGANIC social media intelligence for one or more brands "
        "across Facebook, Instagram, TikTok, and YouTube. "
        "Input: a query string describing the brand(s), platform(s), country, date range, "
        "and post_type (paid|organic|both). "
        "Returns structured JSON with raw snippets tagged paid/organic per brand and platform, "
        "including post content, hashtags, engagement numbers, and ad library references."
    )

    def _run(self, query: str) -> str:
        # Try to pull params from the query string itself (agents may inject JSON)
        params: dict = {}
        try:
            bracket = query.find('{')
            if bracket != -1:
                params = json.loads(query[bracket:])
                query  = query[:bracket].strip()
        except Exception:
            pass

        brands, platforms, post_type = _extract_brands_and_platforms(query, params)
        country   = params.get("country", "")
        date_hint = params.get("date_range", "2025")
        keywords  = params.get("keywords", "")

        geo = f" {country}" if country else ""
        kw  = f" {keywords}" if keywords else ""

        results: list[dict] = []

        for brand in brands:
            brand_entry: dict = {
                "brand":         brand,
                "platform_data": [],
                "posts_found":   0,
            }

            for platform in platforms:
                platform_snippets: list[dict] = []

                # ── PAID: Ad library + paid campaign queries ──────────────
                if post_type in ("paid", "both"):
                    ad_lib_q = _AD_LIBRARY_QUERIES.get(platform, "")
                    if ad_lib_q:
                        raw = _search(f"{brand} {ad_lib_q}{geo}", max_results=5)
                        platform_snippets.extend(_enrich_snippets(raw, "paid"))

                    for tpl in _PAID_QUERIES.get(platform, []):
                        q = tpl.format(brand=brand) + geo + kw
                        raw = _search(q, max_results=4)
                        platform_snippets.extend(_enrich_snippets(raw, "paid"))

                    # Estimated spend / media investment signals
                    raw = _search(
                        f"{brand} {platform} advertising budget media spend investment{geo} {date_hint}",
                        max_results=3,
                    )
                    platform_snippets.extend(_enrich_snippets(raw, "paid"))

                # ── ORGANIC: Engagement + content queries ─────────────────
                if post_type in ("organic", "both"):
                    for tpl in _ORGANIC_QUERIES.get(platform, []):
                        q = tpl.format(brand=brand) + geo + kw
                        raw = _search(q, max_results=4)
                        platform_snippets.extend(_enrich_snippets(raw, "organic"))

                    # Hashtag / viral content discovery
                    raw = _search(
                        f"#{brand.replace(' ','')} {platform} viral trending{geo} {date_hint}",
                        max_results=3,
                    )
                    platform_snippets.extend(_enrich_snippets(raw, "organic"))

                    # Follower / audience size signals (for engagement rate denominator)
                    raw = _search(
                        f"{brand} {platform} followers subscribers audience size{geo} {date_hint}",
                        max_results=3,
                    )
                    platform_snippets.extend(_enrich_snippets(raw, "organic"))

                # ── Cross-platform competitive benchmark ──────────────────
                raw = _search(
                    f"{brand} vs competitors {platform} engagement share of voice{geo} {date_hint}",
                    max_results=3,
                )
                platform_snippets.extend(_enrich_snippets(raw, "both"))

                if platform_snippets:
                    brand_entry["platform_data"].append({
                        "platform":    platform,
                        "raw_results": platform_snippets,  # each has url, title, content, source_type
                    })
                    brand_entry["posts_found"] += len(platform_snippets)

            # ── General brand social health ───────────────────────────────
            gen_q = (
                f"{brand} social media presence overview {' '.join(platforms)} "
                f"followers engagement rate benchmark{geo} {date_hint}"
            )
            gen = _search(gen_q, max_results=4)
            if gen:
                brand_entry["general_presence"] = _enrich_snippets(gen, "organic")

            results.append(brand_entry)

        # ── Fallback to golden dataset ────────────────────────────────────
        if not any(b["platform_data"] for b in results):
            golden_path = os.path.join(os.path.dirname(__file__), "../data/golden_dataset.json")
            if os.path.exists(golden_path):
                with open(golden_path, "r") as f:
                    data = json.load(f)
                return "SEARCH FAILED — using seed data fallback:\n" + json.dumps(data, indent=2)
            return "Search failed and no fallback data available."

        total_snippets = sum(b["posts_found"] for b in results)
        return json.dumps({
            "query_meta": {
                "brands":     brands,
                "platforms":  platforms,
                "post_type":  post_type,
                "country":    country,
                "date_range": date_hint,
                "total_snippets_retrieved": total_snippets,
            },
            "brand_results": results,
        }, indent=2)
