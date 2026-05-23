from fastapi import FastAPI, BackgroundTasks
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from main import run_pipeline
import threading
from pathlib import Path
from collections import deque
from typing import List, Optional

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Shared state
_run_state = {
    "running": False,
    "logs": deque(maxlen=200),
    "error": None,
    "agent_states": {},  # e.g. {"scraper": "active", "analyst": "idle", ...}
    "timed_out": False,
    "retry_count": 0,
    "start_ts": None,   # monotonic time when run started
}
_state_lock = threading.Lock()

# Map CrewAI agent roles → dashboard node IDs
_ROLE_TO_NODE = {
    "social data scraper": "scraper",
    "engagement analyst": "analyst",
    "intelligence reporter": "reporter",
}


class ScanParams(BaseModel):
    advertiser: str = ""
    competitors: List[str] = []
    country: str = ""
    platforms: List[str] = []
    date_range: str = "Last 30 days"
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    cpm_rate: float = 15.0
    keywords: str = ""
    depth: str = "deep"  # "quick" | "deep"


def _set_agent_active(node_id: str, role_label: str = ""):
    with _state_lock:
        for aid, state in _run_state["agent_states"].items():
            if state == "active":
                _run_state["agent_states"][aid] = "done"
        _run_state["agent_states"][node_id] = "active"
        _run_state["logs"].append(f"[Agent] {role_label or node_id} → active")


def _set_agent_done(node_id: str, role_label: str = ""):
    with _state_lock:
        _run_state["agent_states"][node_id] = "done"
        _run_state["logs"].append(f"[Agent] {role_label or node_id} → done")


def _patch_crewai_agent():
    """
    Monkey-patch crewai.Agent.execute_task to fire state hooks directly.
    This is guaranteed to work in any thread — no event bus threading issues.
    """
    from crewai.agent.core import Agent as _Agent

    _orig = _Agent.execute_task

    def _patched(self, task, context=None, tools=None):
        role = (getattr(self, "role", "") or "").lower().strip()
        node_id = _ROLE_TO_NODE.get(role)
        if node_id:
            _set_agent_active(node_id, self.role)
        result = _orig(self, task, context=context, tools=tools)
        if node_id:
            _set_agent_done(node_id, self.role)
        return result

    _Agent.execute_task = _patched


_patch_crewai_agent()


def _log(msg: str):
    with _state_lock:
        _run_state["logs"].append(msg)


def _run_with_logging(params: dict):
    import sys, io, time as _time

    with _state_lock:
        _run_state["running"]     = True
        _run_state["error"]       = None
        _run_state["timed_out"]   = False
        _run_state["retry_count"] = 0
        _run_state["start_ts"]    = _time.monotonic()
        _run_state["logs"].clear()
        _run_state["agent_states"] = {
            "scraper": "idle",
            "analyst": "idle",
            "reporter": "idle",
            "gate": "idle",
        }

    class _LogCapture(io.TextIOBase):
        def write(self, s):
            if s.strip():
                with _state_lock:
                    _run_state["logs"].append(s.rstrip())
            return len(s)

        def flush(self):
            pass

    import main as _main_module

    def _pipeline_hook(node_id: str, state: str):
        if node_id == "__stall__":
            # state is "retry:N"
            retry_n = int(state.split(":")[1]) if ":" in state else 1
            with _state_lock:
                _run_state["timed_out"]   = True
                _run_state["retry_count"] = retry_n
                # Reset all agent states to idle for the retry
                for aid in _run_state["agent_states"]:
                    _run_state["agent_states"][aid] = "idle"
                _run_state["logs"].append(
                    f"[WATCHDOG] Stall detected — retry {retry_n}/{_main_module.MAX_RETRIES}. "
                    "Restarting crew…"
                )
        else:
            with _state_lock:
                _run_state["agent_states"][node_id] = state
                label = "Approval Gate" if node_id == "gate" else node_id
                _run_state["logs"].append(f"[Gate] {label} → {state}")

    _main_module._state_hook = _pipeline_hook

    old_stdout, old_stderr = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = _LogCapture()
    try:
        run_pipeline(params)
        with _state_lock:
            for aid in _run_state["agent_states"]:
                if _run_state["agent_states"][aid] == "active":
                    _run_state["agent_states"][aid] = "done"
    except Exception as e:
        with _state_lock:
            _run_state["error"] = str(e)
            _run_state["logs"].append(f"ERROR: {e}")
    finally:
        sys.stdout, sys.stderr = old_stdout, old_stderr
        _main_module._state_hook = None
        with _state_lock:
            _run_state["running"] = False


@app.post("/run-analysis")
async def trigger_analysis(body: ScanParams, background_tasks: BackgroundTasks):
    with _state_lock:
        if _run_state["running"]:
            return {"status": "already_running", "message": "Analysis already in progress."}
    background_tasks.add_task(_run_with_logging, body.model_dump())
    return {"status": "started", "message": "Analysis started."}


@app.get("/status")
async def get_status():
    import time as _time
    report_path = Path("data/report.json")
    with _state_lock:
        start = _run_state["start_ts"]
        elapsed = round(_time.monotonic() - start) if start is not None else 0
        return {
            "running":      _run_state["running"],
            "report_ready": report_path.exists(),
            "error":        _run_state["error"],
            "logs":         list(_run_state["logs"])[-80:],
            "agent_states": dict(_run_state["agent_states"]),
            "timed_out":    _run_state["timed_out"],
            "retry_count":  _run_state["retry_count"],
            "elapsed_secs": elapsed,
        }


@app.get("/report")
async def get_report():
    report_path = Path("data/report.json")
    if not report_path.exists():
        return {}
    import json
    return json.loads(report_path.read_text())


# Serve dashboard static files
app.mount("/dashboard", StaticFiles(directory="dashboard", html=True), name="dashboard")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
