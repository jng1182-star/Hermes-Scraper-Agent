import json
import os
from pathlib import Path
from crewai import Crew, Process
from agents import SocialAgents
from tasks import SocialTasks

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
            raw = crew.kickoff()
            _save_checkpoint("reporter", str(raw))
            return raw

        elif cp_scraper:
            # Scraper done — run analyst + reporter
            print("[RESUME] Scraper output restored from checkpoint. Running Analyst + Reporter.", flush=True)
            task2 = self.tasks.analysis_task(analyst, prior_context=cp_scraper)
            task3 = self.tasks.reporting_task(reporter)
            crew  = Crew(agents=[analyst, reporter], tasks=[task2, task3],
                         process=Process.sequential, verbose=True)
            raw = crew.kickoff()
            # Save analyst checkpoint from task2 output
            try:
                _save_checkpoint("analyst", str(task2.output))
            except Exception:
                pass
            _save_checkpoint("reporter", str(raw))
            return raw

        else:
            # Full run — hook task callbacks to checkpoint each phase
            task1 = self.tasks.extraction_task(scraper, self.query, self.params)
            task2 = self.tasks.analysis_task(analyst)
            task3 = self.tasks.reporting_task(reporter)

            crew  = Crew(
                agents=[scraper, analyst, reporter],
                tasks=[task1, task2, task3],
                process=Process.sequential,
                verbose=True,
            )
            raw = crew.kickoff()

            # Save checkpoints after full run (for future resume)
            try:
                _save_checkpoint("scraper",  str(task1.output))
                _save_checkpoint("analyst",  str(task2.output))
                _save_checkpoint("reporter", str(raw))
            except Exception:
                pass

            return raw
