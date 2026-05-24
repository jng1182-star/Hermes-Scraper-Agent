import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from crewai.tools import BaseTool
from duckduckgo_search import DDGS

# ── Supported platforms (only these four) ────────────────────────────────────
SUPPORTED_PLATFORMS = ["TikTok", "Instagram", "YouTube", "Facebook"]

# ── Single best-signal paid query per platform ───────────────────────────────
_PAID_QUERIES = {
    "TikTok":    "{brand} TikTok paid ad campaign TopView In-Feed sponsored 2025{geo}",
    "Instagram": "{brand} Instagram paid ad campaign story reel sponsored Meta Ads Library 2025{geo}",
    "YouTube":   "{brand} YouTube paid ad TrueView bumper pre-roll advertising spend 2025{geo}",
    "Facebook":  "{brand} Facebook paid ad boosted post Meta Ads Library campaign 2025{geo}",
}

# ── Single best-signal organic query per platform ────────────────────────────
_ORGANIC_QUERIES = {
    "TikTok":    "{brand} TikTok organic views likes comments followers viral 2025{geo}",
    "Instagram": "{brand} Instagram organic Reels likes saves comments followers reach 2025{geo}",
    "YouTube":   "{brand} YouTube organic views subscribers watch-time engagement 2025{geo}",
    "Facebook":  "{brand} Facebook organic page reach likes shares reactions followers 2025{geo}",
}


# ── Tavily ────────────────────────────────────────────────────────────────────

def _tavily_search(query: str, max_results: int = 3) -> list[dict]:
    from tavily import TavilyClient
    key = os.getenv("TAVILY_API_KEY", "")
    if not key:
        raise EnvironmentError("TAVILY_API_KEY not set")
    client = TavilyClient(api_key=key)
    resp = client.search(
        query,
        search_depth="basic",
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

def _ddg_search(query: str, max_results: int = 3) -> list[dict]:
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

def _search(query: str, max_results: int = 3) -> list[dict]:
    try:
        results = _tavily_search(query, max_results=max_results)
        if results:
            return results
    except Exception:
        pass
    return _ddg_search(query, max_results=max_results)


# ── Query builder helpers ──────────────────────────────────────────────────────

def _extract_brands_and_platforms(query: str, params: dict = None) -> tuple[list[str], list[str], str]:
    params = params or {}
    post_type = params.get("post_type", "both") or "both"

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

    platforms = [p for p in platforms if p in SUPPORTED_PLATFORMS]
    if not platforms:
        platforms = list(SUPPORTED_PLATFORMS)

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
    return [
        {**r, "source_type": source_type}
        for r in results
        if r.get("content")
    ]


# ── Parallel search helper ────────────────────────────────────────────────────

def _parallel_search(tasks: list[tuple[str, str]], max_workers: int = 5) -> list[dict]:
    """
    tasks: list of (query, source_type) tuples.
    Returns flat list of enriched snippets, order not guaranteed.
    max_workers capped at 5 to stay within Tavily rate limits.
    """
    snippets: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(max_workers, len(tasks))) as pool:
        futures = {pool.submit(_search, q, 3): src for q, src in tasks}
        for fut in as_completed(futures):
            src = futures[fut]
            try:
                snippets.extend(_enrich_snippets(fut.result(), src))
            except Exception:
                pass
    return snippets


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

        geo = f" {country}" if country else ""

        # Lazy-load PaidAdLibTool — graceful fallback if playwright not installed
        _paid_tool = None
        if post_type in ("paid", "both"):
            try:
                from tools.paid_adlib_tool import PaidAdLibTool
                _paid_tool = PaidAdLibTool()
            except Exception:
                pass

        results: list[dict] = []

        for brand in brands:
            brand_entry: dict = {
                "brand":         brand,
                "platform_data": [],
                "posts_found":   0,
            }

            # ── PAID: Playwright ad library scraper (one call per brand) ──────
            if post_type in ("paid", "both") and _paid_tool is not None:
                try:
                    paid_raw  = _paid_tool._run(json.dumps({
                        "brand":     brand,
                        "country":   country,
                        "platforms": platforms,
                    }))
                    paid_data = json.loads(paid_raw)
                    for paid_plat in paid_data.get("platform_data", []):
                        matched = next(
                            (p for p in brand_entry["platform_data"]
                             if p["platform"] == paid_plat["platform"]),
                            None,
                        )
                        if matched:
                            matched["raw_results"].extend(paid_plat.get("raw_results", []))
                        else:
                            brand_entry["platform_data"].append(paid_plat)
                        brand_entry["posts_found"] += len(paid_plat.get("raw_results", []))
                except Exception as e:
                    print(f"[SocialSearch] PaidAdLibTool failed for '{brand}': {e}", flush=True)

            for platform in platforms:
                # ── ORGANIC: Tavily/DDG search ─────────────────────────────
                search_tasks: list[tuple[str, str]] = []

                if post_type in ("organic", "both"):
                    q = _ORGANIC_QUERIES[platform].format(brand=brand, geo=geo)
                    search_tasks.append((q, "organic"))

                # Cross-platform competitive benchmark (always)
                search_tasks.append((
                    f"{brand} vs competitors {platform} engagement share of voice{geo} {date_hint}",
                    "both",
                ))

                platform_snippets = _parallel_search(search_tasks)

                if platform_snippets:
                    matched = next(
                        (p for p in brand_entry["platform_data"]
                         if p["platform"] == platform),
                        None,
                    )
                    if matched:
                        matched["raw_results"].extend(platform_snippets)
                    else:
                        brand_entry["platform_data"].append({
                            "platform":    platform,
                            "raw_results": platform_snippets,
                        })
                    brand_entry["posts_found"] += len(platform_snippets)

            # General brand social health (1 query per brand)
            gen_q = (
                f"{brand} social media presence overview {' '.join(platforms)} "
                f"followers engagement rate benchmark{geo} {date_hint}"
            )
            gen = _search(gen_q, max_results=3)
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
