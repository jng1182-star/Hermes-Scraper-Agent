import json
import re
from datetime import datetime


# ── Base CPM benchmarks per market (USD per 1,000 impressions) ───────────────
# Source: eMarketer, Statista, Meta/TikTok/YouTube quarterly revenue disclosures,
# agency trading desk benchmarks 2024-25
COUNTRY_CPM = {
    "":               {"tiktok": 3.50, "instagram": 8.00, "youtube": 9.50,  "facebook": 7.50},
    "United States":  {"tiktok": 5.50, "instagram":12.00, "youtube":15.00,  "facebook":11.00},
    "United Kingdom": {"tiktok": 4.80, "instagram":10.50, "youtube":13.00,  "facebook": 9.50},
    "Canada":         {"tiktok": 4.50, "instagram":10.00, "youtube":12.50,  "facebook": 9.00},
    "Australia":      {"tiktok": 4.20, "instagram": 9.50, "youtube":11.50,  "facebook": 8.50},
    "Germany":        {"tiktok": 4.00, "instagram": 9.00, "youtube":11.00,  "facebook": 8.00},
    "France":         {"tiktok": 3.80, "instagram": 8.50, "youtube":10.50,  "facebook": 7.50},
    "Japan":          {"tiktok": 4.50, "instagram": 9.50, "youtube":12.00,  "facebook": 9.00},
    "South Korea":    {"tiktok": 3.50, "instagram": 8.00, "youtube":10.00,  "facebook": 7.00},
    "UAE":            {"tiktok": 4.00, "instagram": 9.00, "youtube":11.50,  "facebook": 8.50},
    "Saudi Arabia":   {"tiktok": 3.80, "instagram": 8.50, "youtube":11.00,  "facebook": 8.00},
    "Singapore":      {"tiktok": 3.80, "instagram": 8.50, "youtube":11.00,  "facebook": 8.00},
    "Malaysia":       {"tiktok": 1.80, "instagram": 3.50, "youtube": 4.50,  "facebook": 3.00},
    "Thailand":       {"tiktok": 1.50, "instagram": 3.00, "youtube": 4.00,  "facebook": 2.80},
    "Vietnam":        {"tiktok": 1.20, "instagram": 2.50, "youtube": 3.50,  "facebook": 2.20},
    "Indonesia":      {"tiktok": 1.00, "instagram": 2.20, "youtube": 3.00,  "facebook": 2.00},
    "Philippines":    {"tiktok": 1.00, "instagram": 2.00, "youtube": 3.00,  "facebook": 1.80},
    "India":          {"tiktok": 0.80, "instagram": 1.80, "youtube": 2.50,  "facebook": 1.50},
    "Brazil":         {"tiktok": 1.50, "instagram": 3.00, "youtube": 4.00,  "facebook": 2.80},
    "Mexico":         {"tiktok": 1.20, "instagram": 2.80, "youtube": 3.80,  "facebook": 2.50},
}

# ── Industry CPM multipliers (relative to 1.0 baseline) ─────────────────────
# Derived from category-level CPM premium data (DV360, Meta Ads Manager benchmarks)
INDUSTRY_CPM_MULT = {
    "":             1.00,  # General / Mixed
    "fmcg":         0.95,
    "food_bev":     0.90,
    "beauty":       1.10,
    "fashion":      1.05,
    "retail":       1.00,
    "tech":         1.30,
    "telco":        1.25,
    "finance":      2.20,
    "insurance":    2.00,
    "automotive":   1.80,
    "travel":       1.40,
    "health":       1.50,
    "entertainment":0.85,
    "gaming":       1.10,
    "education":    0.90,
    "real_estate":  1.60,
}

INDUSTRY_LABELS = {
    "": "General / Mixed", "fmcg": "FMCG / CPG", "food_bev": "Food & Beverage",
    "beauty": "Beauty & Personal Care", "fashion": "Fashion & Apparel",
    "retail": "Retail & E-commerce", "tech": "Technology & Electronics",
    "telco": "Telecoms", "finance": "Financial Services", "insurance": "Insurance",
    "automotive": "Automotive", "travel": "Travel & Hospitality",
    "health": "Health & Pharma", "entertainment": "Entertainment & Media",
    "gaming": "Gaming", "education": "Education", "real_estate": "Real Estate",
}

# 3-month rolling seasonal index (index 0=Jan … 11=Dec)
# Methodology: 3-month centred moving average applied to observed monthly spend curves
# (Source: Meta, TikTok, YouTube quarterly revenue disclosures; Nielsen ad spend indices)
SEASONAL_INDEX = [
    0.82,  # Jan — post-holiday drop
    0.85,  # Feb — Valentine's lift
    0.90,  # Mar — Q1 close
    0.93,  # Apr
    0.95,  # May
    0.97,  # Jun
    0.95,  # Jul — summer lull
    0.97,  # Aug
    1.02,  # Sep — Q3/Q4 ramp
    1.10,  # Oct — pre-holiday surge
    1.25,  # Nov — Singles Day / Black Friday peak
    1.40,  # Dec — Christmas / year-end peak
]

# ── Average view-through rates by platform (used for paid reach normalisation) ─
# "Of people who saw the ad, what fraction watched it?" — proxy for paid reach.
# Source: Meta/TikTok/YT ad benchmarks, agency trading desks 2024-25.
PLATFORM_AVG_VIEW_RATE = {
    "tiktok":    0.26,   # TikTok: ~20-30% average view-through
    "instagram": 0.30,   # IG Reels / Stories: ~25-35%
    "youtube":   0.32,   # YT True-View: ~25-40%
    "facebook":  0.22,   # FB Feed video: ~18-25%
    "default":   0.25,
}

# ── Industry × Platform ER benchmarks (3-month rolling, %) ──────────────────
# View-based (TikTok/YT): interactions / views × 100
# Follower-based (IG/FB):  interactions / followers × 100
# Source: Socialinsider, Sprout Social, Rival IQ industry reports 2024-25
# Each entry: { platform: { industry: benchmark_pct } }
INDUSTRY_ER_BENCHMARKS = {
    "tiktok": {
        "":             5.0,  "fmcg":        5.5,  "food_bev":   6.0,
        "beauty":       7.0,  "fashion":     6.5,  "retail":     5.5,
        "tech":         4.5,  "telco":       4.0,  "finance":    3.5,
        "insurance":    3.0,  "automotive":  4.5,  "travel":     6.5,
        "health":       5.5,  "entertainment":8.0, "gaming":     7.5,
        "education":    5.0,  "real_estate": 3.5,
    },
    "instagram": {
        "":             1.5,  "fmcg":        1.8,  "food_bev":   2.2,
        "beauty":       2.5,  "fashion":     2.0,  "retail":     1.6,
        "tech":         1.2,  "telco":       1.0,  "finance":    0.9,
        "insurance":    0.8,  "automotive":  1.3,  "travel":     2.3,
        "health":       1.8,  "entertainment":2.8, "gaming":     2.5,
        "education":    1.5,  "real_estate": 1.1,
    },
    "facebook": {
        "":             0.8,  "fmcg":        0.9,  "food_bev":   1.0,
        "beauty":       1.1,  "fashion":     0.9,  "retail":     0.8,
        "tech":         0.6,  "telco":       0.5,  "finance":    0.5,
        "insurance":    0.4,  "automotive":  0.7,  "travel":     1.0,
        "health":       0.8,  "entertainment":1.2, "gaming":     1.1,
        "education":    0.7,  "real_estate": 0.5,
    },
    "youtube": {
        "":             2.0,  "fmcg":        2.2,  "food_bev":   2.5,
        "beauty":       3.0,  "fashion":     2.5,  "retail":     2.0,
        "tech":         1.8,  "telco":       1.5,  "finance":    1.5,
        "insurance":    1.2,  "automotive":  2.0,  "travel":     2.8,
        "health":       2.2,  "entertainment":3.5, "gaming":     3.0,
        "education":    2.0,  "real_estate": 1.3,
    },
}

def _er_benchmark(platform_key: str, industry: str) -> float:
    """Return 3-month rolling ER benchmark for a platform+industry pair."""
    plat_map = INDUSTRY_ER_BENCHMARKS.get(platform_key, INDUSTRY_ER_BENCHMARKS.get("default", {}))
    if not plat_map:
        # Fallback flat benchmarks
        flat = {"tiktok":5.0,"instagram":1.5,"facebook":0.8,"youtube":2.0}
        return flat.get(platform_key, 2.0)
    return plat_map.get(industry or "", plat_map.get("", 2.0))


def _platform_key(platform_str: str) -> str:
    return (platform_str or "").lower().split("/")[0].strip()


def _seasonal_index() -> float:
    return SEASONAL_INDEX[datetime.now().month - 1]


def _effective_cpm(platform: str, country: str, industry: str) -> float:
    base_map = COUNTRY_CPM.get(country, COUNTRY_CPM[""])
    base  = base_map.get(_platform_key(platform), 7.00)
    imult = INDUSTRY_CPM_MULT.get(industry or "", 1.00)
    smult = _seasonal_index()
    return round(base * imult * smult, 2)


def _cpm_note(country: str, industry: str, override: float = None) -> str:
    month = datetime.now().strftime("%B")
    si    = _seasonal_index()
    if override and override > 0:
        return f"User-set CPM override: ${override}/1K impressions (market/industry/seasonal adjustments bypassed)."
    ilabel = INDUSTRY_LABELS.get(industry or "", "General")
    imult  = INDUSTRY_CPM_MULT.get(industry or "", 1.00)
    return (
        f"Auto CPM: base market CPM ({country or 'Global'}) × industry multiplier "
        f"({ilabel}, ×{imult:.2f}) × seasonal index ({month}: ×{si:.2f}). "
        f"Sources: eMarketer, Statista, Meta/TikTok/YouTube quarterly revenue disclosures, "
        f"agency trading desk benchmarks 2024-25."
    )


class ApprovalGate:
    def __init__(self, cpm_rate: float = None, post_type: str = "both",
                 country: str = "", industry: str = ""):
        self.cpm_rate_override = cpm_rate   # None / 0 = use auto derivation
        self.post_type  = post_type  or "both"
        self.country    = country    or ""
        self.industry   = industry   or ""

    def _get_cpm(self, platform: str) -> float:
        if self.cpm_rate_override and self.cpm_rate_override > 0:
            return float(self.cpm_rate_override)
        return _effective_cpm(platform, self.country, self.industry)

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
            benchmark_er = _er_benchmark(plat_key, self.industry)
            er_vs_benchmark = round(eng_rate - benchmark_er, 2)

            # ── Estimated Ad Spend ────────────────────────────────────────
            # Paid reach = views / avg_view_rate  (normalises raw views to
            # estimated unique paid impressions based on platform view-through rates)
            # Spend = (paid_reach / 1,000) × Platform CPM
            # Organic: amplification value = interactions × avg engagement cost
            # Both: 60% paid / 40% organic split assumption
            avg_vr = PLATFORM_AVG_VIEW_RATE.get(plat_key, PLATFORM_AVG_VIEW_RATE["default"])
            AVG_ENGAGEMENT_COST = 0.75  # USD per interaction (industry blended avg)

            if post_type_c == "paid":
                paid_reach = round(max(views, 0) / avg_vr) if avg_vr > 0 else max(views, 0)
                spend = round((paid_reach / 1000) * cpm, 2)
                spend_note = (
                    f"Paid: ({max(views,0):,} views / {avg_vr:.0%} avg view rate) = "
                    f"{paid_reach:,} paid impressions / 1,000 × ${cpm} CPM"
                )
            elif post_type_c == "organic":
                spend = round(interactions * AVG_ENGAGEMENT_COST, 2)
                spend_note = f"Organic value: {interactions:,} interactions × ${AVG_ENGAGEMENT_COST} avg engagement cost"
            else:
                # Both: derive paid reach for 60% impression share, organic for rest
                paid_views = round(views * 0.60)
                paid_reach = round(paid_views / avg_vr) if avg_vr > 0 else paid_views
                spend_paid_part  = round((paid_reach / 1000) * cpm, 2)
                org_value_part   = round(interactions * 0.40 * AVG_ENGAGEMENT_COST, 2)
                spend = round(spend_paid_part + org_value_part, 2)
                spend_note = (
                    f"Mixed: ({paid_views:,} paid views / {avg_vr:.0%} view rate) = "
                    f"{paid_reach:,} impressions × ${cpm} CPM + "
                    f"{interactions:,} interactions × ${AVG_ENGAGEMENT_COST} × 40%"
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
        si = _seasonal_index()
        imult = INDUSTRY_CPM_MULT.get(self.industry, 1.00)
        ilabel = INDUSTRY_LABELS.get(self.industry, "General")
        # Build per-platform effective CPM table for transparency
        plat_cpm_table = {
            p: _effective_cpm(p, self.country, self.industry) if not use_override
               else float(self.cpm_rate_override)
            for p in ("TikTok", "Instagram", "YouTube", "Facebook")
        }
        data["assumptions"] = {
            "post_type":            self.post_type,
            "market":               self.country or "Global",
            "industry":             ilabel,
            "cpm_rate_usd":         self.cpm_rate_override if use_override else "auto (market×industry×seasonal)",
            "cpm_note":             _cpm_note(self.country, self.industry, self.cpm_rate_override),
            "cpm_seasonal_index":   si,
            "cpm_industry_mult":    imult,
            "cpm_by_platform":      plat_cpm_table,
            "spend_formula_paid":   "Paid spend = (Views / Avg Platform View Rate) / 1,000 × Platform CPM",
            "spend_formula_organic":"Organic value = Interactions × $0.75 avg engagement cost",
            "spend_formula_both":   "Mixed = (Views × 60% / Avg View Rate / 1,000 × CPM) + (Interactions × 40% × $0.75)",
            "avg_view_rates":       {k: f"{v:.0%}" for k, v in PLATFORM_AVG_VIEW_RATE.items()},
            "engagement_rate_formula": (
                "ER = (Interactions / Views) × 100  [TikTok, YouTube — view-based]\n"
                "ER = (Interactions / Followers) × 100  [Instagram, Facebook — follower-based]\n"
                "Interactions = Likes + Comments + Shares + Saves"
            ),
            "benchmark_note": (
                f"ER benchmarks: 3-month rolling industry standard ({ilabel}). "
                "Source: Socialinsider, Sprout Social, Rival IQ industry reports 2024-25."
            ),
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
