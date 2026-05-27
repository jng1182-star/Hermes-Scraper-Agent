import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from crewai.tools import BaseTool
try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS

# ── Supported platforms ───────────────────────────────────────────────────────
SUPPORTED_PLATFORMS = ["YouTube", "Facebook", "TikTok"]

# ── Platform profile URL patterns — used to validate discovered URLs ──────────
_PLATFORM_URL_PATTERNS = {
    "YouTube":  [r"youtube\.com/@", r"youtube\.com/channel/", r"youtube\.com/c/", r"youtube\.com/user/"],
    "Facebook": [r"facebook\.com/", r"fb\.com/"],
    "TikTok":   [r"tiktok\.com/@"],
}

# ── Industry → short category label for query disambiguation ──────────────────
_INDUSTRY_CATEGORY_LABEL = {
    "fmcg":         "consumer goods brand",
    "food_bev":     "food beverage brand",
    "beauty":       "beauty brand",
    "fashion":      "fashion brand",
    "retail":       "retail brand",
    "tech":         "technology brand",
    "telco":        "telecom brand",
    "finance":      "financial services brand",
    "insurance":    "insurance brand",
    "automotive":   "automotive brand",
    "travel":       "travel brand",
    "health":       "health brand",
    "entertainment":"entertainment brand",
    "gaming":       "gaming brand",
    "education":    "education brand",
    "real_estate":  "real estate brand",
}

# ── Profile discovery query templates per platform ────────────────────────────
# Goal: find the official brand page/channel URL and handle — nothing more.
_PROFILE_QUERIES = {
    "YouTube":  [
        'site:youtube.com "{brand}" official channel',
        '"{brand}" official YouTube channel {geo}',
        '{adv_brand} YouTube channel official {geo}',
    ],
    "Facebook": [
        'site:facebook.com "{brand}" official page',
        '"{brand}" official Facebook page {geo}',
        '{adv_brand} Facebook official page {geo}',
    ],
    "TikTok":   [
        'site:tiktok.com "@" "{brand}"',
        '"{brand}" official TikTok account {geo}',
        '{adv_brand} TikTok official {geo}',
    ],
}

# ── NSFW content filter ───────────────────────────────────────────────────────
_NSFW_TERMS = frozenset([
    "pornographic", "pornography", "porn", "xxx", "onlyfans", "adult content",
    "nude", "nudity", "naked", "nsfw", "erotic", "erotica", "sex tape",
    "cam girl", "camgirl", "escort", "prostitut", "strip club", "stripper",
    "masturbat", "orgasm", "genitalia", "genital", "fetish",
])

def _is_nsfw(snippet: dict) -> bool:
    text = (
        (snippet.get("content") or "") + " " +
        (snippet.get("title")   or "")
    ).lower()
    return any(term in text for term in _NSFW_TERMS)

def _filter_nsfw(snippets: list[dict], brand: str) -> list[dict]:
    clean, blocked = [], 0
    for s in snippets:
        if _is_nsfw(s):
            blocked += 1
        else:
            clean.append(s)
    if blocked:
        print(f"[BrandSafety] Filtered {blocked} NSFW snippet(s) for '{brand}'.", flush=True)
    return clean


# ── DuckDuckGo search ─────────────────────────────────────────────────────────

def _ddg_search(query: str, max_results: int = 5) -> list[dict]:
    try:
        with DDGS() as ddgs:
            raw = list(ddgs.text(query, max_results=max_results))
            return [
                {"url": r.get("href", ""), "title": r.get("title", ""), "content": r.get("body", "")}
                for r in raw if r.get("href")
            ]
    except Exception:
        return []

def _search(query: str, max_results: int = 5) -> list[dict]:
    return _ddg_search(query, max_results=max_results)


# ── URL validator — checks result URL matches expected platform domain ─────────

def _is_platform_url(url: str, platform: str) -> bool:
    patterns = _PLATFORM_URL_PATTERNS.get(platform, [])
    return any(re.search(p, url, re.IGNORECASE) for p in patterns)


# ── Parallel search ───────────────────────────────────────────────────────────

def _parallel_search(tasks: list[tuple[str, str]], max_workers: int = 4) -> list[dict]:
    snippets: list[dict] = []
    with ThreadPoolExecutor(max_workers=min(max_workers, max(1, len(tasks)))) as pool:
        futures = {pool.submit(_search, q, 5): src for q, src in tasks}
        for fut in as_completed(futures):
            src = futures[fut]
            try:
                for r in fut.result():
                    snippets.append({**r, "source_type": src})
            except Exception:
                pass
    return snippets


# ── Brand/platform extractor ──────────────────────────────────────────────────

def _extract_brands_and_platforms(query: str, params: dict) -> tuple[list[dict], list[str]]:
    raw_platforms = params.get("platforms") or []
    platforms = [p for p in raw_platforms if p in SUPPORTED_PLATFORMS]
    if not platforms:
        platforms = list(SUPPORTED_PLATFORMS)

    brand_pairs: list[dict] = []

    my_brands   = params.get("my_brands")   or []
    comp_brands = params.get("comp_brands") or []
    for entry in list(my_brands) + list(comp_brands):
        if isinstance(entry, dict) and entry.get("brand"):
            brand_pairs.append({
                "brand":      entry["brand"].strip(),
                "advertiser": (entry.get("advertiser") or "").strip(),
            })

    if not brand_pairs:
        advertisers = params.get("advertisers") or []
        competitors = params.get("competitors") or []
        if isinstance(advertisers, str):
            advertisers = [a.strip() for a in advertisers.split(",") if a.strip()]
        if isinstance(competitors, str):
            competitors = [c.strip() for c in competitors.split(",") if c.strip()]
        seen: set[str] = set()
        for b in list(advertisers) + list(competitors):
            if b and b not in seen:
                seen.add(b)
                brand_pairs.append({"brand": b, "advertiser": ""})

    if not brand_pairs:
        brand_part = re.split(r'\s+in\s+|\s+on\s+', query, flags=re.IGNORECASE)[0]
        brand_part = re.sub(r'\bvs\.?\b', ',', brand_part, flags=re.IGNORECASE)
        for b in brand_part.split(','):
            b = b.strip().strip('[]')
            if b and len(b) > 1:
                brand_pairs.append({"brand": b, "advertiser": ""})

    if not brand_pairs:
        first = query.split()[0] if query.split() else "brand"
        brand_pairs = [{"brand": first, "advertiser": ""}]

    return brand_pairs, platforms


# ── Main tool ─────────────────────────────────────────────────────────────────

class SocialSearchTool(BaseTool):
    name: str = "Social Media Profile Discovery"
    description: str = (
        "Discovers the official social media profile pages and channel handles for one or more "
        "brands across YouTube, Facebook, and TikTok. "
        "Returns profile URLs, handles, and page metadata so the Profile Scraper can collect "
        "posts within the user-specified date scope. Does NOT collect posts, ads, or engagement "
        "data — that is the Profile Scraper's job. "
        "Input: JSON with brands (advertisers + competitors), platforms, country, date_from, date_to."
    )

    def _run(self, query) -> str:
        # LLM sometimes passes a dict directly instead of a string — handle both.
        params: dict = {}
        if isinstance(query, dict):
            params = query
            query  = params.pop("query", "") or ""
        else:
            query = str(query or "")
            try:
                bracket = query.find('{')
                if bracket != -1:
                    params = json.loads(query[bracket:])
                    query  = query[:bracket].strip()
            except Exception:
                pass

        brand_pairs, platforms = _extract_brands_and_platforms(query, params)
        country   = params.get("country", "")
        industry  = params.get("industry", "")
        date_from = params.get("date_from", "")
        date_to   = params.get("date_to", "")

        markets: list[str] = params.get("markets") or ([country] if country else [""])
        _cat_label = _INDUSTRY_CATEGORY_LABEL.get(industry or "", "")

        results: list[dict] = []

        for pair in brand_pairs:
            brand      = pair["brand"]
            advertiser = pair.get("advertiser", "")
            adv_brand  = f"{advertiser} {brand}".strip() if advertiser else brand

            brand_entry: dict = {
                "brand":      brand,
                "advertiser": advertiser,
                "profiles":   [],   # discovered profile records per platform
            }

            for market in markets:
                geo = market if market else ""

                for platform in platforms:
                    templates = _PROFILE_QUERIES.get(platform, [])

                    # Build search tasks from all templates for this platform
                    tasks = [
                        (t.format(brand=brand, adv_brand=adv_brand, geo=geo), "profile_discovery")
                        for t in templates
                    ]

                    raw = _filter_nsfw(_parallel_search(tasks), brand)

                    # Prefer results whose URL is actually on the platform domain
                    on_platform = [r for r in raw if _is_platform_url(r.get("url", ""), platform)]
                    snippets    = on_platform if on_platform else raw

                    if not snippets:
                        # Retry: even simpler — just brand name + platform + official
                        retry_q = f"{adv_brand} official {platform} {geo}".strip()
                        snippets = _filter_nsfw(_search(retry_q, max_results=5), brand)
                        on_platform = [r for r in snippets if _is_platform_url(r.get("url", ""), platform)]
                        snippets = on_platform if on_platform else snippets

                    if snippets:
                        # Best candidate: first URL that matches platform domain, else first result
                        best = next(
                            (r for r in snippets if _is_platform_url(r.get("url", ""), platform)),
                            snippets[0],
                        )
                        brand_entry["profiles"].append({
                            "platform":        platform,
                            "market":          market,
                            "profile_url":     best.get("url", ""),
                            "page_title":      best.get("title", ""),
                            "snippet":         best.get("content", "")[:300],
                            "all_candidates":  [r.get("url", "") for r in snippets[:5]],
                            "industry_context": _cat_label,
                            "date_scope":      {"from": date_from, "to": date_to},
                        })
                        print(
                            f"[SocialSearch] {brand} / {platform} / {market}: "
                            f"found {best.get('url', '(no url)')}",
                            flush=True,
                        )
                    else:
                        print(
                            f"[SocialSearch] {brand} / {platform} / {market}: no profile found.",
                            flush=True,
                        )
                        brand_entry["profiles"].append({
                            "platform":    platform,
                            "market":      market,
                            "profile_url": "",
                            "page_title":  "",
                            "not_found":   True,
                            "date_scope":  {"from": date_from, "to": date_to},
                        })

            results.append(brand_entry)

        brands_flat = [p["brand"] for p in brand_pairs]
        return json.dumps({
            "query_meta": {
                "brands":    brands_flat,
                "platforms": platforms,
                "markets":   markets,
                "date_from": date_from,
                "date_to":   date_to,
                "note": (
                    "Profile URLs only. Post collection, engagement metrics, paid/organic "
                    "classification, and ER baseline are handled by the Profile Scraper "
                    "and Ad Library Collector agents using these handles."
                ),
            },
            "brand_results": results,
        }, indent=2)
