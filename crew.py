import json
import os
import signal
import threading
from pathlib import Path
from crewai import Crew, Process
from agents import SocialAgents
from tasks import SocialTasks

# Per-phase caps (scraper has no cap — runs until complete)
_ANALYST_TIMEOUT  = int(os.getenv("ANALYST_TIMEOUT",  "600"))  # 10 min — complex analysis needs time
_REPORTER_TIMEOUT = int(os.getenv("REPORTER_TIMEOUT", "300"))  # 5 min
_GATE_TIMEOUT     = int(os.getenv("GATE_TIMEOUT",     "120"))  # 2 min


def _run_with_timeout(fn, timeout_secs: int, phase_name: str):
    """Run fn() in a thread; raise RuntimeError (treated as stall) if it exceeds timeout_secs."""
    import ctypes
    result      = [None]
    exc_holder  = [None]
    done_evt    = threading.Event()
    tid_holder  = [None]

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
    if not done_evt.wait(timeout=timeout_secs):
        # Interrupt the stuck phase thread so it doesn't keep consuming resources
        if tid_holder[0]:
            try:
                ctypes.pythonapi.PyThreadState_SetAsyncExc(
                    ctypes.c_ulong(tid_holder[0]),
                    ctypes.py_object(RuntimeError),
                )
            except Exception:
                pass
        # Raise with "None or empty" so main.py stall-detection triggers a retry
        raise RuntimeError(
            f"[PHASE TIMEOUT] {phase_name} exceeded {timeout_secs}s cap. "
            "None or empty — will retry from checkpoint."
        )
    if exc_holder[0]:
        raise exc_holder[0]
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

        # ── Checkpoint recovery ───────────────────────────────────────────────
        cp_researcher = _load_checkpoint("researcher") if self.resume else None
        cp_profile    = _load_checkpoint("profile")    if self.resume else None
        cp_feed       = _load_checkpoint("feed")       if self.resume else None
        cp_analyst    = _load_checkpoint("analyst")    if self.resume else None

        # ── Phase 0: Researcher — identify correct social profiles ────────────
        # Runs first so profile_scraper and feed_scroller know exactly which
        # pages/handles to target, rather than guessing from brand names alone.
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
                print(f"[PHASE] Researcher failed: {exc} — proceeding without profile map.", flush=True)
                profile_map = ""
            finally:
                _fire_hook("scraper", "done")

        # ── Phase 1 + 2: Profile baseline + Feed scroll (concurrent) ──────────
        # Both receive the researcher's profile map so they target verified pages.
        profile_output = cp_profile
        feed_output    = cp_feed

        if not profile_output or not feed_output:
            import concurrent.futures

            def _run_profile():
                _fire_hook("profile", "active")
                try:
                    task = self.tasks.profile_task(profile_agent, self.params,
                                                   profile_map=profile_map)
                    crew = Crew(agents=[profile_agent], tasks=[task],
                                process=Process.sequential, verbose=True)
                    crew.kickoff()
                    _fire_hook("profile", "done")
                    return str(task.output)
                except Exception:
                    _fire_hook("profile", "done")
                    raise

            def _run_feed():
                _fire_hook("feed", "active")
                try:
                    task = self.tasks.feed_task(feed_agent, self.params,
                                                profile_map=profile_map)
                    crew = Crew(agents=[feed_agent], tasks=[task],
                                process=Process.sequential, verbose=True)
                    crew.kickoff()
                    _fire_hook("feed", "done")
                    return str(task.output)
                except Exception:
                    _fire_hook("feed", "done")
                    raise

            futures = {}
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                if not profile_output:
                    futures["profile"] = pool.submit(_run_profile)
                if not feed_output:
                    futures["feed"] = pool.submit(_run_feed)

                for key, fut in futures.items():
                    try:
                        result = fut.result(timeout=360)
                        if key == "profile":
                            profile_output = result
                            _save_checkpoint("profile", result)
                        else:
                            feed_output = result
                            _save_checkpoint("feed", result)
                        print(f"[PHASE] {key} complete.", flush=True)
                    except Exception as exc:
                        print(f"[PHASE] {key} failed: {exc}", flush=True)
                        if key == "profile":
                            profile_output = profile_output or "{}"
                        else:
                            feed_output = feed_output or "{}"

        # ── Phase 3: Analyst ──────────────────────────────────────────────────
        if cp_analyst:
            print("[RESUME] Analyst output restored from checkpoint. Running Reporter only.", flush=True)
        else:
            analyst_context = _build_analyst_context(
                profile_output or "{}",
                feed_output    or "{}",
                profile_map    or "",   # researcher output used as supplementary context
            )
            task_analyst = self.tasks.analysis_task(analyst, prior_context=analyst_context, params=self.params)
            crew_analyst = Crew(agents=[analyst], tasks=[task_analyst],
                                process=Process.sequential, verbose=True)
            _fire_hook("analyst", "active")
            _run_with_timeout(crew_analyst.kickoff, _ANALYST_TIMEOUT, "analyst")
            _fire_hook("analyst", "done")
            try:
                cp_analyst = str(task_analyst.output)
                _save_checkpoint("analyst", cp_analyst)
            except Exception:
                pass

        # ── Phase 4: Reporter ─────────────────────────────────────────────────
        task_reporter = self.tasks.reporting_task(reporter, prior_context=cp_analyst, params=self.params)
        crew_reporter = Crew(agents=[reporter], tasks=[task_reporter],
                             process=Process.sequential, verbose=True)
        _fire_hook("reporter", "active")
        raw = _run_with_timeout(crew_reporter.kickoff, _REPORTER_TIMEOUT, "reporter")
        _fire_hook("reporter", "done")
        _save_checkpoint("reporter", str(raw))
        return raw


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

    # Legacy Playwright format uses "baselines"; API tool uses "api_data"
    has_profile = bool(
        profile_data.get("baselines") or
        any(
            b.get("platform_data")
            for b in profile_data.get("api_data", [])
        )
    )
    has_feed = bool(
        feed_data.get("brand_matched_ads") or
        feed_data.get("total_ads_captured", 0) > 0
    )

    return {
        "has_data":    has_profile or has_feed,
        "has_profile": has_profile,
        "has_feed":    has_feed,
        "profile":     profile_data,
        "feed":        feed_data,
    }


def _build_analyst_context(profile_json: str, feed_json: str, search_json: str) -> str:
    """
    Build a combined context string for the analyst agent from all three sources.
    Clearly labels each data source so the analyst can weight them correctly.
    """
    parts = []

    if profile_json and profile_json != "{}":
        # Detect API data vs legacy Playwright data for labelling
        try:
            pd = json.loads(profile_json)
            is_api = bool(pd.get("api_data"))
        except Exception:
            is_api = False
        label = (
            "=== AGENT 1: BRAND API DATA (YouTube Data API v3 + Meta Ad Library) ===\n"
            "Source: Official platform APIs — exact real numbers.\n"
            "Confidence: HIGH — authoritative source data.\n\n"
        ) if is_api else (
            "=== AGENT 1: PROFILE BASELINE (DOM scrape) ===\n"
            "Source: Public brand profile pages.\n"
            "Confidence: HIGH — real numbers read from DOM.\n\n"
        )
        parts.append(label + profile_json[:5000])

    if feed_json and feed_json != "{}":
        parts.append(
            "=== AGENT 2: FEED AD CAPTURE (DOM scrape) ===\n"
            "Source: In-feed doom scroll — strict DOM marker detection.\n"
            "Confidence: HIGH — ads confirmed via explicit platform-native labels.\n\n"
            + feed_json[:3000]
        )

    if search_json:
        parts.append(
            "=== SEARCH FALLBACK (secondary source) ===\n"
            "Source: Web search snippets — use only where API/DOM data is absent.\n"
            "Confidence: LOWER — metrics inferred from editorial text.\n\n"
            + search_json[:2000]
        )

    combined = "\n\n".join(parts)
    if len(combined) > 10000:
        combined = combined[:10000] + "\n[... TRUNCATED ...]"
    return combined
