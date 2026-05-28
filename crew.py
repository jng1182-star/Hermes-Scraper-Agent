import json
import os
import re
import signal
import threading
import time
from collections import deque
from pathlib import Path
from crewai import Crew, Process
from agents import SocialAgents
from tasks import SocialTasks
from sentinel import (
    init_sentinel, get_sentinel, reset_sentinel,
    SentinelEvent, normalize_sov, _resolve_run_state,
)

# Per-phase caps
_PROFILE_TIMEOUT  = int(os.getenv("PROFILE_TIMEOUT",  "600"))  # 10 min — DOM scraping across multiple brands
_FEED_TIMEOUT     = int(os.getenv("FEED_TIMEOUT",     "600"))  # 10 min — two scroll passes + ad library
_ANALYST_TIMEOUT  = int(os.getenv("ANALYST_TIMEOUT",  "300"))  # 5 min — must complete before ngrok 550s drop
_REPORTER_TIMEOUT = int(os.getenv("REPORTER_TIMEOUT", "240"))  # 4 min
_GATE_TIMEOUT     = int(os.getenv("GATE_TIMEOUT",     "120"))  # 2 min


_PAUSE_GATE_TIMEOUT = 120   # max seconds phase waits for Sentinel resolution


def _run_with_timeout(fn, timeout_secs: int, phase_name: str):
    """Run fn() in a thread; raise RuntimeError (treated as stall) if it exceeds timeout_secs.
    Posts heartbeat and timeout-warning events to the Sentinel observer while waiting.
    Skips Sentinel pause gate when the phase ended via timeout — gate check is only
    meaningful after a clean completion."""
    import ctypes
    result       = [None]
    exc_holder   = [None]
    done_evt     = threading.Event()
    tid_holder   = [None]
    timed_out    = [False]   # set when we inject async exception into the phase thread

    def _target():
        tid_holder[0] = threading.current_thread().ident
        try:
            result[0] = fn()
        except Exception as e:
            exc_holder[0] = e
        finally:
            done_evt.set()

    t = threading.Thread(target=_target, daemon=True, name=f"phase-{phase_name}")
    t.start()

    # Notify Sentinel that phase has started; also stamp active_phase in _run_state
    # so the live log tap thread knows which phase new log lines belong to.
    _s = get_sentinel()
    if _s:
        _s.post(SentinelEvent(
            event_type="phase_start", phase_name=phase_name,
            timestamp=time.monotonic(),
            payload={"timeout_secs": timeout_secs},
        ))
    try:
        from sentinel import _resolve_run_state as _rrs
        _rs, _sl = _rrs()
        if _rs is not None:
            with _sl:
                _rs["active_phase"] = phase_name
    except Exception:
        pass

    # Poll with heartbeats instead of a single long wait
    _hb_interval = 30
    _start_ts    = time.monotonic()
    _warned_80   = False

    while not done_evt.wait(timeout=min(_hb_interval, timeout_secs)):
        elapsed   = time.monotonic() - _start_ts
        remaining = timeout_secs - elapsed
        if remaining <= 0:
            timed_out[0] = True
            # Interrupt the stuck phase thread
            if tid_holder[0]:
                try:
                    ctypes.pythonapi.PyThreadState_SetAsyncExc(
                        ctypes.c_ulong(tid_holder[0]),
                        ctypes.py_object(RuntimeError),
                    )
                except Exception:
                    pass
            raise RuntimeError(
                f"[PHASE TIMEOUT] {phase_name} exceeded {timeout_secs}s cap. "
                "None or empty — will retry from checkpoint."
            )
        pct = elapsed / timeout_secs
        if _s:
            _s.post(SentinelEvent(
                event_type="phase_heartbeat", phase_name=phase_name,
                timestamp=time.monotonic(),
                payload={"elapsed_secs": elapsed, "timeout_secs": timeout_secs},
            ))
            if pct >= 0.80 and not _warned_80:
                _warned_80 = True
                _s.post(SentinelEvent(
                    event_type="phase_timeout_warning", phase_name=phase_name,
                    timestamp=time.monotonic(),
                    payload={"elapsed_secs": elapsed, "timeout_secs": timeout_secs, "pct": pct},
                ))

    if exc_holder[0]:
        raise exc_holder[0]

    # Check Sentinel pause gate before returning output downstream.
    # Only on clean completion — skip if phase timed out (gate check too late to be useful).
    if _s and not timed_out[0]:
        pause_gate = _s.get_pause_gate(phase_name)
        if not pause_gate.is_set():
            pause_gate.wait(timeout=_PAUSE_GATE_TIMEOUT)

    return result[0]

_CHECKPOINT_DIR = Path("data/checkpoints")
_PHASE_NAMES    = ["researcher", "profile", "feed", "analyst", "reporter"]


def _cp_path(phase: str) -> Path:
    return _CHECKPOINT_DIR / f"{phase}.json"


def _save_checkpoint(phase: str, output: str) -> None:
    _CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)
    _cp_path(phase).write_text(json.dumps({"output": str(output)}), encoding="utf-8")


def _load_checkpoint(phase: str):
    p = _cp_path(phase)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8")).get("output")
        except Exception:
            return None
    return None


def clear_checkpoints() -> None:
    """Call this before a fresh run (not a retry)."""
    if _CHECKPOINT_DIR.exists():
        for f in _CHECKPOINT_DIR.iterdir():
            if f.suffix == ".json":
                try:
                    f.unlink()
                except Exception:
                    pass


def _fire_hook(node_id: str, state: str):
    """Fire the dashboard state hook if one is registered in main."""
    try:
        import main as _main
        if _main._state_hook:
            _main._state_hook(node_id, state)
    except Exception:
        pass


class SocialListeningCrew:
    def __init__(self, query: str, depth: str = "deep", params: dict = None,
                 resume: bool = False):
        self.query  = query
        self.params = params or {}
        self.agents = SocialAgents(depth=depth)
        self.tasks  = SocialTasks()
        self.resume = resume

    def run(self):
        researcher    = self.agents.researcher_agent()
        profile_agent = self.agents.profile_agent()
        feed_agent    = self.agents.feed_agent()
        analyst       = self.agents.analyst_agent()
        reporter      = self.agents.reporter_agent()

        # ── Sentinel Observer init ────────────────────────────────────────────
        _sentinel = None
        try:
            _rs, _sl = _resolve_run_state()
            if _rs is None:
                raise RuntimeError("Cannot resolve _run_state — neither api nor server importable")

            def _gate_log_fn(line: str) -> None:
                with _sl:
                    _rs["logs"].append(line)
                    _rs.setdefault("sentinel_logs", deque(maxlen=500)).append(line)

            def _flag_fn(flag_dict: dict) -> None:
                """Write/update a Sentinel flag into _run_state["active_flags"] for dashboard."""
                with _sl:
                    active = _rs.setdefault("active_flags", {})
                    fid = flag_dict.get("flag_id", "")
                    if fid:
                        active[fid] = flag_dict

            _sentinel = init_sentinel(_gate_log_fn, flag_fn=_flag_fn)
            _sentinel.start(run_params=self.params)
        except Exception as _se:
            print(f"[SENTINEL] Init failed: {_se} — running without observer.", flush=True)

        # ── Checkpoint recovery ───────────────────────────────────────────────
        cp_researcher = _load_checkpoint("researcher") if self.resume else None
        cp_profile    = _load_checkpoint("profile")    if self.resume else None
        cp_feed       = _load_checkpoint("feed")       if self.resume else None
        cp_analyst    = _load_checkpoint("analyst")    if self.resume else None

        try:
            # ── Phase 0: Researcher — identify correct social profiles ────────
            profile_map = cp_researcher
            if not profile_map:
                _fire_hook("scraper", "active")
                print("[PHASE] Researcher — identifying brand social profiles.", flush=True)
                task_research = self.tasks.researcher_task(researcher, self.params)
                crew_research = Crew(agents=[researcher], tasks=[task_research],
                                     process=Process.sequential, verbose=True)
                try:
                    crew_research.kickoff()
                    profile_map = str(task_research.output)
                    _save_checkpoint("researcher", profile_map)
                    print("[PHASE] Researcher complete.", flush=True)
                except Exception as exc:
                    if _sentinel:
                        _sentinel.receive_agent_response("researcher", f"Phase exception: {str(exc)[:200]}")
                    print(f"[PHASE] Researcher failed: {exc} — proceeding without profile map.", flush=True)
                    profile_map = ""
                finally:
                    _fire_hook("scraper", "done")

            # ── Phase 1: Profile scraper ──────────────────────────────────────
            profile_output = cp_profile
            if not profile_output:
                _fire_hook("profile", "active")
                print("[PHASE] Profile Scraper — collecting posts and building baselines.", flush=True)
                try:
                    task_profile = self.tasks.profile_task(profile_agent, self.params,
                                                           profile_map=profile_map)
                    crew_profile = Crew(agents=[profile_agent], tasks=[task_profile],
                                        process=Process.sequential, verbose=True)
                    _run_with_timeout(crew_profile.kickoff, _PROFILE_TIMEOUT, "profile")
                    profile_output = str(task_profile.output)
                    _save_checkpoint("profile", profile_output)
                    # Post data_quality_check events to Sentinel
                    _post_profile_quality(profile_output, _sentinel)
                    print("[PHASE] Profile Scraper complete.", flush=True)
                except Exception as exc:
                    if _sentinel:
                        _sentinel.receive_agent_response("profile", f"Phase exception: {str(exc)[:200]}")
                    print(f"[PHASE] Profile Scraper failed: {exc} — proceeding without baselines.", flush=True)
                    profile_output = "{}"
                finally:
                    _fire_hook("profile", "done")

            # Extract baselines from profile output (passed to feed task for schema compat)
            _profile_baselines = _extract_baselines(profile_output)

            # ── Phase 2: Ad Library queries (replaces feed scroll) ────────────
            # Feed scroll (Playwright authenticated sessions for Instagram/Facebook/TikTok/YouTube)
            # was retired: it crashed Railway containers (OOM on concurrent Chromium instances)
            # and required anti-detect browser infra not available in the deployed environment.
            # The primary SOV signal — ad creative volume, velocity, reach, geo — comes from
            # the ad libraries (Meta, Google ATC, TikTok CCL) which require no auth or feed context.
            # Profile scraper already captures organic paid detection via DOM labels + ER baselines.
            feed_output = cp_feed
            if not feed_output:
                _fire_hook("feed", "active")
                print("[PHASE] Ad Library — querying Meta, Google ATC, TikTok CCL.", flush=True)
                try:
                    task_feed = self.tasks.feed_task(feed_agent, self.params,
                                                     profile_map=profile_map,
                                                     profile_baselines=_profile_baselines)
                    crew_feed = Crew(agents=[feed_agent], tasks=[task_feed],
                                     process=Process.sequential, verbose=True)
                    _run_with_timeout(crew_feed.kickoff, _FEED_TIMEOUT, "feed")
                    feed_output = str(task_feed.output)
                    _save_checkpoint("feed", feed_output)
                    print("[PHASE] Ad Library complete.", flush=True)
                except Exception as exc:
                    if _sentinel:
                        _sentinel.receive_agent_response("feed", f"Phase exception: {str(exc)[:200]}")
                    print(f"[PHASE] Ad Library failed: {exc}", flush=True)
                    feed_output = "{}"
                finally:
                    _fire_hook("feed", "done")

            # ── Phase 3: Analyst ──────────────────────────────────────────────
            if cp_analyst:
                print("[RESUME] Analyst output restored from checkpoint. Running Reporter only.", flush=True)
            else:
                # Check if Sentinel flagged analyst_compact (fires when analyst approaches timeout)
                _compact_mode = False
                try:
                    _rs2, _sl2 = _resolve_run_state()
                    if _rs2 is not None:
                        with _sl2:
                            _compact_mode = _rs2.get("sentinel_directives", {}).get("analyst_compact", False)
                except Exception:
                    pass

                # Pull sentinel-derived synthetic baselines; patch or stub profile entries
                _profile_for_analyst = profile_output or "{}"
                try:
                    _rs3, _sl3 = _resolve_run_state()
                    if _rs3 is not None:
                        with _sl3:
                            _syn   = dict(_rs3.get("synthetic_baselines", {}))
                            _stubs = {k: v for k, v in _rs3.get("sentinel_directives", {}).items()
                                      if k.startswith("profile_stub:")}
                        if _syn or _stubs:
                            _pdata = json.loads(_profile_for_analyst) if _profile_for_analyst != "{}" else {}
                            _profiles = _pdata.setdefault("profiles", [])
                            # Patch existing entries that have baseline_available=False
                            _existing_keys = set()
                            for _entry in _profiles:
                                _key = f"{_entry.get('brand','')}:{_entry.get('platform','').lower()}"
                                _existing_keys.add(_key)
                                if _key in _syn and not _entry.get("baseline_available"):
                                    _sb = _syn[_key]
                                    _entry["baseline_available"] = True
                                    _entry["er_threshold"]       = _sb["er_threshold"]
                                    _entry["baseline_note"]      = f"sentinel_synthetic ({_sb['source']})"
                            # Add stub entries for brands the scraper missed entirely
                            for _stub_key, _stub_val in _stubs.items():
                                _key = f"{_stub_val['brand']}:{_stub_val['platform']}"
                                if _key not in _existing_keys:
                                    _profiles.append(_stub_val)
                            _profile_for_analyst = json.dumps(_pdata)
                except Exception:
                    pass

                analyst_context = _build_analyst_context(
                    _profile_for_analyst,
                    feed_output    or "{}",
                    profile_map    or "",
                    compact=_compact_mode,
                )
                if _compact_mode:
                    print("[SENTINEL] Analyst running in compact context mode (2000-char cap).", flush=True)
                task_analyst = self.tasks.analysis_task(analyst, prior_context=analyst_context, params=self.params)
                crew_analyst = Crew(agents=[analyst], tasks=[task_analyst],
                                    process=Process.sequential, verbose=True)
                _fire_hook("analyst", "active")
                _analyst_failed = False
                try:
                    _run_with_timeout(crew_analyst.kickoff, _ANALYST_TIMEOUT, "analyst")
                except Exception as _ae:
                    print(f"[PHASE] Analyst failed: {_ae} — Sentinel will attempt fallback synthesis.", flush=True)
                    _analyst_failed = True
                    if _sentinel:
                        _sentinel.receive_agent_response("analyst", f"Phase failed: {str(_ae)[:200]}")
                _fire_hook("analyst", "done")

                try:
                    _analyst_out = str(task_analyst.output) if not _analyst_failed else ""
                    if _analyst_out and len(_analyst_out) > 20:
                        cp_analyst = _analyst_out
                        _save_checkpoint("analyst", cp_analyst)
                    elif not cp_analyst:
                        # Sentinel fallback: synthesise minimal SOV records from raw signals
                        cp_analyst = _sentinel_fallback_analysis(
                            profile_output or "{}",
                            feed_output    or "{}",
                            self.params,
                        )
                        if cp_analyst:
                            _save_checkpoint("analyst", cp_analyst)
                            print("[SENTINEL] Fallback analyst synthesis complete — proceeding to reporter.", flush=True)
                        else:
                            cp_analyst = analyst_context[:2000]
                except Exception:
                    pass

            # ── Phase 4: Reporter ─────────────────────────────────────────────
            task_reporter = self.tasks.reporting_task(reporter, prior_context=cp_analyst, params=self.params)
            crew_reporter = Crew(agents=[reporter], tasks=[task_reporter],
                                 process=Process.sequential, verbose=True)
            _fire_hook("reporter", "active")
            raw = _run_with_timeout(crew_reporter.kickoff, _REPORTER_TIMEOUT, "reporter")
            _fire_hook("reporter", "done")
            _save_checkpoint("reporter", str(raw))

            # ── Post-processing: normalization, disclaimer, confidence caps ───
            # All arithmetic done in Python — never left to LLM judgment.
            raw = _postprocess_report(raw, self.params)

            return raw

        finally:
            # Always stop the Sentinel, even on exception
            if _sentinel:
                try:
                    _sentinel.stop()
                    reset_sentinel()
                except Exception:
                    pass


def _sentinel_fallback_analysis(profile_json: str, feed_json: str, params: dict) -> str:
    """
    Pure-Python SOV synthesis — no LLM required.
    Called when the analyst agent times out or returns empty output.
    Produces a minimal but valid SOV record set from raw scraper + ad library signals.

    Methodology: compute each of the 6 SOV signals directly from the structured data,
    normalize across brands per platform, and return a JSON string matching the
    analyst's expected_output schema. The reporter can then format this into the
    final report without needing to call the analyst again.
    """
    import copy
    from datetime import datetime, timezone

    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # ── Parse inputs ──────────────────────────────────────────────────────────
    try:
        profile_data = json.loads(_strip_md_fences(profile_json)) if profile_json else {}
    except Exception:
        profile_data = {}
    try:
        feed_data = json.loads(_strip_md_fences(feed_json)) if feed_json else {}
    except Exception:
        feed_data = {}

    profiles  = profile_data.get("profiles", [])
    adlib     = feed_data.get("ad_library_results", {})
    platforms = params.get("platforms", ["Facebook", "YouTube", "TikTok"])

    # ── Build per-brand signal dicts ──────────────────────────────────────────
    brand_signals: dict[str, dict] = {}

    for entry in profiles:
        brand    = (entry.get("brand") or "").strip()
        platform = (entry.get("platform") or "").lower().strip()
        if not brand:
            continue
        b = brand_signals.setdefault(brand, {})
        b.setdefault("platforms", {})
        b["platforms"][platform] = {
            "posts_in_scope":     entry.get("posts_in_scope", 0),
            "paid_post_count":    entry.get("paid_post_count", 0),
            "organic_post_count": entry.get("organic_post_count", 0),
            "avg_er_pct":         entry.get("avg_er_pct", 0.0),
            "follower_count":     entry.get("follower_count", 0),
            "baseline_available": entry.get("baseline_available", False),
        }

    for brand_name, bdata in adlib.items():
        b = brand_signals.setdefault(brand_name, {})
        b["adlib"] = {
            "active_ads_found": bdata.get("active_ads_found", 0),
            "impressions_min":  bdata.get("impressions_min"),
            "impressions_max":  bdata.get("impressions_max"),
            "new_ads_last_7d":  bdata.get("new_ads_last_7d", 0),
            "ad_start_dates":   bdata.get("ad_start_dates", []),
            "geo_countries":    bdata.get("geo_countries", []),
        }

    if not brand_signals:
        return ""

    brands_list = sorted(brand_signals.keys())

    # ── Compute per-platform SOV signals ──────────────────────────────────────
    def _tier(imp_min, imp_max):
        if imp_min is None and imp_max is None:
            return 0
        val = imp_max or imp_min or 0
        if val > 100_000: return 4
        if val > 10_000:  return 3
        if val > 1_000:   return 2
        return 1

    def _longevity(start_dates: list, today: str) -> float:
        if not start_dates:
            return 0.0
        try:
            today_dt = datetime.fromisoformat(today)
            days = []
            for d in start_dates:
                try:
                    days.append((today_dt - datetime.fromisoformat(d[:10])).days)
                except Exception:
                    pass
            return float(sum(days) / len(days)) if days else 0.0
        except Exception:
            return 0.0

    def _norm(vals: list[float]) -> list[float]:
        mx = max(vals) if vals else 0
        return [(v / mx * 100) if mx > 0 else 0.0 for v in vals]

    sov_records = []

    for plat in [p.lower() for p in platforms]:
        # Collect raw signals per brand for this platform
        raw_vol   = []
        raw_vel   = []
        raw_long  = []
        raw_reach = []
        raw_er    = []

        for brand in brands_list:
            b     = brand_signals[brand]
            adl   = b.get("adlib", {})
            pdata = (b.get("platforms") or {}).get(plat, {})

            raw_vol.append(float(adl.get("active_ads_found") or 0))
            raw_vel.append(float(adl.get("new_ads_last_7d") or 0))
            raw_long.append(_longevity(adl.get("ad_start_dates", []), today_str))
            raw_reach.append(float(_tier(adl.get("impressions_min"), adl.get("impressions_max"))))
            raw_er.append(float(pdata.get("avg_er_pct") or 0))

        norm_vol   = _norm(raw_vol)
        norm_vel   = _norm(raw_vel)
        norm_long  = _norm(raw_long)
        norm_reach = [v / 4 * 100 for v in raw_reach]   # tier 1-4 → 0-100
        norm_er    = _norm(raw_er)
        # Platform presence: brand active on this platform?
        active_counts = [
            len([pl2 for pl2 in brand_signals[b].get("platforms", {}) if pl2])
            for b in brands_list
        ]
        norm_pres = _norm([float(c) for c in active_counts])

        for i, brand in enumerate(brands_list):
            sov = (
                norm_vol[i]   * 0.30 +
                norm_vel[i]   * 0.10 +
                norm_long[i]  * 0.15 +
                norm_pres[i]  * 0.15 +
                norm_reach[i] * 0.15 +
                norm_er[i]    * 0.15
            )
            sov_records.append({
                "brand": brand, "platform": plat,
                "sov_index": round(min(sov, 100), 1),
                "signals": {
                    "creative_volume_share":    round(norm_vol[i],   1),
                    "creative_velocity_score":  round(norm_vel[i],   1),
                    "longevity_score":          round(norm_long[i],  1),
                    "geo_presence_score":       round(norm_pres[i],  1),
                    "reach_bucket_score":       round(norm_reach[i], 1),
                    "engagement_corroboration": round(norm_er[i],    1),
                },
                "confidence": "Low",
                "confidence_note": "Sentinel fallback synthesis — analyst timed out.",
                "consistency_flag": False,
                "source": "sentinel_fallback",
            })

    # ── Assemble per-brand output records ─────────────────────────────────────
    brand_records = []
    for brand in brands_list:
        plat_data = {}
        composite_total = 0.0
        plat_count = 0
        for rec in sov_records:
            if rec["brand"] != brand:
                continue
            plat_data[rec["platform"]] = {
                "sov_index":        rec["sov_index"],
                "sov_label":        f"{rec['sov_index']} (Directional / Indexed – Not Actual Spend)",
                "confidence":       rec["confidence"],
                "confidence_note":  rec["confidence_note"],
                "consistency_flag": rec["consistency_flag"],
                "signals":          rec["signals"],
                "posts":            [],
                "data_source":      rec["source"],
            }
            composite_total += rec["sov_index"]
            plat_count += 1

        composite = round(composite_total / plat_count, 1) if plat_count else 0.0
        brand_records.append({
            "name": brand,
            "markets": params.get("markets", [params.get("country", "")]),
            "platforms": plat_data,
            "composite_sov": composite,
            "composite_sov_label": f"{composite} (Directional / Indexed – Not Actual Spend)",
            "composite_confidence": "Low",
            "content_themes": [],
            "hashtags": [],
            "top_posts": [],
            "keywords_by_type": {"brand_say": [], "sma": [], "others_say": []},
            "by_month": [], "by_week": [], "by_day": [],
            "sentiment": "Neutral",
        })

    return json.dumps(brand_records, indent=None)


def _post_profile_quality(profile_output: str, sentinel) -> None:
    """Post data_quality_check events to the Sentinel after profile phase completes."""
    if not sentinel or not profile_output or profile_output == "{}":
        return
    try:
        data = json.loads(_strip_md_fences(profile_output))
        for entry in data.get("profiles", []):
            sentinel.post(SentinelEvent(
                event_type="data_quality_check",
                phase_name="profile",
                timestamp=time.monotonic(),
                payload={
                    "brand":              entry.get("brand", ""),
                    "platform":           entry.get("platform", ""),
                    "post_count":         entry.get("posts_in_scope", 0),
                    "baseline_available": entry.get("baseline_available", False),
                    "er_threshold":       entry.get("er_threshold", 0.0),
                    "consecutive_empty":  0,
                },
            ))
    except Exception:
        pass


def _postprocess_report(raw, params: dict) -> object:
    """
    Apply Python-enforced post-processing to the reporter's output:
      1. SOV normalization per platform to sum to 100
      2. Methodology disclaimer injection (if absent)
      3. Confidence caps (baseline_available, signal coverage, TikTok non-EU)
      4. Instagram modelled from Facebook
      5. Schema version stamp
    Returns normalized output (same type as input if JSON parse fails).
    """
    raw_str = str(raw)
    try:
        cleaned = re.sub(r"```(?:json)?\s*|```", "", raw_str).strip()
        start   = cleaned.find("{")
        end     = cleaned.rfind("}") + 1
        if start == -1 or end <= start:
            return raw
        report = json.loads(cleaned[start:end])
        # Inject country for TikTok confidence cap
        if params.get("country"):
            report.setdefault("country", params["country"])
        normalized = normalize_sov(report)
        print(
            f"[POST-PROCESS] SOV normalized. "
            f"Brands: {len(normalized.get('brands') or normalized.get('competitors') or [])}. "
            f"Disclaimer: {'present' if normalized.get('methodology_disclaimer') else 'INJECTED'}.",
            flush=True,
        )
        return normalized
    except Exception as exc:
        print(f"[POST-PROCESS] normalize_sov failed ({exc!s:.80}) — returning raw reporter output.", flush=True)
        # Must return a JSON-serializable dict; TaskOutput / str are not safe for json.dumps
        return {"raw_output": raw_str, "_postprocess_failed": True}


_BASELINE_FIELDS = {
    "platform", "handle", "brand", "follower_count",
    "avg_er_pct", "er_threshold", "baseline_available",
}


def _strip_md_fences(text: str) -> str:
    """Remove markdown code fences that LLMs sometimes wrap JSON output in."""
    text = re.sub(r"```(?:json)?\s*", "", text)
    text = re.sub(r"```", "", text)
    return text.strip()


def _extract_baselines(profile_json: str) -> str:
    """
    Pull the 'profiles' list from profile scraper output and return a whitelist-
    trimmed JSON string suitable for injection into the feed task's tool input.
    Strips markdown fences before parsing — the LLM agent may re-wrap clean JSON.
    Supports old checkpoints that used key 'baselines' instead of 'profiles'.
    """
    try:
        cleaned = _strip_md_fences(profile_json) if profile_json else ""
        data = json.loads(cleaned) if cleaned else {}
        profiles = data.get("profiles") or data.get("baselines", [])
        if not isinstance(profiles, list):
            profiles = []
        if data.get("baselines") and not data.get("profiles"):
            print("[WARN] _extract_baselines: using stale checkpoint key 'baselines' — "
                  "re-run from scratch to update.", flush=True)
        slim = [{k: v for k, v in p.items() if k in _BASELINE_FIELDS} for p in profiles]
        return json.dumps(slim)
    except Exception as exc:
        print(f"[WARN] _extract_baselines: JSON parse failed ({str(exc)[:80]}) — "
              "feed will use DOM labels only.", flush=True)
        return "[]"


def _merge_scrape_outputs(profile_json: str, feed_json: str) -> dict:
    """Combine profile and feed outputs; flag if any real data was collected."""
    profile_data = {}
    feed_data    = {}
    try:
        profile_data = json.loads(profile_json) if profile_json else {}
    except Exception:
        pass
    try:
        feed_data = json.loads(feed_json) if feed_json else {}
    except Exception:
        pass

    has_profile = bool(profile_data.get("profiles") and profile_data["profiles"])
    has_feed    = bool(
        feed_data.get("brand_paid_posts") or
        feed_data.get("total_posts_scrolled", 0) > 0
    )

    return {
        "has_data":    has_profile or has_feed,
        "has_profile": has_profile,
        "has_feed":    has_feed,
        "profile":     profile_data,
        "feed":        feed_data,
    }


def _build_analyst_context(profile_json: str, feed_json: str, search_json: str,
                            compact: bool = False) -> str:
    """
    Build a structured context string for the analyst agent.

    Instead of raw JSON with a hard character cap (which truncates mid-record and
    causes the analyst to hallucinate scores for truncated brands), this function
    produces a compact per-brand signal struct — only the 6 SOV input signals and
    key counts per brand×platform. This delivers complete, loss-free data for a
    5-brand scan in ~4,000 chars vs. the old 10,000-char cap on raw output.

    Post arrays (organic_posts, paid_posts) are excluded — the analyst scores on
    aggregate signals, not individual posts. A deep copy is used so the original
    data is not mutated for downstream use.
    """
    import copy
    parts = []

    # ── Profile scraper signal extraction ────────────────────────────────────
    profile_signals: list[dict] = []
    if profile_json and profile_json != "{}":
        try:
            _pd = copy.deepcopy(json.loads(profile_json))
            for entry in _pd.get("profiles", []):
                # Extract only the signal-relevant fields; drop post arrays
                profile_signals.append({
                    "brand":              entry.get("brand", ""),
                    "platform":           entry.get("platform", ""),
                    "handle":             entry.get("handle", ""),
                    "posts_in_scope":     entry.get("posts_in_scope", 0),
                    "organic_post_count": entry.get("organic_post_count", 0),
                    "paid_post_count":    entry.get("paid_post_count", 0),
                    "avg_er_pct":         entry.get("avg_er_pct", 0.0),
                    "er_threshold":       entry.get("er_threshold", 0.0),
                    "baseline_available": entry.get("baseline_available", False),
                    "baseline_note":      entry.get("baseline_note", ""),
                    "avg_likes":          entry.get("avg_likes", 0),
                    "avg_views":          entry.get("avg_views", 0),
                    "follower_count":     entry.get("follower_count", 0),
                    "data_source":        entry.get("data_source", "profile_scraper"),
                })
        except Exception:
            pass

    if profile_signals:
        parts.append(
            "=== AGENT 1: PROFILE SCRAPER (geo-unconstrained, DOM scrape) ===\n"
            "Signal quality: HIGH — real DOM numbers. Use avg_er_pct for ER corroboration.\n"
            "paid_post_count = DOM-labelled + ER-outlier detected paid posts.\n"
            "baseline_available=False → ER signal unavailable for this brand×platform.\n\n"
            + json.dumps(profile_signals, indent=None)
        )

    # ── Feed scroller signal extraction ──────────────────────────────────────
    feed_signals: dict = {}
    if feed_json and feed_json != "{}":
        try:
            _fd = json.loads(feed_json)
            # Extract ad library results per brand (primary SOV signals)
            for brand_key, adlib in (_fd.get("ad_library_results") or {}).items():
                feed_signals[brand_key] = {
                    "active_ads_found":   adlib.get("active_ads_found", 0),
                    "impressions_min":     adlib.get("impressions_min"),
                    "impressions_max":     adlib.get("impressions_max"),
                    "new_ads_last_7d":     adlib.get("new_ads_last_7d", 0),
                    "ad_start_dates":      (adlib.get("ad_start_dates") or [])[:3],
                    "geo_countries":       adlib.get("geo_countries", []),
                    "source":              adlib.get("source", "ad_library"),
                }
            # Top-level feed summary
            feed_summary = {
                "total_posts_scrolled": _fd.get("total_posts_scrolled", 0),
                "total_dom_ads":        _fd.get("total_dom_ads", 0),
                "total_baseline_outliers": _fd.get("total_baseline_outliers", 0),
                "baselines_applied":    _fd.get("baselines_applied", False),
                "brand_ad_library":     feed_signals,
            }
        except Exception:
            feed_summary = {}

        if feed_summary:
            parts.append(
                "=== AGENT 2: FEED SCROLLER (geo-bounded) + AD LIBRARIES ===\n"
                "Ad library = primary SOV signal (creative volume, velocity, reach, geo).\n"
                "baselines_applied=False → feed paid detection was DOM-only; "
                "under-reports paid in SEA markets (~30–40% miss rate).\n\n"
                + json.dumps(feed_summary, indent=None)
            )

    # ── Researcher profile map ────────────────────────────────────────────────
    _search_cap = 800 if compact else 1500
    if search_json:
        parts.append(
            "=== AGENT 0: RESEARCHER PROFILE MAP ===\n"
            "Verified brand handles/URLs discovered by the researcher before scraping.\n"
            "Use handle/url fields to cross-reference scraper data per brand×platform.\n\n"
            + search_json[:_search_cap]
        )

    # In compact mode, trim overall context to 2000 chars to prevent analyst stall
    joined = "\n\n".join(parts)
    if compact and len(joined) > 2000:
        joined = joined[:2000] + "\n[SENTINEL COMPACT MODE: context trimmed to 2000 chars]"
    return joined
