#!/usr/bin/env python3
"""
Benchmark Refresh Script — auto-updates CPM, VTR, and ER benchmark constants.

Sources searched:
  CPM : eMarketer, Statista, Meta/YouTube quarterly revenue disclosures
  VTR : Kantar APAC, TikTok SEA benchmarks, Meta APAC advertiser benchmarks
  ER  : Socialinsider Industry Report, Sprout Social Index, Rival IQ

Run manually:
  python3 scripts/refresh_benchmarks.py

Or triggered via:
  POST /refresh-benchmarks  (api.py)
  Weekly launchd / Railway cron

Output: data/benchmarks.json  (loaded by approval_gate.py on import)
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Allow running from repo root or scripts/ dir
_HERE = Path(__file__).parent
_ROOT = _HERE.parent
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
load_dotenv(_ROOT / ".env")

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

BENCHMARKS_PATH = _ROOT / "data" / "benchmarks.json"
BENCHMARKS_PATH.parent.mkdir(parents=True, exist_ok=True)

# ── Fallback — hardcoded baseline values (same as approval_gate.py) ───────────
# Used when extraction fails for a given key. Never left empty.
_FALLBACK = {
    "country_cpm": {
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
    },
    "market_vtr": {
        "Thailand":    {"youtube": 0.30, "facebook": 0.20},
        "Philippines": {"youtube": 0.28, "facebook": 0.19},
        "Vietnam":     {"youtube": 0.32, "facebook": 0.22},
        "Indonesia":   {"youtube": 0.29, "facebook": 0.18},
        "Malaysia":    {"youtube": 0.31, "facebook": 0.21},
        "Singapore":   {"youtube": 0.33, "facebook": 0.22},
    },
    "platform_avg_vtr": {"youtube": 0.32, "facebook": 0.22, "default": 0.25},
    "industry_er": {
        "facebook": {
            "":0.8,"fmcg":0.9,"food_bev":1.0,"beauty":1.1,"fashion":0.9,
            "retail":0.8,"tech":0.6,"telco":0.5,"finance":0.5,"insurance":0.4,
            "automotive":0.7,"travel":1.0,"health":0.8,"entertainment":1.2,
            "gaming":1.1,"education":0.7,"real_estate":0.5,
        },
        "youtube": {
            "":2.0,"fmcg":2.2,"food_bev":2.5,"beauty":3.0,"fashion":2.5,
            "retail":2.0,"tech":1.8,"telco":1.5,"finance":1.5,"insurance":1.2,
            "automotive":2.0,"travel":2.8,"health":2.2,"entertainment":3.5,
            "gaming":3.0,"education":2.0,"real_estate":1.3,
        },
        "instagram": {
            "":1.5,"fmcg":1.8,"food_bev":2.2,"beauty":2.5,"fashion":2.0,
            "retail":1.6,"tech":1.2,"telco":1.0,"finance":0.9,"insurance":0.8,
            "automotive":1.3,"travel":2.3,"health":1.8,"entertainment":2.8,
            "gaming":2.5,"education":1.5,"real_estate":1.1,
        },
    },
}


def _tavily_search(query: str, max_results: int = 5) -> list[dict]:
    try:
        from tavily import TavilyClient
        key = os.getenv("TAVILY_API_KEY", "")
        if not key:
            return []
        client = TavilyClient(api_key=key)
        resp = client.search(query, search_depth="advanced", topic="general",
                             max_results=max_results, include_answer=False)
        return [
            {"url": r.get("url",""), "title": r.get("title",""), "content": r.get("content","")[:1500]}
            for r in resp.get("results", []) if r.get("content")
        ]
    except Exception as e:
        logger.warning("Tavily search failed: %s", e)
        return []


def _ollama_extract(prompt: str, model: str = "gemma4:e4b") -> str:
    """Call local Ollama to extract structured data from benchmark snippets."""
    import urllib.request, json as _json
    ollama_host = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    payload = {
        "model":  model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.0, "num_predict": 2048},
    }
    try:
        req = urllib.request.Request(
            f"{ollama_host}/api/generate",
            data=_json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = _json.loads(resp.read())
            return result.get("response", "")
    except Exception as e:
        logger.warning("Ollama extraction failed: %s", e)
        return ""


def _parse_json_from_response(text: str) -> dict | None:
    """Extract first valid JSON object from LLM response."""
    import re
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```", "", text)
    start = text.find("{")
    end   = text.rfind("}") + 1
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end])
    except Exception:
        return None


# ── Per-table refresh functions ───────────────────────────────────────────────

def refresh_er_benchmarks(existing: dict) -> tuple[dict, list[str]]:
    """Search for latest ER benchmarks and extract via LLM."""
    sources = []
    queries = [
        "Socialinsider social media industry engagement rate benchmarks 2025 Facebook YouTube",
        "Sprout Social index engagement rate benchmark by industry 2025",
        "Rival IQ social media industry report engagement rate 2025 beauty FMCG finance",
    ]
    snippets = []
    for q in queries:
        results = _tavily_search(q, max_results=3)
        snippets.extend(results)
        sources.extend([r["url"] for r in results if r.get("url")])
        time.sleep(0.5)

    if not snippets:
        logger.warning("[BenchmarkRefresh] No ER snippets found — keeping existing values.")
        return existing, []

    snippet_text = "\n\n".join(
        f"[{i+1}] {s.get('title','')}\n{s.get('content','')}"
        for i, s in enumerate(snippets[:8])
    )

    prompt = f"""You are extracting social media engagement rate benchmarks from industry reports.
From the text below, extract the LATEST available engagement rate (ER) benchmarks as percentages.

ER definitions:
- Facebook/Instagram: interactions / followers × 100 (follower-based)
- YouTube: interactions / views × 100 (view-based)

Extract values for these industries: General (blank key ""), fmcg, food_bev, beauty, fashion, retail, tech, telco, finance, insurance, automotive, travel, health, entertainment, gaming, education, real_estate

Return ONLY valid JSON in this exact format (no markdown, no explanation):
{{
  "facebook": {{"": 0.8, "fmcg": 0.9, "beauty": 1.1, "finance": 0.5, ...}},
  "youtube":  {{"": 2.0, "fmcg": 2.2, "beauty": 3.0, "finance": 1.5, ...}},
  "instagram":{{"": 1.5, "fmcg": 1.8, "beauty": 2.5, "finance": 0.9, ...}}
}}

If a value is not found in the text, use the fallback: facebook general=0.8, youtube general=2.0, instagram general=1.5.
Include ALL industry keys even if you must use fallback values.

SOURCE TEXT:
{snippet_text}
"""

    raw = _ollama_extract(prompt)
    extracted = _parse_json_from_response(raw)

    if not extracted or not all(k in extracted for k in ("facebook", "youtube")):
        logger.warning("[BenchmarkRefresh] ER extraction incomplete — keeping existing values.")
        return existing, sources

    # Merge: keep fallback values for any missing industry keys
    merged = {}
    for platform in ("facebook", "youtube", "instagram"):
        fallback_plat = _FALLBACK["industry_er"].get(platform, {})
        extracted_plat = extracted.get(platform, {})
        merged[platform] = {**fallback_plat, **{k: v for k, v in extracted_plat.items() if isinstance(v, (int, float)) and 0 < v < 50}}

    logger.info("[BenchmarkRefresh] ER benchmarks updated.")
    return merged, sources


def refresh_cpm(existing: dict) -> tuple[dict, list[str]]:
    """Search for latest CPM benchmarks and extract via LLM."""
    sources = []
    queries = [
        "eMarketer digital advertising CPM benchmarks 2025 YouTube Facebook by country",
        "Statista global digital advertising CPM rates 2025 Asia Pacific",
        "Meta YouTube advertising CPM rates 2025 Southeast Asia Singapore Philippines",
    ]
    snippets = []
    for q in queries:
        results = _tavily_search(q, max_results=3)
        snippets.extend(results)
        sources.extend([r["url"] for r in results if r.get("url")])
        time.sleep(0.5)

    if not snippets:
        logger.warning("[BenchmarkRefresh] No CPM snippets found — keeping existing values.")
        return existing, []

    snippet_text = "\n\n".join(
        f"[{i+1}] {s.get('title','')}\n{s.get('content','')}"
        for i, s in enumerate(snippets[:8])
    )

    prompt = f"""You are extracting digital advertising CPM (cost per 1,000 impressions) rates in USD from industry reports.
From the text below, extract the LATEST available CPM benchmarks for YouTube and Facebook by country/market.

Return ONLY valid JSON in this exact format (no markdown, no explanation):
{{
  "": {{"youtube": 9.50, "facebook": 7.50}},
  "United States": {{"youtube": 15.00, "facebook": 11.00}},
  "United Kingdom": {{"youtube": 13.00, "facebook": 9.50}},
  "Singapore": {{"youtube": 11.00, "facebook": 8.00}},
  "Philippines": {{"youtube": 3.00, "facebook": 1.80}},
  "Thailand": {{"youtube": 4.00, "facebook": 2.80}},
  "Vietnam": {{"youtube": 3.50, "facebook": 2.20}},
  "Malaysia": {{"youtube": 4.50, "facebook": 3.00}},
  "Indonesia": {{"youtube": 3.00, "facebook": 2.00}},
  "India": {{"youtube": 2.50, "facebook": 1.50}}
}}

Include ALL markets shown above even if you must use the existing values as fallback.
Only update values you find explicitly stated in the source text.
Values must be in USD. Reasonable range: YouTube $1-20, Facebook $1-15.

SOURCE TEXT:
{snippet_text}
"""

    raw = _ollama_extract(prompt)
    extracted = _parse_json_from_response(raw)

    if not extracted:
        logger.warning("[BenchmarkRefresh] CPM extraction failed — keeping existing values.")
        return existing, sources

    # Merge extracted into existing; validate ranges
    merged = dict(existing)
    for market, platforms in extracted.items():
        if not isinstance(platforms, dict):
            continue
        clean = {
            p: round(float(v), 2)
            for p, v in platforms.items()
            if p in ("youtube", "facebook") and isinstance(v, (int, float)) and 0.5 < v < 30
        }
        if clean:
            merged[market] = {**(merged.get(market, {})), **clean}

    logger.info("[BenchmarkRefresh] CPM benchmarks updated.")
    return merged, sources


def refresh_vtr(existing: dict) -> tuple[dict, list[str]]:
    """Search for latest VTR benchmarks — primarily SEA markets."""
    sources = []
    queries = [
        "Kantar APAC digital advertising view-through rate benchmark 2025 Southeast Asia",
        "Meta APAC advertiser benchmarks view rate 2025 Philippines Thailand Vietnam",
        "YouTube video completion rate benchmark 2025 Asia Pacific",
    ]
    snippets = []
    for q in queries:
        results = _tavily_search(q, max_results=3)
        snippets.extend(results)
        sources.extend([r["url"] for r in results if r.get("url")])
        time.sleep(0.5)

    if not snippets:
        logger.warning("[BenchmarkRefresh] No VTR snippets found — keeping existing values.")
        return existing, []

    snippet_text = "\n\n".join(
        f"[{i+1}] {s.get('title','')}\n{s.get('content','')}"
        for i, s in enumerate(snippets[:6])
    )

    prompt = f"""You are extracting video view-through rate (VTR) benchmarks from advertising industry reports.
VTR = proportion of ad impressions that result in a counted video view (expressed as a decimal, e.g. 0.30 = 30%).

From the text below, extract VTR values for YouTube and Facebook by market.

Return ONLY valid JSON in this exact format (no markdown, no explanation):
{{
  "Thailand":    {{"youtube": 0.30, "facebook": 0.20}},
  "Philippines": {{"youtube": 0.28, "facebook": 0.19}},
  "Vietnam":     {{"youtube": 0.32, "facebook": 0.22}},
  "Indonesia":   {{"youtube": 0.29, "facebook": 0.18}},
  "Malaysia":    {{"youtube": 0.31, "facebook": 0.21}},
  "Singapore":   {{"youtube": 0.33, "facebook": 0.22}}
}}

Include all markets above. Only update values explicitly stated in the source. Values must be decimals between 0.05 and 0.70.

SOURCE TEXT:
{snippet_text}
"""

    raw = _ollama_extract(prompt)
    extracted = _parse_json_from_response(raw)

    if not extracted:
        logger.warning("[BenchmarkRefresh] VTR extraction failed — keeping existing values.")
        return existing, sources

    merged = dict(existing)
    for market, platforms in extracted.items():
        if not isinstance(platforms, dict):
            continue
        clean = {
            p: round(float(v), 3)
            for p, v in platforms.items()
            if p in ("youtube", "facebook") and isinstance(v, (int, float)) and 0.05 < v < 0.70
        }
        if clean:
            merged[market] = {**(merged.get(market, {})), **clean}

    logger.info("[BenchmarkRefresh] VTR benchmarks updated.")
    return merged, sources


# ── Main entry point ──────────────────────────────────────────────────────────

def run_refresh(force: bool = False) -> dict:
    """
    Run the full benchmark refresh cycle.
    Returns a status dict: {success, updated_at, sources, message}
    """
    logger.info("[BenchmarkRefresh] Starting benchmark refresh...")

    # Load existing benchmarks (or fallback if missing)
    existing = dict(_FALLBACK)
    if BENCHMARKS_PATH.exists():
        try:
            with open(BENCHMARKS_PATH) as f:
                on_disk = json.load(f)
            existing = {
                "country_cpm":      on_disk.get("country_cpm",      _FALLBACK["country_cpm"]),
                "market_vtr":       on_disk.get("market_vtr",       _FALLBACK["market_vtr"]),
                "platform_avg_vtr": on_disk.get("platform_avg_vtr", _FALLBACK["platform_avg_vtr"]),
                "industry_er":      on_disk.get("industry_er",      _FALLBACK["industry_er"]),
            }
        except Exception as e:
            logger.warning("[BenchmarkRefresh] Could not load existing benchmarks: %s", e)

    all_sources = []

    # ── Refresh each table ──────────────────────────────────────────────────
    new_er,  er_sources  = refresh_er_benchmarks(existing["industry_er"])
    new_cpm, cpm_sources = refresh_cpm(existing["country_cpm"])
    new_vtr, vtr_sources = refresh_vtr(existing["market_vtr"])

    all_sources = list(set(er_sources + cpm_sources + vtr_sources))

    result = {
        "country_cpm":      new_cpm,
        "market_vtr":       new_vtr,
        "platform_avg_vtr": existing["platform_avg_vtr"],
        "industry_er":      new_er,
        "updated_at":       datetime.now(timezone.utc).isoformat(),
        "sources":          all_sources[:20],  # cap stored sources
        "methodology":      (
            "Auto-refreshed via Tavily web search + Ollama LLM extraction. "
            "Sources: eMarketer, Statista, Socialinsider, Sprout Social, Rival IQ, Kantar APAC. "
            "Values validated for plausible ranges; fallback to prior values if extraction fails."
        ),
    }

    with open(BENCHMARKS_PATH, "w") as f:
        json.dump(result, f, indent=2)

    logger.info("[BenchmarkRefresh] Done. Written to %s", BENCHMARKS_PATH)
    return {"success": True, "updated_at": result["updated_at"], "sources": all_sources[:5]}


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Refresh Hermes benchmark constants")
    parser.add_argument("--force", action="store_true", help="Force refresh even if recently updated")
    args = parser.parse_args()
    result = run_refresh(force=args.force)
    print(json.dumps(result, indent=2))
