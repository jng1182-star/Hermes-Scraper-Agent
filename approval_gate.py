"""
Approval Gate — SOV index validation and quality assurance.

Validates:
  - All sov_index values are in [0, 100]
  - Per-platform sov_index values sum to approximately 100% across brands
  - Confidence scores are High/Medium/Low only
  - consistency_flag brands cannot have confidence above Medium
  - No dollar spend values present in output
  - methodology_disclaimer field is present with competitive set scope caveat
  - TikTok platform entry is present for every brand (or stubbed with Low confidence)

Confidence tiers (after consistency check):
  High:   >=3 signals from primary_api, no consistency_flag
  Medium: 2 primary signals OR 1 primary + 1 fallback OR has consistency_flag (was High)
  Low:    <=1 signal, only search_fallback, or consistency_flag + was already Medium
"""

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ── Secret value set for scrub() — loaded once at import time ─────────────────
_SECRET_VALUES: set[str] = {
    v for k in (
        "TIKTOK_APP_SECRET", "TIKTOK_APP_ID",
        "META_AD_LIBRARY_TOKEN", "SEARCHAPI_KEY", "OPENAI_API_KEY",
        "PROXY_TH", "PROXY_PH", "PROXY_VN", "PROXY_ID", "PROXY_MY", "PROXY_SG",
    )
    if (v := os.getenv(k, "")) and len(v) > 8
}

_METHODOLOGY_VERSION = "3.0.0"
_METHODOLOGY_DATE    = "2026-05"

_METHODOLOGY_DISCLAIMER = (
    "All values are directional Share-of-Voice indices (0–100 scale, "
    "Directional / Indexed – Not Actual Spend) reflecting relative advertising "
    "presence within the selected competitive set. These are estimates based on "
    "observable data (ad counts, reach proxies, presence signals) and do not "
    "represent actual spend figures. All indices are calculated within the context "
    "of the selected competitor group and represent share of voice among these "
    "competitors only, not an entire industry or market."
)

_STALE_DAYS = 14


def _load_live_benchmarks() -> dict | None:
    bench_path = os.path.join(os.path.dirname(__file__), "data", "benchmarks.json")
    if not os.path.exists(bench_path):
        return None
    try:
        with open(bench_path) as f:
            data = json.load(f)
        updated_at = data.get("updated_at", "")
        if updated_at:
            try:
                dt  = datetime.fromisoformat(updated_at)
                age = (datetime.now(timezone.utc) - dt).days
                if age > _STALE_DAYS:
                    logger.warning(
                        "[ApprovalGate] benchmarks.json is %d days old (threshold: %d).",
                        age, _STALE_DAYS,
                    )
                else:
                    logger.info("[ApprovalGate] Loaded live benchmarks (age: %d days).", age)
            except Exception:
                pass
        return data
    except Exception as e:
        logger.warning("[ApprovalGate] Could not load benchmarks.json: %s", e)
        return None


_LIVE = _load_live_benchmarks()

# ── Industry × Platform ER benchmarks (kept for engagement corroboration only) ─
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
    "tiktok": {
        "":             5.0,  "fmcg":        5.5,  "food_bev":   6.0,
        "beauty":       6.5,  "fashion":     6.0,  "retail":     5.0,
        "tech":         4.0,  "telco":       3.5,  "finance":    3.0,
        "insurance":    2.8,  "automotive":  4.5,  "travel":     5.5,
        "health":       5.0,  "entertainment":7.0, "gaming":     7.5,
        "education":    4.5,  "real_estate": 3.0,
    },
}

if _LIVE and _LIVE.get("industry_er"):
    INDUSTRY_ER_BENCHMARKS = _LIVE["industry_er"]


def _er_benchmark(platform_key: str, industry: str) -> float:
    plat_map = INDUSTRY_ER_BENCHMARKS.get(platform_key, {})
    if not plat_map:
        flat = {"tiktok": 5.0, "instagram": 1.5, "facebook": 0.8, "youtube": 2.0}
        return flat.get(platform_key, 2.0)
    return plat_map.get(industry or "", plat_map.get("", 2.0))


def _platform_key(platform_str: str) -> str:
    return (platform_str or "").lower().split("/")[0].strip()


def _confidence_tier_sov(signals_populated: int, primary_api_count: int) -> str:
    """Base confidence tier from signal count and source quality."""
    if primary_api_count >= 3:
        return "High"
    if primary_api_count >= 2 or (primary_api_count >= 1 and signals_populated >= 2):
        return "Medium"
    return "Low"


def _apply_consistency_downgrade(confidence: str, consistency_flag: bool) -> str:
    """Downgrade confidence one tier if cross-signal consistency check failed."""
    if not consistency_flag:
        return confidence
    order = ["Low", "Medium", "High"]
    idx = order.index(confidence) if confidence in order else 0
    return order[max(0, idx - 1)]


_SPEND_FIELDS = frozenset({
    "estimated_spend_usd", "cpm_used", "sos_pct", "inferred_impressions",
    "impression_method", "impression_formula", "spend_formula", "paid_signal",
    "confidence_tier", "er_vs_benchmark", "benchmark_er_pct", "vtr_used",
    "spend_usd", "cpm_used_usd",
})


def scrub(text: str) -> str:
    """Redact secret API key values from any string before it leaves this process."""
    for secret in _SECRET_VALUES:
        if secret and secret in text:
            text = text.replace(secret, "***REDACTED***")
    return text


class ApprovalGate:
    def __init__(self, country: str = "", industry: str = ""):
        self.country  = country  or ""
        self.industry = industry or ""

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
                return {"brands": lst}
            except json.JSONDecodeError:
                pass
        raise ValueError(f"Could not parse JSON from agent output: {cleaned[:200]}")

    def _normalize_to_brands(self, data: dict) -> dict:
        """Convert legacy competitors[] schema to brands[] if needed."""
        if "brands" in data and data["brands"]:
            return data
        # Legacy format: competitors[] with estimated_spend_usd etc.
        competitors = data.get("competitors", [])
        if not competitors:
            data["brands"] = []
            return data

        by_brand: dict[str, dict] = {}
        for c in competitors:
            name = c.get("name", "Unknown")
            if name not in by_brand:
                by_brand[name] = {
                    "name": name,
                    "platforms": {},
                    "composite_sov": 0.0,
                    "composite_sov_label": "0.0 (Directional / Indexed – Not Actual Spend)",
                    "composite_confidence": "Low",
                    "content_themes": c.get("content_themes", []),
                    "hashtags": c.get("hashtags", []),
                    "top_posts": c.get("top_posts", []),
                    "sentiment": c.get("sentiment", "Neutral"),
                }
            plat = _platform_key(c.get("platform", "facebook"))
            er = float(c.get("engagement_rate") or c.get("metrics", {}).get("er_pct") or 0)
            sov_val = float(c.get("sov_pct") or 0)
            by_brand[name]["platforms"][plat] = {
                "sov_index": sov_val,
                "sov_label": f"{sov_val} (Directional / Indexed – Not Actual Spend)",
                "confidence": "Low",
                "consistency_flag": False,
                "signals": {
                    "creative_volume_share": 0.0,
                    "creative_velocity_score": 0.0,
                    "longevity_score": 0.0,
                    "geo_presence_score": 0.0,
                    "reach_bucket_score": 0.0,
                    "engagement_corroboration": er,
                },
            }
        data["brands"] = list(by_brand.values())
        return data

    def _stub_tiktok(self, brand: dict) -> None:
        """Inject a zero-score TikTok entry if missing."""
        platforms = brand.setdefault("platforms", {})
        if "tiktok" not in platforms:
            logger.warning(
                "[ApprovalGate] TikTok platform missing for brand '%s' — injecting Low-confidence stub.",
                brand.get("name", "?"),
            )
            platforms["tiktok"] = {
                "sov_index": 0.0,
                "sov_label": "0.0 (Directional / Indexed – Not Actual Spend)",
                "confidence": "Low",
                "consistency_flag": False,
                "signals": {
                    "creative_volume_share": 0.0,
                    "creative_velocity_score": 0.0,
                    "longevity_score": 0.0,
                    "geo_presence_score": 0.0,
                    "reach_bucket_score": 0.0,
                    "engagement_corroboration": 0.0,
                },
            }

    def _validate_platform(self, pd: dict, brand_name: str, plat: str) -> dict:
        """Clamp, label, enforce consistency downgrade, strip spend fields."""
        # Clamp sov_index
        raw_idx = pd.get("sov_index", 0)
        try:
            idx = round(float(raw_idx), 1)
        except (TypeError, ValueError):
            idx = 0.0
        idx = max(0.0, min(100.0, idx))
        pd["sov_index"] = idx
        pd["sov_label"] = f"{idx} (Directional / Indexed – Not Actual Spend)"

        # Validate confidence
        conf = pd.get("confidence", "Low")
        if conf not in ("High", "Medium", "Low"):
            logger.warning(
                "[ApprovalGate] Invalid confidence '%s' for %s/%s — defaulting to Low.",
                conf, brand_name, plat,
            )
            conf = "Low"

        # Apply consistency downgrade
        consistency_flag = bool(pd.get("consistency_flag", False))
        conf = _apply_consistency_downgrade(conf, consistency_flag)
        if consistency_flag and conf == "High":
            conf = "Medium"
        pd["confidence"] = conf
        pd["consistency_flag"] = consistency_flag

        # Ensure signals dict exists
        pd.setdefault("signals", {})
        for sig in ("creative_volume_share", "creative_velocity_score", "longevity_score",
                    "geo_presence_score", "reach_bucket_score", "engagement_corroboration"):
            pd["signals"].setdefault(sig, 0.0)
            try:
                pd["signals"][sig] = round(float(pd["signals"][sig]), 2)
            except (TypeError, ValueError):
                pd["signals"][sig] = 0.0

        # Strip any spend fields that hallucinated LLM may have added
        for f in _SPEND_FIELDS:
            pd.pop(f, None)

        return pd

    def process_final_report(self, raw_output: str) -> str:
        cleaned = self._extract_json(raw_output)
        data    = self._parse(cleaned)

        # Normalize to brands[] schema (handles legacy competitors[] too)
        data = self._normalize_to_brands(data)

        brands = data.get("brands", [])

        # ── Per-brand validation ──────────────────────────────────────────
        for brand in brands:
            brand_name = brand.get("name", "?")

            # Ensure TikTok entry exists
            self._stub_tiktok(brand)

            # Validate each platform
            platforms = brand.get("platforms", {})
            for plat, pd in platforms.items():
                if not isinstance(pd, dict):
                    platforms[plat] = {}
                    pd = platforms[plat]
                platforms[plat] = self._validate_platform(pd, brand_name, plat)

            # Validate composite SOV
            comp_raw = brand.get("composite_sov", 0)
            try:
                comp_sov = round(float(comp_raw), 1)
            except (TypeError, ValueError):
                comp_sov = 0.0
            comp_sov = max(0.0, min(100.0, comp_sov))
            brand["composite_sov"] = comp_sov
            brand["composite_sov_label"] = f"{comp_sov} (Directional / Indexed – Not Actual Spend)"

            # Validate composite confidence (lowest across platforms)
            plat_confs = [pd.get("confidence", "Low") for pd in platforms.values()]
            order = {"High": 3, "Medium": 2, "Low": 1}
            comp_conf = min(plat_confs, key=lambda c: order.get(c, 1)) if plat_confs else "Low"
            if comp_conf not in ("High", "Medium", "Low"):
                comp_conf = "Low"
            brand["composite_confidence"] = comp_conf

            # Ensure required fields
            brand.setdefault("content_themes", [])
            brand.setdefault("hashtags", [])
            brand.setdefault("top_posts", [])
            brand.setdefault("sentiment", "Neutral")

            # Strip spend fields at brand level
            for f in _SPEND_FIELDS:
                brand.pop(f, None)

        # ── Per-platform SOV sum check ────────────────────────────────────
        for plat in ("facebook", "youtube", "tiktok"):
            plat_sum = sum(
                b.get("platforms", {}).get(plat, {}).get("sov_index", 0)
                for b in brands
            )
            if brands and abs(plat_sum - 100.0) > 5.0:
                logger.warning(
                    "[ApprovalGate] %s SOV indices sum to %.1f%% (expected ~100%%). "
                    "Check normalization in analysis_task.",
                    plat.capitalize(), plat_sum,
                )

        # ── Confidence distribution check ─────────────────────────────────
        if brands:
            low_count = sum(1 for b in brands if b.get("composite_confidence") == "Low")
            if low_count / len(brands) > 0.5:
                logger.warning(
                    "[ApprovalGate] Low confidence dominant: %d/%d brands are Low. "
                    "Consider enabling API keys for primary data sources.",
                    low_count, len(brands),
                )

        # ── Ensure methodology_disclaimer ─────────────────────────────────
        if not data.get("methodology_disclaimer"):
            data["methodology_disclaimer"] = _METHODOLOGY_DISCLAIMER

        # ── Strip spend fields from top-level assumptions if present ──────
        data.pop("assumptions", None)
        for f in _SPEND_FIELDS:
            data.pop(f, None)

        # ── Ensure category_totals ────────────────────────────────────────
        cat = data.setdefault("category_totals", {})
        cat.setdefault("facebook_total_ads", 0)
        cat.setdefault("youtube_total_videos", 0)
        cat.setdefault("tiktok_total_ads", 0)
        cat["scan_date"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # ── Timestamp ─────────────────────────────────────────────────────
        data["report_generated_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        data["methodology_version"]  = _METHODOLOGY_VERSION

        result = json.dumps(data, indent=2, ensure_ascii=False)
        return scrub(result)


# ── Sentinel override registry ────────────────────────────────────────────────
# Populated by server.py /sentinel-override POST endpoint.
# The Approval Gate has final authority — it can bypass any Sentinel directive.
_PENDING_OVERRIDES: dict[str, str] = {}
_OVERRIDE_LOCK = threading.Lock()


def register_override(flag_id: str, reason: str) -> None:
    """Called by server.py when a human sends a SENTINEL_OVERRIDE via the dashboard."""
    with _OVERRIDE_LOCK:
        _PENDING_OVERRIDES[flag_id] = reason
    try:
        from sentinel import get_sentinel
        s = get_sentinel()
        if s:
            s.override(flag_id, reason)
        else:
            logger.warning("[ApprovalGate] override for flag %s: no active Sentinel.", flag_id)
    except Exception as e:
        logger.warning("[ApprovalGate] Sentinel override dispatch failed: %s", e)


def pop_override(flag_id: str) -> str | None:
    """Consume and return a pending override reason, or None if not found."""
    with _OVERRIDE_LOCK:
        return _PENDING_OVERRIDES.pop(flag_id, None)
