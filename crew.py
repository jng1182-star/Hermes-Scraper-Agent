import json
import os
import signal
import threading
from pathlib import Path
from crewai import Crew, Process
from agents import SocialAgents
from tasks import SocialTasks

# Per-phase caps (scraper has no cap — runs until complete)
_ANALYST_TIMEOUT  = int(os.getenv("ANALYST_TIMEOUT",  "300"))  # 5 min
_REPORTER_TIMEOUT = int(os.getenv("REPORTER_TIMEOUT", "180"))  # 3 min
_GATE_TIMEOUT     = int(os.getenv("GATE_TIMEOUT",     "120"))  # 2 min


def _run_with_timeout(fn, timeout_secs: int, phase_name: str):
    """Run fn() in the current thread with a background timer that raises RuntimeError on timeout."""
    result      = [None]
    exc_holder  = [None]
    done_evt    = threading.Event()

    def _target():
        try:
            result[0] = fn()
        except Exception as e:
            exc_holder[0] = e
        finally:
            done_evt.set()

    t = threading.Thread(target=_target, daemon=True, name=f"phase-{phase_name}")
    t.start()
    if not done_evt.wait(timeout=timeout_secs):
        raise RuntimeError(
            f"[PHASE TIMEOUT] {phase_name} exceeded {timeout_secs}s cap. "
            "Will retry from checkpoint."
        )
    if exc_holder[0]:
        raise exc_holder[0]
    return result[0]

_CHECKPOINT_DIR = Path("data/checkpoints")
_PHASE_NAMES    = ["profile", "feed", "scraper", "analyst", "reporter"]


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
        profile_agent = self.agents.profile_agent()
        feed_agent    = self.agents.feed_agent()
        scraper       = self.agents.scraper_agent()   # fallback search-based
        analyst       = self.agents.analyst_agent()
        reporter      = self.agents.reporter_agent()

        # ── Checkpoint recovery ───────────────────────────────────────────────
        cp_profile = _load_checkpoint("profile") if self.resume else None
        cp_feed    = _load_checkpoint("feed")    if self.resume else None
        cp_scraper = _load_checkpoint("scraper") if self.resume else None
        cp_analyst = _load_checkpoint("analyst") if self.resume else None

        # ── Phase 1 + 2: Profile baseline + Feed scroll (run concurrently) ───
        # Both are I/O-bound Playwright tasks — running them concurrently cuts
        # total scrape time roughly in half. They feed the analyst independently.
        profile_output = cp_profile
        feed_output    = cp_feed

        if not profile_output or not feed_output:
            import concurrent.futures, json as _json

            def _run_profile():
                _fire_hook("profile", "active")
                try:
                    task = self.tasks.profile_task(profile_agent, self.params)
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
                    task = self.tasks.feed_task(feed_agent, self.params)
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

        # ── Phase 3: Search fallback (only if both DOM scrapes returned nothing) ──
        # Combine profile + feed output into context for the analyst.
        combined_scrape = _merge_scrape_outputs(profile_output or "{}", feed_output or "{}")

        if not combined_scrape.get("has_data") and not cp_scraper:
            print("[PHASE] DOM scrapes empty — running search fallback.", flush=True)
            _fire_hook("scraper", "active")
            task_search = self.tasks.extraction_task(scraper, self.query, self.params)
            crew_search = Crew(agents=[scraper], tasks=[task_search],
                               process=Process.sequential, verbose=True)
            crew_search.kickoff()
            fallback_output = str(task_search.output)
            _save_checkpoint("scraper", fallback_output)
            cp_scraper = fallback_output
            _fire_hook("scraper", "done")

        # ── Phase 4: Analyst ──────────────────────────────────────────────────
        if cp_analyst:
            print("[RESUME] Analyst output restored from checkpoint. Running Reporter only.", flush=True)
        else:
            # Pass combined first-party data + optional search fallback to analyst
            analyst_context = _build_analyst_context(
                profile_output or "{}",
                feed_output    or "{}",
                cp_scraper     or "",
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

        # ── Phase 5: Reporter ─────────────────────────────────────────────────
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

    has_profile = bool(profile_data.get("baselines"))
    has_feed    = bool(feed_data.get("brand_matched_ads") or feed_data.get("total_ads_captured", 0) > 0)

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
        parts.append(
            "=== AGENT 1: PROFILE BASELINE (first-party DOM scrape) ===\n"
            "Source: Public brand profile pages — actual observed metrics.\n"
            "Confidence: HIGH — these are real numbers read from the DOM.\n\n"
            + profile_json[:4000]
        )

    if feed_json and feed_json != "{}":
        parts.append(
            "=== AGENT 2: FEED AD CAPTURE (first-party DOM scrape) ===\n"
            "Source: In-feed doom scroll — strict DOM marker detection.\n"
            "Confidence: HIGH — ads confirmed via explicit platform-native labels.\n\n"
            + feed_json[:4000]
        )

    if search_json:
        parts.append(
            "=== SEARCH FALLBACK (secondary source) ===\n"
            "Source: Web search snippets — use only where DOM scrape data is absent.\n"
            "Confidence: LOWER — metrics inferred from editorial text.\n\n"
            + search_json[:2000]
        )

    combined = "\n\n".join(parts)
    if len(combined) > 6000:
        combined = combined[:6000] + "\n[... TRUNCATED — earlier data omitted to stay within token limit ...]"
    return combined
