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
_PHASE_NAMES    = ["scraper", "analyst", "reporter"]


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


class SocialListeningCrew:
    def __init__(self, query: str, depth: str = "deep", params: dict = None,
                 resume: bool = False):
        self.query  = query
        self.params = params or {}
        self.agents = SocialAgents(depth=depth)
        self.tasks  = SocialTasks()
        self.resume = resume   # True → reuse existing checkpoints where possible

    def run(self):
        scraper  = self.agents.scraper_agent()
        analyst  = self.agents.analyst_agent()
        reporter = self.agents.reporter_agent()

        # ── Check which phases are already checkpointed ───────────────────────
        cp_scraper  = _load_checkpoint("scraper")  if self.resume else None
        cp_analyst  = _load_checkpoint("analyst")  if self.resume else None

        # ── Build task list, skipping completed phases ────────────────────────
        # Determine start phase
        if cp_analyst:
            # Both scraper and analyst done — only run reporter
            print("[RESUME] Scraper + Analyst outputs restored from checkpoint. Running Reporter only.", flush=True)
            task3 = self.tasks.reporting_task(reporter, prior_context=cp_analyst)
            crew  = Crew(agents=[reporter], tasks=[task3],
                         process=Process.sequential, verbose=True)
            raw = _run_with_timeout(crew.kickoff, _REPORTER_TIMEOUT, "reporter")
            _save_checkpoint("reporter", str(raw))
            return raw

        elif cp_scraper:
            # Scraper done — run analyst + reporter sequentially with caps
            print("[RESUME] Scraper output restored from checkpoint. Running Analyst + Reporter.", flush=True)
            task2 = self.tasks.analysis_task(analyst, prior_context=cp_scraper)
            crew_analyst = Crew(agents=[analyst], tasks=[task2],
                                process=Process.sequential, verbose=True)
            _run_with_timeout(crew_analyst.kickoff, _ANALYST_TIMEOUT, "analyst")
            try:
                _save_checkpoint("analyst", str(task2.output))
            except Exception:
                pass

            task3 = self.tasks.reporting_task(reporter)
            crew_reporter = Crew(agents=[reporter], tasks=[task3],
                                 process=Process.sequential, verbose=True)
            raw = _run_with_timeout(crew_reporter.kickoff, _REPORTER_TIMEOUT, "reporter")
            _save_checkpoint("reporter", str(raw))
            return raw

        else:
            # Full run — all phases have hard caps
            task1 = self.tasks.extraction_task(scraper, self.query, self.params)
            crew_scraper = Crew(agents=[scraper], tasks=[task1],
                                process=Process.sequential, verbose=True)
            crew_scraper.kickoff()
            try:
                _save_checkpoint("scraper", str(task1.output))
            except Exception:
                pass

            task2 = self.tasks.analysis_task(analyst)
            crew_analyst = Crew(agents=[analyst], tasks=[task2],
                                process=Process.sequential, verbose=True)
            _run_with_timeout(crew_analyst.kickoff, _ANALYST_TIMEOUT, "analyst")
            try:
                _save_checkpoint("analyst", str(task2.output))
            except Exception:
                pass

            task3 = self.tasks.reporting_task(reporter)
            crew_reporter = Crew(agents=[reporter], tasks=[task3],
                                 process=Process.sequential, verbose=True)
            raw = _run_with_timeout(crew_reporter.kickoff, _REPORTER_TIMEOUT, "reporter")
            _save_checkpoint("reporter", str(raw))
            return raw
