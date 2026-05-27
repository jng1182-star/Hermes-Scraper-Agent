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
SUPPORTED_PLATFORMS = ["YouTube", "Facebook"]

# ── Industry → short category label for query disambiguation ─────────────────
# Appended to brand name so "Close Up" → "Close Up toothpaste brand"
# Prevents ambiguous brand names from pulling NSFW or off-topic results.
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

# ── NSFW content filter ───────────────────────────────────────────────────────
# Block snippets that contain NSFW signals unrelated to brand advertising.
# Conservative list — only clear NSFW terms, not ambiguous marketing language.
_NSFW_TERMS = frozenset([
    "pornographic", "pornography", "porn", "xxx", "onlyfans", "adult content",
    "nude", "nudity", "naked", "nsfw", "erotic", "erotica", "sex tape",
    "cam girl", "camgirl", "escort", "prostitut", "strip club", "stripper",
    "masturbat", "orgasm", "genitalia", "genital", "fetish",
])

def _is_nsfw(snippet: dict) -> bool:
    """Return True if snippet content contains NSFW signals."""
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

# ── Localized "Sponsored" / ad label equivalents by market ───────────────────
# Used to detect geo-targeted paid content labels that platforms render in local languages.
# Sources: TikTok UI localization docs; Meta Ads Manager locale reference; Google ATC.
LOCALIZED_PAID_LABELS = {
    "TH": "โฆษณา",           # Thai: advertisement / ad
    "VN": "Được tài trợ",   # Vietnamese: sponsored
    "ID": "Bersponsor",     # Indonesian: sponsored
    "MY": "Ditaja",         # Malay: sponsored
    "PH": "Sponsored",      # Filipino: English dominant
    "SG": "Sponsored",      # Singapore: English dominant
}

# Market code lookup by full country name
_COUNTRY_TO_MARKET_CODE = {
    "Thailand": "TH", "Vietnam": "VN", "Indonesia": "ID",
    "Malaysia": "MY", "Philippines": "PH", "Singapore": "SG",
}

# ── Single best-signal paid query per platform ───────────────────────────────
_PAID_QUERIES = {
    "YouTube":  "{brand} YouTube paid ad TrueView bumper pre-roll advertising spend 2025{geo}",
    "Facebook": "{brand} Facebook paid ad boosted post {paid_label} sponsored Meta Ads Library campaign 2025{geo}",
}

# ── Single best-signal organic query per platform ────────────────────────────
_ORGANIC_QUERIES = {
    "YouTube":  "{brand} YouTube organic views subscribers watch-time engagement 2025{geo}",
    "Facebook": "{brand} Facebook organic page reach likes shares reactions followers 2025{geo}",
}

# ── Category SoV fallback query templates ────────────────────────────────────
# Fired when primary competitor search returns 0 results for a brand-platform pair.
# Returns top-10 category ads to build a regional category SoV matrix.
_CATEGORY_SOV_QUERIES = {
    "YouTube":  "top 10 {industry} brand YouTube ads TrueView pre-roll 2025{geo}",
    "Facebook": "top 10 {industry} brand Facebook ads boosted sponsored 2025{geo}",
}


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


# ── Unified search (DuckDuckGo only) ────────────────────────────────────────

def _search(query: str, max_results: int = 3) -> list[dict]:
    return _ddg_search(query, max_results=max_results)


# ── Query builder helpers ──────────────────────────────────────────────────────

def _extract_brands_and_platforms(query: str, params: dict = None) -> tuple[list[dict], list[str], str]:
    """
    Returns (brand_pairs, platforms, post_type).
    brand_pairs: list of {"brand": str, "advertiser": str} dicts.
    Advertiser is included in search queries (e.g. "Unilever Axe Philippines Facebook")
    to avoid ambiguous brand names being confused with unrelated entities.
    Industry context is NOT added to search queries — it is passed separately for
    profile validation (agents use it to confirm the correct brand profile was found).
    """
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

    brand_pairs: list[dict] = []

    # Prefer structured brand+advertiser pairs from new row-based form
    my_brands  = params.get("my_brands")   or []   # [{"brand":..,"advertiser":..}]
    comp_brands = params.get("comp_brands") or []   # [{"brand":..,"advertiser":..}]
    for entry in list(my_brands) + list(comp_brands):
        if isinstance(entry, dict) and entry.get("brand"):
            brand_pairs.append({
                "brand":      entry["brand"].strip(),
                "advertiser": (entry.get("advertiser") or "").strip(),
            })

    # Fallback: flat advertisers + competitors lists (backward compat)
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

    # Last resort: parse from query string
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

    return brand_pairs, platforms, post_type


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
    max_workers capped at 5 to keep search concurrency modest.
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
        "across YouTube and Facebook. "
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

        brand_pairs, platforms, post_type = _extract_brands_and_platforms(query, params)
        country   = params.get("country", "")
        industry  = params.get("industry", "")
        date_hint = params.get("date_range", "2025")

        # Multi-market: each market multiplies the brand×platform search matrix.
        # e.g. brands=[Axe/Unilever], platforms=[Facebook, YouTube, TikTok], markets=[PH, SG]
        # → "Unilever Axe Facebook Philippines", "Unilever Axe YouTube Philippines", ...
        markets: list[str] = params.get("markets") or ([country] if country else [""])

        _cat_label = _INDUSTRY_CATEGORY_LABEL.get(industry or "", "brand")

        # Industry is NOT added to search queries — used only as profile validation context
        _industry_guard = (
            f" INDUSTRY VALIDATION: Confirm the brand profile belongs to the '{industry}' "
            f"industry before accepting any data. Discard profiles that are clearly unrelated "
            f"(e.g. if industry is 'beauty', reject a profile for a hardware or food brand "
            f"with the same name)."
        ) if industry else ""

        _paid_tool = None
        if post_type in ("paid", "both"):
            try:
                from tools.paid_adlib_tool import PaidAdLibTool
                _paid_tool = PaidAdLibTool()
            except Exception:
                pass

        results: list[dict] = []

        for pair in brand_pairs:
            brand      = pair["brand"]
            advertiser = pair.get("advertiser", "")

            # Search query: "Unilever Axe consumer goods brand" (advertiser + brand + category)
            # Advertiser disambiguates the brand; category further narrows search intent.
            # Industry is intentionally excluded from query — only used for profile validation.
            brand_tag = " ".join(filter(None, [advertiser, brand, _cat_label]))

            brand_entry: dict = {
                "brand":         brand,
                "advertiser":    advertiser,
                "platform_data": [],
                "posts_found":   0,
                "industry_note": _industry_guard,
            }

            # ── Outer loop: market — each market is a separate geo context ──
            for market in markets:
                geo = f" {market}" if market else ""

                # Localized paid label resolved per-market
                market_code = _COUNTRY_TO_MARKET_CODE.get(market, "")
                paid_label  = LOCALIZED_PAID_LABELS.get(market_code, "Sponsored")

                # ── PAID: ad library scraper — once per brand × market ────────
                if post_type in ("paid", "both") and _paid_tool is not None:
                    try:
                        paid_raw  = _paid_tool._run(json.dumps({
                            "brand":     brand,
                            "country":   market,
                            "platforms": platforms,
                            "markets":   [market],
                        }))
                        paid_data = json.loads(paid_raw)
                        for paid_plat in paid_data.get("platform_data", []):
                            paid_plat.setdefault("market", market)
                            matched = next(
                                (p for p in brand_entry["platform_data"]
                                 if p["platform"] == paid_plat["platform"]
                                 and p.get("market") == market),
                                None,
                            )
                            if matched:
                                matched["raw_results"].extend(paid_plat.get("raw_results", []))
                            else:
                                brand_entry["platform_data"].append(paid_plat)
                            brand_entry["posts_found"] += len(paid_plat.get("raw_results", []))
                    except Exception as e:
                        print(f"[SocialSearch] PaidAdLibTool failed for '{brand}' in {market}: {e}", flush=True)

                # ── Inner loop: platform — each platform has its own query template ──
                # Platform is embedded in the template (not a {platform} var) because
                # each platform needs a distinct query structure.
                for platform in platforms:
                    search_tasks: list[tuple[str, str]] = []

                    if post_type in ("paid", "both"):
                        tmpl = _PAID_QUERIES.get(platform)
                        if tmpl:
                            search_tasks.append((
                                tmpl.format(brand=brand_tag, geo=geo, paid_label=paid_label),
                                "paid",
                            ))

                    if post_type in ("organic", "both"):
                        tmpl = _ORGANIC_QUERIES.get(platform)
                        if tmpl:
                            search_tasks.append((
                                tmpl.format(brand=brand_tag, geo=geo),
                                "organic",
                            ))

                    # Competitive benchmark query — always fired
                    search_tasks.append((
                        f"{brand_tag} vs competitors {platform} engagement share of voice{geo} {date_hint}",
                        "both",
                    ))

                    platform_snippets = _filter_nsfw(_parallel_search(search_tasks), brand)

                    # Iterative fallback: if full query returns nothing, retry with
                    # progressively simpler queries before giving up on this brand×platform.
                    # advertiser (parent company) is kept in early retries — it disambiguates
                    # brand names (e.g. "Unilever Closeup" vs unrelated "Closeup" entities).
                    _adv_prefix = f"{advertiser} " if advertiser else ""

                    if not platform_snippets:
                        # Retry 1: advertiser + brand + platform (drop category label only)
                        simple_tasks: list[tuple[str, str]] = []
                        if post_type in ("paid", "both"):
                            simple_tasks.append((
                                f"{_adv_prefix}{brand} {platform} paid ad sponsored 2025{geo}", "paid"
                            ))
                        if post_type in ("organic", "both"):
                            simple_tasks.append((
                                f"{_adv_prefix}{brand} {platform} official page followers engagement 2025{geo}", "organic"
                            ))
                        platform_snippets = _filter_nsfw(_parallel_search(simple_tasks), brand)
                        if platform_snippets:
                            print(f"[SocialSearch] Retry 1 (adv+brand+platform) succeeded for '{brand}' on {platform}{geo}.", flush=True)

                    if not platform_snippets:
                        # Retry 2: brand + platform only (drop advertiser — try brand alone)
                        simple_tasks2: list[tuple[str, str]] = []
                        if post_type in ("paid", "both"):
                            simple_tasks2.append((
                                f"{brand} {platform} paid ad sponsored 2025{geo}", "paid"
                            ))
                        if post_type in ("organic", "both"):
                            simple_tasks2.append((
                                f"{brand} {platform} official page engagement 2025{geo}", "organic"
                            ))
                        platform_snippets = _filter_nsfw(_parallel_search(simple_tasks2), brand)
                        if platform_snippets:
                            print(f"[SocialSearch] Retry 2 (brand+platform) succeeded for '{brand}' on {platform}{geo}.", flush=True)

                    if not platform_snippets:
                        # Retry 3: bare — widest possible net
                        bare_tasks = [(f"{_adv_prefix}{brand} {platform} social media 2025{geo}", "both")]
                        platform_snippets = _filter_nsfw(_parallel_search(bare_tasks), brand)
                        if platform_snippets:
                            print(f"[SocialSearch] Retry 3 (bare) succeeded for '{brand}' on {platform}{geo}.", flush=True)

                    for s in platform_snippets:
                        s.setdefault("market", market)

                    if platform_snippets:
                        matched = next(
                            (p for p in brand_entry["platform_data"]
                             if p["platform"] == platform and p.get("market") == market),
                            None,
                        )
                        if matched:
                            matched["raw_results"].extend(platform_snippets)
                        else:
                            brand_entry["platform_data"].append({
                                "platform":    platform,
                                "market":      market,
                                "raw_results": platform_snippets,
                            })
                        brand_entry["posts_found"] += len(platform_snippets)
                    elif post_type in ("paid", "both"):
                        # Category SoV fallback — only fires after all retries exhausted
                        print(
                            f"[SocialSearch] No results for '{brand}' on {platform}{geo} after retries — "
                            "running category SoV fallback.",
                            flush=True,
                        )
                        industry_label = industry or "brand"
                        tmpl = _CATEGORY_SOV_QUERIES.get(platform)
                        if tmpl:
                            cat_q = tmpl.format(industry=industry_label, geo=geo, paid_label=paid_label)
                            cat_snippets = _parallel_search([(cat_q, "paid_category_fallback")])
                            for s in cat_snippets:
                                s.setdefault("market", market)
                            if cat_snippets:
                                brand_entry["platform_data"].append({
                                    "platform":          platform,
                                    "market":            market,
                                    "raw_results":       cat_snippets,
                                    "category_fallback": True,
                                    "fallback_note": (
                                        f"No '{brand}' ads found on {platform}{geo} after retries. "
                                        f"Top-10 {industry_label} category ads returned instead."
                                    ),
                                })
                                brand_entry["posts_found"] += len(cat_snippets)

                # General brand social health — 1 query per brand × market
                gen_q = (
                    f"{brand_tag} social media presence overview {' '.join(platforms)} "
                    f"followers engagement rate benchmark{geo} {date_hint}"
                )
                gen = _filter_nsfw(_search(gen_q, max_results=3), brand)
                if gen:
                    for s in gen:
                        s.setdefault("market", market)
                    brand_entry.setdefault("general_presence", []).extend(
                        _enrich_snippets(gen, "organic")
                    )

            results.append(brand_entry)

        # ── Fallback to golden dataset ────────────────────────────────────
        brands_flat = [p["brand"] for p in brand_pairs]
        if not any(b["platform_data"] for b in results):
            golden_path = os.path.join(os.path.dirname(__file__), "../data/golden_dataset.json")
            if os.path.exists(golden_path):
                with open(golden_path, "r") as f:
                    golden_data = json.load(f)
                return json.dumps({
                    "fallback": True,
                    "fallback_reason": "All live searches returned zero results — serving cached seed data.",
                    "query_meta": {
                        "brands": brands_flat, "platforms": platforms,
                        "post_type": post_type, "markets": markets,
                    },
                    "results": golden_data,
                })
            return json.dumps({
                "fallback": True,
                "fallback_reason": "All live searches returned zero results and no seed data is available.",
                "results": [],
            })

        total_snippets = sum(b["posts_found"] for b in results)
        return json.dumps({
            "query_meta": {
                "brands":      brands_flat,
                "brand_pairs": brand_pairs,
                "platforms":   platforms,
                "post_type":   post_type,
                "markets":     markets,
                "date_range":  date_hint,
                "total_snippets_retrieved": total_snippets,
            },
            "brand_results": results,
        }, indent=2)
