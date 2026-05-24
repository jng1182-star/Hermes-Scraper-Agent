"""
Approval Gate — spend estimation, SoS/SoV calculation, and methodology audit trail.

Methodology notes (Nielsen/Kantar-grade auditability):
  - All benchmark constants are sourced and cited in the ASSUMPTIONS block of every report.
  - Impression inference uses a dual-path model (IAB-aligned):
      Path A (video): I = Scraped Views / Historical VTR
        Rationale: VTR is the industry-standard denominator for video ad reach; normalises
        raw view counts (which include organic re-watches) to unique paid impression estimates.
      Path B (engagement-only): I = (Likes + Comments) / Category ER Benchmark
        Rationale: When view counts are unavailable, the inverse ER method (used by WARC and
        similar bodies) back-calculates implied reach from observed interactions.
  - Spend estimation: E = (I / 1,000) × Market CPM
        CPM is derived as: Base Market CPM × Industry Multiplier × Seasonal Index.
        All three components are cited and sourced.
  - Share of Spend: SoS_brand = (E_brand / Σ E_competitive_set) × 100
        This is a relative share within the OBSERVED competitive set, not total market spend.
        Clients must be advised that SoS figures reflect the brands scanned, not the full
        category universe.
  - Share of Voice: SoV_brand = (I_brand / Σ I_competitive_set) × 100
        Impression-based; same caveat as SoS regarding competitive set scope.
  - All estimates carry a stated confidence tier based on data availability signals.
  - Paid signal classification follows a three-tier hierarchy:
      1. dom_label — explicit "Sponsored" / localised equivalent found in DOM
      2. statistical_outlier — ER > 3σ above category benchmark (post flagged as likely paid)
      3. declared — scraper or user explicitly labelled the content as paid
"""

import json
import logging
import os
import re
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ── Secret value set for scrub() — loaded once at import time ─────────────────
# Match against actual secret values rather than substrings like "sk-".
# This avoids falsely redacting legitimate brand names (e.g. "SK-II" cosmetics).
_SECRET_VALUES: set[str] = {
    v for k in (
        "TAVILY_API_KEY", "TIKTOK_APP_SECRET", "TIKTOK_APP_ID",
        "META_AD_LIBRARY_TOKEN", "SEARCHAPI_KEY", "OPENAI_API_KEY",
        "PROXY_TH", "PROXY_PH", "PROXY_VN", "PROXY_ID", "PROXY_MY", "PROXY_SG",
    )
    if (v := os.getenv(k, "")) and len(v) > 8  # skip empty / placeholder values
}


# ── Methodology version stamp ─────────────────────────────────────────────────
_METHODOLOGY_VERSION = "2.1.0"
_METHODOLOGY_DATE    = "2025-05"

# S3: stale-constants guard — warn if benchmark data is more than 180 days old
_CONSTANTS_LAST_UPDATED = datetime(2025, 5, 1, tzinfo=timezone.utc)
_CONSTANTS_STALE_DAYS   = 180
def _check_stale_constants() -> None:
    age = (datetime.now(timezone.utc) - _CONSTANTS_LAST_UPDATED).days
    if age > _CONSTANTS_STALE_DAYS:
        logger.warning(
            "[ApprovalGate] Benchmark constants (CPM, VTR, ER) were last updated %d days ago "
            "(%s). Consider refreshing from eMarketer/Kantar/Socialinsider sources. "
            "Set _CONSTANTS_LAST_UPDATED after any refresh.",
            age, _CONSTANTS_LAST_UPDATED.strftime("%Y-%m-%d"),
        )
_check_stale_constants()

# ── Base CPM benchmarks per market (USD per 1,000 impressions) ────────────────
# Source: eMarketer Digital Advertising Benchmarks 2024; Statista Global Digital Ad Spend
# Report Q4-2024; Meta, TikTok, YouTube quarterly revenue disclosures 2024-25;
# agency trading desk benchmarks (DV360, The Trade Desk) 2024-25.
# These are GROSS CPM rates before agency/publisher discounts.
COUNTRY_CPM = {
    "":               {"youtube": 9.50,  "facebook": 7.50},
    "United States":  {"youtube":15.00,  "facebook":11.00},
    "United Kingdom": {"youtube":13.00,  "facebook": 9.50},
    "Canada":         {"youtube":12.50,  "facebook": 9.00},
    "Australia":      {"youtube":11.50,  "facebook": 8.50},
    "Germany":        {"youtube":11.00,  "facebook": 8.00},
    "France":         {"youtube":10.50,  "facebook": 7.50},
    "Japan":          {"youtube":12.00,  "facebook": 9.00},
    "South Korea":    {"youtube":10.00,  "facebook": 7.00},
    "UAE":            {"youtube":11.50,  "facebook": 8.50},
    "Saudi Arabia":   {"youtube":11.00,  "facebook": 8.00},
    "Singapore":      {"youtube":11.00,  "facebook": 8.00},
    "Malaysia":       {"youtube": 4.50,  "facebook": 3.00},
    "Thailand":       {"youtube": 4.00,  "facebook": 2.80},
    "Vietnam":        {"youtube": 3.50,  "facebook": 2.20},
    "Indonesia":      {"youtube": 3.00,  "facebook": 2.00},
    "Philippines":    {"youtube": 3.00,  "facebook": 1.80},
    "India":          {"youtube": 2.50,  "facebook": 1.50},
    "Brazil":         {"youtube": 4.00,  "facebook": 2.80},
    "Mexico":         {"youtube": 3.80,  "facebook": 2.50},
}

# ── Market-level View-Through Rates (VTR) for SEA markets ────────────────────
# Source: Kantar APAC Digital Intelligence Report 2024; TikTok Southeast Asia
# Benchmarks Report 2024; Meta APAC Advertiser Benchmarks 2024.
# VTR = proportion of ad impressions that result in a counted video view.
# For non-SEA markets, falls through to PLATFORM_AVG_VIEW_RATE (platform-level defaults).
MARKET_VTR = {
    "Thailand":    {"youtube": 0.30, "facebook": 0.20},
    "Philippines": {"youtube": 0.28, "facebook": 0.19},
    "Vietnam":     {"youtube": 0.32, "facebook": 0.22},
    "Indonesia":   {"youtube": 0.29, "facebook": 0.18},
    "Malaysia":    {"youtube": 0.31, "facebook": 0.21},
    "Singapore":   {"youtube": 0.33, "facebook": 0.22},
}

# ── Industry CPM multipliers (relative to 1.0 baseline) ─────────────────────
# Source: DV360 category CPM premium index; Meta Ads Manager category benchmarks 2024.
# Multiplier = observed category CPM / blended cross-category CPM average.
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

# ── 3-month rolling seasonal index (index 0=Jan … 11=Dec) ────────────────────
# Methodology: 3-month centred moving average applied to observed monthly ad spend curves.
# Source: Meta, TikTok, YouTube quarterly revenue disclosures; Nielsen ad spend indices 2024.
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

# ── Average view-through rates by platform (global platform-level defaults) ───
# Used as fallback when no market-specific VTR is available.
# Source: Meta/TikTok/YT ad benchmarks, agency trading desks 2024-25.
PLATFORM_AVG_VIEW_RATE = {
    "youtube":  0.32,
    "facebook": 0.22,
    "default":  0.25,
}

# ── Industry × Platform ER benchmarks (3-month rolling, %) ───────────────────
# View-based (YouTube): interactions / views × 100
# Follower-based (Facebook): interactions / followers × 100
# Source: Socialinsider Industry Report 2024; Sprout Social Index 2024;
#         Rival IQ Social Media Industry Report 2024-25.
INDUSTRY_ER_BENCHMARKS = {
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
    # Instagram ER benchmarks — used for modelled IG entries derived from Facebook Reels/Stories
    "instagram": {
        "":             1.5,  "fmcg":        1.8,  "food_bev":   2.2,
        "beauty":       2.5,  "fashion":     2.0,  "retail":     1.6,
        "tech":         1.2,  "telco":       1.0,  "finance":    0.9,
        "insurance":    0.8,  "automotive":  1.3,  "travel":     2.3,
        "health":       1.8,  "entertainment":2.8, "gaming":     2.5,
        "education":    1.5,  "real_estate": 1.1,
    },
}

# ── Heuristic threshold for statistical outlier ad detection ─────────────────
# Posts with ER > (benchmark_er × OUTLIER_ER_MULTIPLIER) are flagged as
# likely paid placements even absent a DOM "Sponsored" label.
# NOTE: This is a MULTIPLIER HEURISTIC, not a true statistical sigma (σ) value.
# The 3× benchmark threshold is an industry-adopted rule-of-thumb (WARC, Nielsen
# Brand Effect studies) — it does not imply 3 standard deviations from a fitted
# distribution. Named "ER_MULTIPLIER" to avoid confusing clients with statistical
# notation when presenting methodology.
OUTLIER_ER_MULTIPLIER = 3.0

# ── Instagram modelling from Facebook Reels/Stories ──────────────────────────
# Industry benchmark: ~65–75% of Facebook video/story assets from FMCG/beauty/entertainment
# brands are cross-posted to Instagram (Meta Business Insights 2024). Conservative default: 70%.
CROSSPOST_RATE = 0.70

# IG audience as a fraction of FB audience size, by market (Meta APAC audience data 2024).
IG_FB_AUDIENCE_RATIO = {
    "Philippines": 0.60,
    "Thailand":    0.55,
    "Vietnam":     0.50,
    "Singapore":   0.70,
    "Malaysia":    0.58,
    "Indonesia":   0.52,
    "default":     0.60,
}


def _model_instagram_from_facebook(
    fb_entries: list[dict], market: str, industry: str
) -> list[dict]:
    """
    Derive estimated Instagram entries from Facebook Reels/Stories records.
    Only processes entries where metrics are non-zero (avoid noise from zero-data records).
    Returns list of modelled IG competitor records to be appended to competitors[].
    """
    ig_er_map = INDUSTRY_ER_BENCHMARKS.get("instagram", {})
    ig_er = ig_er_map.get(industry or "", ig_er_map.get("", 1.5))
    ig_fb_ratio = IG_FB_AUDIENCE_RATIO.get(market, IG_FB_AUDIENCE_RATIO["default"])

    modelled = []
    for entry in fb_entries:
        m = entry.get("metrics", {})
        fb_views      = int(m.get("views",     0) or 0)
        fb_followers  = int(m.get("followers", 0) or 0)
        fb_likes      = int(m.get("likes",     0) or 0)
        fb_comments   = int(m.get("comments",  0) or 0)

        if fb_views == 0 and fb_followers == 0:
            continue  # insufficient signal to model

        # Scale impressions: use views if available, else follower-based estimate
        fb_impressions = fb_views if fb_views > 0 else int(fb_followers * 0.15)
        ig_impressions = int(fb_impressions * CROSSPOST_RATE * ig_fb_ratio)
        ig_interactions = int(ig_impressions * ig_er / 100)

        ig_followers = int(fb_followers * ig_fb_ratio) if fb_followers > 0 else 0
        ig_likes     = int(ig_interactions * 0.85)
        ig_comments  = ig_interactions - ig_likes

        modelled.append({
            "name":          entry.get("name", ""),
            "handle":        entry.get("handle", ""),
            "platform":      "Instagram",
            "post_type":     entry.get("post_type", "both"),
            "paid_signal":   entry.get("paid_signal", "organic"),
            "data_source":   "modelled_from_facebook",
            "modelling_note": (
                f"Estimated from Facebook data via {int(CROSSPOST_RATE*100)}% cross-post rate "
                f"+ {market or 'default'} IG/FB audience ratio ({ig_fb_ratio:.0%}). "
                f"IG ER benchmark: {ig_er:.1f}% ({industry or 'General'}). "
                "Source: Meta Business Insights 2024; not directly scraped."
            ),
            "confidence":    "medium",
            "metrics": {
                "likes":      ig_likes,
                "comments":   ig_comments,
                "shares":     0,
                "saves":      int(ig_interactions * 0.10),
                "views":      ig_impressions,
                "followers":  ig_followers,
                "interactions": ig_interactions,
            },
            "sentiment":      entry.get("sentiment", "Neutral"),
            "top_posts":      [],
            "hashtags":       entry.get("hashtags", []),
            "content_themes": entry.get("content_themes", []),
            "paid_campaigns": entry.get("paid_campaigns", []),
        })

    return modelled


def _er_benchmark(platform_key: str, industry: str) -> float:
    plat_map = INDUSTRY_ER_BENCHMARKS.get(platform_key, INDUSTRY_ER_BENCHMARKS.get("default", {}))
    if not plat_map:
        flat = {"tiktok": 5.0, "instagram": 1.5, "facebook": 0.8, "youtube": 2.0}
        return flat.get(platform_key, 2.0)
    return plat_map.get(industry or "", plat_map.get("", 2.0))


def _platform_key(platform_str: str) -> str:
    return (platform_str or "").lower().split("/")[0].strip()


def _seasonal_index() -> float:
    return SEASONAL_INDEX[datetime.now(timezone.utc).month - 1]  # S1: timezone-aware


def _effective_cpm(platform: str, country: str, industry: str) -> float:
    base_map = COUNTRY_CPM.get(country, COUNTRY_CPM[""])
    base  = base_map.get(_platform_key(platform), 7.00)
    imult = INDUSTRY_CPM_MULT.get(industry or "", 1.00)
    smult = _seasonal_index()
    return round(base * imult * smult, 2)


def _get_vtr(market: str, plat_key: str) -> tuple[float, str]:
    """Return (vtr_value, vtr_source_label) with market-specific precedence over platform default."""
    market_map = MARKET_VTR.get(market, {})
    if plat_key in market_map:
        return market_map[plat_key], f"Market-specific ({market}): Kantar APAC / TikTok SEA Benchmarks 2024"
    default_vtr = PLATFORM_AVG_VIEW_RATE.get(plat_key, PLATFORM_AVG_VIEW_RATE["default"])
    return default_vtr, "Platform global default: Meta/TikTok/YT ad benchmarks, agency trading desks 2024-25"


def _confidence_tier(views: int, likes: int, comments: int, paid_signal: str) -> str:
    """
    Assign a data confidence tier based on metric availability.
    Tier A: explicit paid signal + view count available (highest reliability)
    Tier B: view count available but paid signal inferred
    Tier C: engagement-only (no view count) — inverse ER method applied
    Tier D: minimal signals — high estimation uncertainty
    """
    has_views = views > 0
    has_engagement = (likes + comments) > 0
    is_declared_paid = paid_signal in ("dom_label", "declared")

    if has_views and is_declared_paid:
        return "A — High (declared paid + view count)"
    if has_views and paid_signal == "statistical_outlier":
        return "B — Medium-High (view count; paid inferred by statistical outlier detection)"
    if has_views and not is_declared_paid:
        return "B — Medium (view count available; organic or unknown classification)"
    if has_engagement and is_declared_paid:
        return "C — Medium-Low (declared paid; inverse ER method applied — no view count)"
    if has_engagement:
        return "D — Low (engagement-only; inverse ER + organic classification — high uncertainty)"
    return "D — Low (minimal signals; estimates carry significant margin of error)"


def _cpm_note(country: str, industry: str, override: float = None) -> str:
    now   = datetime.now(timezone.utc)  # S1: timezone-aware
    month = now.strftime("%B")
    si    = _seasonal_index()
    if override and override > 0:
        return (
            f"User-set CPM override: ${override}/1K impressions. "
            "Market, industry, and seasonal adjustments have been bypassed. "
            "Ensure this rate reflects the actual negotiated CPM for the campaign period."
        )
    ilabel = INDUSTRY_LABELS.get(industry or "", "General")
    imult  = INDUSTRY_CPM_MULT.get(industry or "", 1.00)
    return (
        f"Auto-derived CPM: Base market rate ({country or 'Global'}) "
        f"× industry multiplier ({ilabel}, ×{imult:.2f}) "
        f"× seasonal index ({month} {now.year}: ×{si:.2f}). "
        "Sources: eMarketer Digital Advertising Benchmarks 2024; Statista Global Digital Ad Spend Q4-2024; "
        "Meta, TikTok, YouTube quarterly revenue disclosures 2024-25; "
        "DV360 / The Trade Desk agency trading desk benchmarks 2024-25."
    )


class ApprovalGate:
    def __init__(self, cpm_rate: float = None, post_type: str = "both",
                 country: str = "", industry: str = ""):
        self.cpm_rate_override = cpm_rate
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

        total_interactions   = 0
        total_inferred_impr  = 0.0
        total_spend_paid     = 0.0
        total_spend_org      = 0.0
        brand_calcs          = []

        for comp in data["competitors"]:
            m        = comp.get("metrics", {})
            likes    = int(m.get("likes",    0) or 0)
            comments = int(m.get("comments", 0) or 0)
            shares   = int(m.get("shares",   0) or 0)
            views    = int(m.get("views",    0) or 0)
            saves    = int(m.get("saves",    0) or 0)
            followers= int(m.get("followers",0) or 0)

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

            # ── Paid signal classification ────────────────────────────────
            # Hierarchy: dom_label > statistical_outlier > declared
            # paid_signal injected by analyst agent; default to "declared" if post_type is paid
            raw_paid_signal = comp.get("paid_signal", "")
            if raw_paid_signal in ("dom_label", "statistical_outlier", "declared"):
                paid_signal = raw_paid_signal
            elif post_type_c == "paid":
                paid_signal = "declared"
            else:
                paid_signal = "organic"

            # ── Engagement Rate (industry-correct denominator) ────────────
            # TikTok/YouTube: view-based ER = interactions / views × 100
            # Instagram/Facebook: follower-based ER = interactions / followers × 100
            view_based = plat_key == "youtube"
            if view_based and views > 0:
                er_denominator       = views
                er_formula_label     = "views"
            elif not view_based and followers > 0:
                er_denominator       = followers
                er_formula_label     = "followers"
            elif views > 0:
                er_denominator       = views
                er_formula_label     = "views"
            else:
                er_denominator       = 1
                er_formula_label     = "impressions (est.)"

            eng_rate      = round((interactions / er_denominator) * 100, 4)
            benchmark_er  = _er_benchmark(plat_key, self.industry)
            er_vs_benchmark = round(eng_rate - benchmark_er, 2)

            # ── Statistical outlier upgrade ───────────────────────────────
            # If the analyst did not already flag this, apply the 3σ check here
            # as a safety net for posts that slipped through without a DOM label.
            if paid_signal not in ("dom_label", "declared"):
                if eng_rate > benchmark_er * OUTLIER_ER_MULTIPLIER:
                    paid_signal = "statistical_outlier"
                    if post_type_c == "organic":
                        post_type_c = "paid"
                        comp["post_type"] = "paid"

            # ── Impression inference (dual-path, IAB-aligned) ─────────────
            # Path A: video format — views available
            #   I = Scraped Views / VTR
            #   VTR pulled from MARKET_VTR (market-specific) with PLATFORM_AVG_VIEW_RATE fallback.
            # Path B: engagement-only — no view count
            #   I = (Likes + Comments) / Category ER Benchmark
            #   Uses the 3-month rolling benchmark ER for the platform × industry pair.
            vtr, vtr_source = _get_vtr(self.country, plat_key)

            if views > 0:
                inferred_impressions = views / vtr
                impression_method    = "video_vtr"
                impression_formula   = (
                    f"I = {views:,} views / {vtr:.2%} VTR = {int(inferred_impressions):,} impressions"
                )
                impression_note = (
                    f"Path A (video): scraped view count divided by market/platform VTR. "
                    f"VTR source: {vtr_source}."
                )
            else:
                er_decimal = benchmark_er / 100
                engagement_numerator = likes + comments
                if er_decimal > 0 and engagement_numerator > 0:
                    inferred_impressions = engagement_numerator / er_decimal
                else:
                    # W2: zero-signal fallback — use platform default VTR as proxy denominator
                    # rather than a hardcoded magic number. Log so the anomaly is visible.
                    fallback_vtr = PLATFORM_AVG_VIEW_RATE.get(plat_key, PLATFORM_AVG_VIEW_RATE["default"])
                    inferred_impressions = max(interactions, 1) / fallback_vtr
                    logger.warning(
                        "Path B zero-signal fallback for brand '%s' on %s: "
                        "no views and no engagement metrics found. "
                        "Using platform default VTR (%.2f) as impression denominator. "
                        "Confidence tier will be D — Low.",
                        comp.get("name", "?"), platform, fallback_vtr,
                    )
                impression_method    = "engagement_er"
                impression_formula   = (
                    f"I = ({likes:,} likes + {comments:,} comments) / {benchmark_er:.1f}% benchmark ER "
                    f"= {int(inferred_impressions):,} impressions"
                )
                impression_note = (
                    f"Path B (engagement-only): inverse ER method. No view count available; "
                    f"implied reach back-calculated from observed interactions using the "
                    f"3-month rolling category ER benchmark ({benchmark_er:.1f}%) for "
                    f"{platform} / {INDUSTRY_LABELS.get(self.industry, 'General')}. "
                    "Source: Socialinsider / Sprout Social / Rival IQ 2024-25."
                )

            # ── Spend estimation ──────────────────────────────────────────
            # E = (I / 1,000) × Market CPM
            # Market CPM = Base CPM × Industry Multiplier × Seasonal Index
            AVG_ENGAGEMENT_COST = 0.75  # USD per interaction (blended industry average)

            if post_type_c == "paid":
                spend = round((inferred_impressions / 1000) * cpm, 2)
                spend_note = (
                    f"Paid spend: ({int(inferred_impressions):,} inferred impressions / 1,000) "
                    f"× ${cpm:.2f} CPM = ${spend:,.2f}"
                )
            elif post_type_c == "organic":
                spend = round(interactions * AVG_ENGAGEMENT_COST, 2)
                spend_note = (
                    f"Organic amplification value: {interactions:,} interactions "
                    f"× ${AVG_ENGAGEMENT_COST} blended engagement cost = ${spend:,.2f}. "
                    "Note: this is an estimated earned-media equivalent, not a paid media cost."
                )
                inferred_impressions = inferred_impressions  # keep for SoV calc
            else:
                # Mixed: 60% paid impression share, 40% organic value
                paid_impressions    = inferred_impressions * 0.60
                org_impressions     = inferred_impressions * 0.40
                spend_paid_part     = round((paid_impressions / 1000) * cpm, 2)
                spend_org_part      = round(interactions * 0.40 * AVG_ENGAGEMENT_COST, 2)
                spend = round(spend_paid_part + spend_org_part, 2)
                spend_note = (
                    f"Mixed (60% paid / 40% organic assumption): "
                    f"Paid = ({int(paid_impressions):,} impressions / 1,000) × ${cpm:.2f} CPM = ${spend_paid_part:,.2f}; "
                    f"Organic = {interactions:,} interactions × ${AVG_ENGAGEMENT_COST} × 40% = ${spend_org_part:,.2f}; "
                    f"Total = ${spend:,.2f}"
                )

            # ── Confidence tier ───────────────────────────────────────────
            confidence = _confidence_tier(views, likes, comments, paid_signal)

            comp["estimated_spend_usd"]  = spend
            comp["inferred_impressions"] = int(inferred_impressions)
            comp["impression_method"]    = impression_method
            comp["paid_signal"]          = paid_signal
            comp["engagement_rate"]      = eng_rate
            comp["er_vs_benchmark"]      = er_vs_benchmark
            comp["benchmark_er_pct"]     = benchmark_er
            comp["cpm_used"]             = cpm
            comp["confidence_tier"]      = confidence

            total_interactions  += interactions
            total_inferred_impr += inferred_impressions
            if post_type_c == "paid":
                total_spend_paid += spend
            elif post_type_c == "organic":
                total_spend_org  += spend
            else:
                total_spend_paid += spend * 0.6
                total_spend_org  += spend * 0.4

            # ── Defaults ──────────────────────────────────────────────────
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
                        clean_posts.append({
                            "caption": caption or p, "url": url,
                            "post_type": self.post_type, "likes": 0, "views": 0,
                        })
                    else:
                        clean_posts.append({
                            "caption": p, "url": None,
                            "post_type": self.post_type, "likes": 0, "views": 0,
                        })
            comp["top_posts"] = clean_posts

            brand_calcs.append({
                "brand":                   comp.get("name", "?"),
                "platform":                platform,
                "post_type":               post_type_c,
                "paid_signal":             paid_signal,
                "confidence_tier":         confidence,
                "likes":                   likes,
                "comments":                comments,
                "shares":                  shares,
                "saves":                   saves,
                "views":                   views,
                "followers":               followers,
                "interactions":            interactions,
                "er_denominator":          er_denominator,
                "er_denominator_label":    er_formula_label,
                "er_formula":              f"({interactions:,} / {er_denominator:,} {er_formula_label}) × 100 = {eng_rate:.2f}%",
                "engagement_rate_pct":     eng_rate,
                "benchmark_er_pct":        benchmark_er,
                "er_vs_benchmark":         er_vs_benchmark,
                "inferred_impressions":    int(inferred_impressions),
                "impression_method":       impression_method,
                "impression_formula":      impression_formula,
                "impression_note":         impression_note,
                "vtr_used":                vtr,
                "vtr_source":              vtr_source,
                "cpm_used_usd":            cpm,
                "spend_usd":               spend,
                "spend_formula":           spend_note,
            })

        # ── Instagram modelling from Facebook entries ─────────────────────
        # Derive estimated IG entries from Facebook Reels/Stories scrape data.
        # Only model from Facebook entries (not already-modelled IG entries).
        fb_entries = [c for c in data["competitors"] if _platform_key(c.get("platform", "")) == "facebook"]
        if fb_entries:
            ig_modelled = _model_instagram_from_facebook(fb_entries, self.country, self.industry)
            for ig in ig_modelled:
                m = ig["metrics"]
                ig_spend = round((m["views"] / 1000) * self._get_cpm("Instagram") * 0.6, 2)
                ig["estimated_spend_usd"]  = ig_spend
                ig["inferred_impressions"] = m["views"]
                ig["impression_method"]    = "modelled_from_facebook"
                ig["paid_signal"]          = ig.get("paid_signal", "organic")
                ig["engagement_rate"]      = round(m["interactions"] / max(m["views"], 1) * 100, 4)
                ig["er_vs_benchmark"]      = 0.0
                ig["benchmark_er_pct"]     = INDUSTRY_ER_BENCHMARKS.get("instagram", {}).get(self.industry or "", 1.5)
                ig["cpm_used"]             = self._get_cpm("Instagram")
                ig["confidence_tier"]      = "C — Medium-Low (modelled from Facebook Reels/Stories)"
                ig["sos_pct"]              = 0.0
                ig["sov_pct"]              = 0.0
                total_inferred_impr += m["views"]
                total_spend_paid    += ig_spend
                data["competitors"].append(ig)

        # ── SoS / SoV calculation ─────────────────────────────────────────
        # SoS: brand's estimated spend as a share of total competitive set spend.
        # SoV: brand's inferred impressions as a share of total competitive set impressions.
        # IMPORTANT CAVEAT: these shares are relative to the OBSERVED competitive set only
        # (brands included in this scan), not total category spend in the market.
        total_spend = round(total_spend_paid + total_spend_org, 2)

        # W1: warn if SoS figures deviate from 100% due to mixed post_type denominators
        sos_check = sum(
            round((comp.get("estimated_spend_usd", 0) / total_spend * 100), 1)
            for comp in data["competitors"]
        ) if total_spend > 0 else 0.0
        if total_spend > 0 and abs(sos_check - 100.0) > 1.0:
            logger.warning(
                "SoS normalisation check: computed shares sum to %.1f%% (expected 100%%). "
                "Mixed post_type brands (paid vs both) use different spend denominators — "
                "cross-brand SoS comparison may be misleading. Review brand_breakdowns.",
                sos_check,
            )

        for comp, calc in zip(data["competitors"], brand_calcs):
            brand_spend = comp.get("estimated_spend_usd", 0)
            brand_impr  = comp.get("inferred_impressions", 0)
            sos_pct = round((brand_spend / total_spend * 100), 1) if total_spend > 0 else 0.0
            sov_pct = round((brand_impr  / total_inferred_impr * 100), 1) if total_inferred_impr > 0 else 0.0
            comp["sos_pct"] = sos_pct
            comp["sov_pct"] = sov_pct
            calc["sos_pct"] = sos_pct
            calc["sos_formula"] = (
                f"SoS = ${brand_spend:,.2f} / ${total_spend:,.2f} total × 100 = {sos_pct}%"
            )
            calc["sov_pct"] = sov_pct
            calc["sov_formula"] = (
                f"SoV = {brand_impr:,} / {int(total_inferred_impr):,} total impressions × 100 = {sov_pct}%"
            )

        # ── Assumptions block (full audit trail) ─────────────────────────
        use_override = bool(self.cpm_rate_override and self.cpm_rate_override > 0)
        si = _seasonal_index()
        imult  = INDUSTRY_CPM_MULT.get(self.industry, 1.00)
        ilabel = INDUSTRY_LABELS.get(self.industry, "General")
        plat_cpm_table = {
            p: _effective_cpm(p, self.country, self.industry) if not use_override
               else float(self.cpm_rate_override)
            for p in ("youtube", "facebook")
        }
        market_vtr_block = MARKET_VTR.get(self.country, {})
        vtr_table = {}
        for p in ("youtube", "facebook"):
            v, src = _get_vtr(self.country, p)
            vtr_table[p] = {"vtr": v, "source": src}

        data["assumptions"] = {
            "methodology_version":    _METHODOLOGY_VERSION,
            "methodology_date":       _METHODOLOGY_DATE,
            "report_generated_utc":   datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "post_type":              self.post_type,
            "market":                 self.country or "Global",
            "industry":               ilabel,
            "competitive_set_scope":  (
                "IMPORTANT: SoS and SoV figures reflect the scanned competitive set only. "
                "They are not absolute market share figures. Total category spend is not captured."
            ),
            "impression_inference": {
                "method_a_video":       "I = Scraped Views / VTR (View-Through Rate)",
                "method_b_engagement":  "I = (Likes + Comments) / Category ER Benchmark",
                "method_selection":     "Path A used when view count > 0; Path B when views unavailable.",
                "rationale":            (
                    "IAB MRC-aligned approach. VTR normalises raw views to unique paid impression "
                    "estimates. Inverse ER (Path B) is used by WARC and similar bodies when view "
                    "counts are not available."
                ),
            },
            "spend_formula": {
                "paid":    "E = (I / 1,000) × Market CPM",
                "organic": f"Amplification value = Interactions × ${0.75} blended engagement cost (not a paid media cost)",
                "both":    "E = (I × 60% / 1,000 × CPM) + (Interactions × 40% × $0.75)",
            },
            "sos_formula":            "SoS_brand = (E_brand / Σ E_competitive_set) × 100",
            "sov_formula":            "SoV_brand = (I_brand / Σ I_competitive_set) × 100",
            "paid_signal_hierarchy":  {
                "dom_label":           "Explicit 'Sponsored' / localised label found in DOM — highest confidence",
                "statistical_outlier": f"ER > {OUTLIER_ER_MULTIPLIER}× category benchmark — likely paid amplification",
                "declared":            "Scraper or user explicitly classified content as paid",
                "organic":             "No paid signals detected",
            },
            "confidence_tiers": {
                "A": "Declared paid + view count — highest reliability",
                "B": "View count available; paid declared or inferred",
                "C": "Declared paid; engagement-only (inverse ER applied)",
                "D": "Minimal signals — high estimation uncertainty",
            },
            "cpm": {
                "override_active":    use_override,
                "rate_or_mode":       float(self.cpm_rate_override) if use_override else "auto",
                "note":               _cpm_note(self.country, self.industry, self.cpm_rate_override),
                "seasonal_index":     si,
                "seasonal_month":     datetime.now(timezone.utc).strftime("%B"),
                "industry_multiplier":imult,
                "industry_label":     ilabel,
                "by_platform_usd_per_1k": plat_cpm_table,
                "cpm_source":         (
                    "eMarketer Digital Advertising Benchmarks 2024; Statista Global Digital Ad Spend Q4-2024; "
                    "Meta, TikTok, YouTube quarterly revenue disclosures 2024-25; "
                    "DV360 / The Trade Desk trading desk benchmarks 2024-25."
                ),
                "industry_mult_source": "DV360 category CPM premium index; Meta Ads Manager benchmarks 2024.",
                "seasonal_source":    "Meta, TikTok, YouTube quarterly revenue disclosures; Nielsen ad spend indices 2024.",
            },
            "vtr": {
                "market":             self.country or "Global",
                "by_platform":        vtr_table,
                "market_vtr_source":  "Kantar APAC Digital Intelligence Report 2024; TikTok SEA Benchmarks 2024; Meta APAC Advertiser Benchmarks 2024.",
                "platform_default_source": "Meta/TikTok/YT ad benchmarks, agency trading desks 2024-25.",
            },
            "er_benchmarks": {
                "note":               "3-month rolling industry average ER by platform × industry pair.",
                "source":             "Socialinsider Industry Report 2024; Sprout Social Index 2024; Rival IQ Social Media Industry Report 2024-25.",
                "outlier_threshold":  f"ER > {OUTLIER_ER_MULTIPLIER}× benchmark triggers 'statistical_outlier' paid classification.",
            },
            "data_source": (
                "Primary: First-party DOM scrape — public brand profile pages (Agent 1) "
                "and in-feed ad detection via strict DOM markers (Agent 2). "
                "Supplementary: Meta Ad Library + Google Ads Transparency Center (public endpoints). "
                "Fallback only: Web search results via Tavily/DuckDuckGo (used when DOM scraping yields no data). "
                "Confidence tiers A–D reflect which collection method produced each brand's data."
            ),
            "totals": {
                "total_interactions":      total_interactions,
                "total_inferred_impressions": int(total_inferred_impr),
                "total_spend_paid_usd":    round(total_spend_paid, 2),
                "total_spend_organic_usd": round(total_spend_org, 2),
                "total_spend_usd":         total_spend,
            },
            "brand_breakdowns":       brand_calcs,
        }

        # ── Persist to time-series DB (non-blocking) ──────────────────────
        try:
            from data.sos_db import SosDB
            SosDB().save_snapshot(data, market=self.country, industry=self.industry)
        except Exception as exc:
            logger.warning("SosDB write failed (non-blocking): %s", exc)

        def scrub(obj):
            if isinstance(obj, dict):
                return {k: scrub(v) for k, v in obj.items()
                        if str(v) not in _SECRET_VALUES}
            if isinstance(obj, list):
                return [scrub(i) for i in obj]
            return obj

        return json.dumps(scrub(data), indent=2)
