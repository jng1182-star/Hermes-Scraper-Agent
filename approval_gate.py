import json
import re


# ── Industry CPM benchmarks (USD) — used when user doesn't override ──────────
# Source: industry consensus 2024-2025 (Sprout Social, Hootsuite, Nielsen benchmarks)
PLATFORM_CPM_DEFAULTS = {
    "tiktok":    3.50,   # TikTok In-Feed avg CPM
    "instagram": 8.00,   # Instagram Feed/Story avg CPM
    "facebook":  7.50,   # Facebook avg CPM (paid)
    "youtube":   9.50,   # YouTube TrueView avg CPM
    "default":   7.00,   # blended cross-platform estimate
}

# ── Industry engagement rate benchmarks by platform ──────────────────────────
PLATFORM_ER_BENCHMARKS = {
    "tiktok":    5.0,    # % — TikTok median ER (views-based)
    "instagram": 1.5,    # % — Instagram median ER (followers-based)
    "facebook":  0.8,    # % — Facebook median ER (followers-based)
    "youtube":   2.0,    # % — YouTube median ER (views-based)
    "default":   2.0,
}


def _platform_key(platform_str: str) -> str:
    return (platform_str or "").lower().split("/")[0].strip()


class ApprovalGate:
    def __init__(self, cpm_rate: float = None, post_type: str = "both"):
        self.cpm_rate_override = cpm_rate  # None = use per-platform defaults
        self.post_type = post_type or "both"

    def _get_cpm(self, platform: str) -> float:
        if self.cpm_rate_override and self.cpm_rate_override > 0:
            return float(self.cpm_rate_override)
        key = _platform_key(platform)
        return PLATFORM_CPM_DEFAULTS.get(key, PLATFORM_CPM_DEFAULTS["default"])

    def _extract_json(self, raw: str) -> str:
        raw = re.sub(r'```(?:json)?\s*', '', raw)
        raw = re.sub(r'```', '', raw)
        raw = re.sub(r',\s*([\]}])', r'\1', raw)
        return raw.strip()

    def _parse(self, cleaned: str) -> dict:
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass
        start = cleaned.find('{')
        end   = cleaned.rfind('}') + 1
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start:end])
            except json.JSONDecodeError:
                pass
        start = cleaned.find('[')
        end   = cleaned.rfind(']') + 1
        if start != -1 and end > start:
            try:
                lst = json.loads(cleaned[start:end])
                return {"competitors": lst}
            except json.JSONDecodeError:
                pass
        raise ValueError(f"Could not parse JSON from agent output: {cleaned[:200]}")

    def process_final_report(self, raw_output: str) -> str:
        cleaned = self._extract_json(raw_output)
        data    = self._parse(cleaned)

        if "competitors" not in data:
            if isinstance(data, list):
                data = {"competitors": data}
            else:
                for v in data.values():
                    if isinstance(v, list):
                        data = {"competitors": v}
                        break
                else:
                    data = {"competitors": []}

        total_interactions = 0
        total_impressions  = 0
        total_spend_paid   = 0.0
        total_spend_org    = 0.0
        brand_calcs        = []

        for comp in data["competitors"]:
            m        = comp.get("metrics", {})
            likes    = int(m.get("likes",    0) or 0)
            comments = int(m.get("comments", 0) or 0)
            shares   = int(m.get("shares",   0) or 0)
            views    = int(m.get("views",    0) or 0)
            saves    = int(m.get("saves",    0) or 0)
            followers= int(m.get("followers",0) or 0)

            # Interactions = sum of all measurable engagement actions
            interactions = likes + comments + shares + saves

            m.update(
                likes=likes, comments=comments, shares=shares,
                views=views, saves=saves, followers=followers,
                interactions=interactions,
            )
            comp["metrics"] = m

            platform    = comp.get("platform", "")
            plat_key    = _platform_key(platform)
            cpm         = self._get_cpm(platform)
            post_type_c = comp.get("post_type", self.post_type) or self.post_type

            # ── Engagement Rate (industry-correct) ────────────────────────
            # View-based platforms (TikTok, YouTube): ER = interactions / views × 100
            # Follower-based platforms (Instagram, Facebook): ER = interactions / followers × 100
            # Fallback: interactions / max(views, 1) × 100
            view_based = plat_key in ("tiktok", "youtube")
            if view_based and views > 0:
                er_denominator = views
                er_formula_label = "views"
            elif not view_based and followers > 0:
                er_denominator = followers
                er_formula_label = "followers"
            elif views > 0:
                er_denominator = views
                er_formula_label = "views"
            else:
                er_denominator = 1
                er_formula_label = "impressions (est.)"

            eng_rate = round((interactions / er_denominator) * 100, 4)
            benchmark_er = PLATFORM_ER_BENCHMARKS.get(plat_key, PLATFORM_ER_BENCHMARKS["default"])
            er_vs_benchmark = round(eng_rate - benchmark_er, 2)

            # ── Estimated Ad Spend (CPM × impressions/views / 1000) ───────
            # CPM = cost per 1,000 impressions. Views/reach is the impression proxy.
            # For PAID posts: apply CPM to views (impressions proxy)
            # For ORGANIC posts: no direct spend; estimate amplification value
            #   = interactions × avg engagement cost (industry: ~$0.50–1.50 per engagement)
            # For BOTH: split 60% paid / 40% organic assumption if no explicit split
            AVG_ENGAGEMENT_COST = 0.75  # USD per interaction (industry blended avg)

            if post_type_c == "paid":
                spend = round((max(views, 0) / 1000) * cpm, 2)
                spend_note = f"Paid: {max(views,0):,} impressions / 1,000 × ${cpm} CPM"
            elif post_type_c == "organic":
                spend = round(interactions * AVG_ENGAGEMENT_COST, 2)
                spend_note = f"Organic value: {interactions:,} interactions × ${AVG_ENGAGEMENT_COST} avg engagement cost"
            else:
                # Both: CPM-based spend on paid impressions portion
                paid_impressions = round(views * 0.60)
                spend_paid_part  = round((paid_impressions / 1000) * cpm, 2)
                org_value_part   = round(interactions * 0.40 * AVG_ENGAGEMENT_COST, 2)
                spend = round(spend_paid_part + org_value_part, 2)
                spend_note = (
                    f"Mixed: {paid_impressions:,} est. paid impressions × ${cpm} CPM + "
                    f"{interactions:,} interactions × ${ AVG_ENGAGEMENT_COST} × 40%"
                )

            comp["estimated_spend_usd"]  = spend
            comp["engagement_rate"]      = eng_rate
            comp["er_vs_benchmark"]      = er_vs_benchmark
            comp["benchmark_er_pct"]     = benchmark_er
            comp["cpm_used"]             = cpm

            total_interactions += interactions
            total_impressions  += views
            if post_type_c == "paid":
                total_spend_paid += spend
            elif post_type_c == "organic":
                total_spend_org  += spend
            else:
                total_spend_paid += spend * 0.6
                total_spend_org  += spend * 0.4

            # ── Defaults ─────────────────────────────────────────────────
            comp.setdefault("name",           comp.get("handle", "Unknown"))
            comp.setdefault("handle",         "")
            comp.setdefault("platform",       "Social Media")
            comp.setdefault("sentiment",      "Neutral")
            comp.setdefault("top_posts",      [])
            comp.setdefault("hashtags",       [])
            comp.setdefault("content_themes", [])
            comp.setdefault("post_type",      self.post_type)

            for list_field in ("hashtags", "content_themes"):
                val = comp[list_field]
                if not isinstance(val, list):
                    comp[list_field] = [str(val)] if val else []
                else:
                    comp[list_field] = [str(x) for x in val if x]

            # ── Sanitise top_posts ────────────────────────────────────────
            raw_posts = comp.get("top_posts", [])
            if not isinstance(raw_posts, list):
                raw_posts = [raw_posts] if raw_posts else []
            clean_posts = []
            for p in raw_posts:
                if isinstance(p, dict):
                    clean_posts.append({
                        "caption":   str(p.get("caption") or p.get("text") or p.get("description") or ""),
                        "url":       str(p["url"]) if p.get("url") else None,
                        "post_type": str(p.get("post_type") or self.post_type),
                        "likes":     int(p.get("likes") or 0),
                        "views":     int(p.get("views") or 0),
                    })
                elif isinstance(p, str) and p:
                    import re as _re
                    url_match = _re.search(r'https?://\S+', p)
                    if url_match:
                        url = url_match.group(0).rstrip('.,;)')
                        caption = p.replace(url, '').strip(' —:-')
                        clean_posts.append({"caption": caption or p, "url": url, "post_type": self.post_type, "likes": 0, "views": 0})
                    else:
                        clean_posts.append({"caption": p, "url": None, "post_type": self.post_type, "likes": 0, "views": 0})
            comp["top_posts"] = clean_posts

            brand_calcs.append({
                "brand":              comp.get("name", "?"),
                "platform":           platform,
                "post_type":          post_type_c,
                "likes":              likes,
                "comments":           comments,
                "shares":             shares,
                "saves":              saves,
                "views":              views,
                "followers":          followers,
                "interactions":       interactions,
                "er_denominator":     er_denominator,
                "er_denominator_label": er_formula_label,
                "engagement_rate":    eng_rate,
                "benchmark_er":       benchmark_er,
                "er_vs_benchmark":    er_vs_benchmark,
                "cpm_used":           cpm,
                "spend_usd":          spend,
                "spend_note":         spend_note,
                "er_formula":         f"({interactions:,} interactions / {er_denominator:,} {er_formula_label}) × 100",
                "spend_formula":      spend_note,
            })

        # ── Assumptions block ─────────────────────────────────────────────
        use_override = bool(self.cpm_rate_override and self.cpm_rate_override > 0)
        data["assumptions"] = {
            "post_type":            self.post_type,
            "cpm_rate_usd":         self.cpm_rate_override or "per-platform defaults",
            "cpm_note":             (
                f"User-set CPM: ${self.cpm_rate_override} applied uniformly." if use_override
                else f"Platform defaults used: TikTok=${PLATFORM_CPM_DEFAULTS['tiktok']}, "
                     f"Instagram=${PLATFORM_CPM_DEFAULTS['instagram']}, "
                     f"YouTube=${PLATFORM_CPM_DEFAULTS['youtube']}, "
                     f"Facebook=${PLATFORM_CPM_DEFAULTS['facebook']}."
            ),
            "spend_formula_paid":   "Paid spend = (Views / 1,000) × Platform CPM",
            "spend_formula_organic":"Organic value = Interactions × $0.75 avg engagement cost",
            "spend_formula_both":   "Mixed = (Views × 60% / 1,000 × CPM) + (Interactions × 40% × $0.75)",
            "engagement_rate_formula": (
                "ER = (Interactions / Views) × 100  [TikTok, YouTube — view-based]\n"
                "ER = (Interactions / Followers) × 100  [Instagram, Facebook — follower-based]\n"
                "Interactions = Likes + Comments + Shares + Saves"
            ),
            "benchmark_note":       "Industry ER benchmarks: TikTok 5%, Instagram 1.5%, Facebook 0.8%, YouTube 2%",
            "data_source":          "Web search results via Tavily/DuckDuckGo — estimates only, not official platform data.",
            "total_interactions":   total_interactions,
            "total_impressions":    total_impressions,
            "total_spend_paid_usd": round(total_spend_paid, 2),
            "total_spend_org_usd":  round(total_spend_org, 2),
            "total_spend_usd":      round(total_spend_paid + total_spend_org, 2),
            "brand_breakdowns":     brand_calcs,
        }

        def scrub(obj):
            if isinstance(obj, dict):
                return {k: scrub(v) for k, v in obj.items()
                        if "sk-" not in str(v) and "API_KEY" not in str(v)}
            if isinstance(obj, list):
                return [scrub(i) for i in obj]
            return obj

        return json.dumps(scrub(data), indent=2)
