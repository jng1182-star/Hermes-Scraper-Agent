from fastapi import FastAPI, BackgroundTasks, Query
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator
from main import run_pipeline
import threading
from pathlib import Path
from collections import deque
from typing import Annotated, Dict, List, Optional

_PROJECT_ROOT = Path(__file__).parent

app = FastAPI()

# Restrict CORS to localhost only — this app is local-first and should never be
# reachable from an external origin. A wildcard CORS policy would allow any
# webpage opened in the browser to trigger pipeline runs or read reports.
_ALLOWED_ORIGINS = [
    "http://localhost:8000",
    "http://127.0.0.1:8000",
    "http://localhost:3000",   # dev frontend if running separately
    "http://127.0.0.1:3000",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
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
    "profile baseline scraper": "profile",
    "feed ad capture agent":    "feed",
    "social data scraper":      "scraper",
    "engagement analyst":       "analyst",
    "intelligence reporter":    "reporter",
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
    Wrapped in try/except — CrewAI internal module paths change across minor versions.
    If the import fails, agent state tracking is disabled gracefully (no server crash).
    """
    try:
        from crewai.agent.core import Agent as _Agent
    except (ImportError, AttributeError):
        try:
            from crewai import Agent as _Agent  # fallback: newer CrewAI public import
        except (ImportError, AttributeError):
            return  # agent state tracking unavailable — degraded mode, not fatal

    _orig = getattr(_Agent, "execute_task", None)
    if _orig is None:
        return  # method not present in this version — skip

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
    import io, time as _time
    import logging as _logging

    with _state_lock:
        _run_state["running"]     = True
        _run_state["error"]       = None
        _run_state["timed_out"]   = False
        _run_state["retry_count"] = 0
        _run_state["start_ts"]    = _time.monotonic()
        _run_state["logs"].clear()
        _run_state["agent_states"] = {
            "profile":  "idle",
            "feed":     "idle",
            "scraper":  "idle",   # fallback — only activates if DOM scrapes yield nothing
            "analyst":  "idle",
            "reporter": "idle",
            "gate":     "idle",
        }

    # Capture pipeline log output via a logging handler attached to the root logger
    # for this thread only — avoids replacing the global sys.stdout (thread-unsafe).
    class _QueueHandler(_logging.Handler):
        def emit(self, record):
            msg = self.format(record).rstrip()
            if msg:
                with _state_lock:
                    _run_state["logs"].append(msg)

    _handler = _QueueHandler()
    _handler.setFormatter(_logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    _root_logger = _logging.getLogger()
    _root_logger.addHandler(_handler)

    import main as _main_module

    def _pipeline_hook(node_id: str, state: str):
        if node_id == "__stall__":
            retry_n = int(state.split(":")[1]) if ":" in state else 1
            with _state_lock:
                _run_state["timed_out"]   = True
                _run_state["retry_count"] = retry_n
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
        _root_logger.removeHandler(_handler)
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
    report_path = _PROJECT_ROOT / "data" / "report.json"
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
    report_path = _PROJECT_ROOT / "data" / "report.json"
    if not report_path.exists():
        return {}
    import json
    return json.loads(report_path.read_text())


class SimulateParams(BaseModel):
    adjustments: Dict[str, float]  # {"BrandName": multiplier}  e.g. {"Nike": 1.5, "Adidas": 0.7}

    @field_validator("adjustments")
    @classmethod
    def validate_adjustments(cls, v: Dict[str, float]) -> Dict[str, float]:
        if len(v) > 50:
            raise ValueError("Too many brands in adjustments (max 50).")
        for brand, mult in v.items():
            if not isinstance(brand, str) or len(brand) > 120:
                raise ValueError(f"Brand name too long or invalid: {brand!r}")
            if not (0.0 < mult <= 20.0):
                raise ValueError(
                    f"Multiplier for '{brand}' must be between 0 (exclusive) and 20. Got: {mult}"
                )
        return v


@app.post("/simulate")
async def simulate_scenario(body: SimulateParams):
    """
    What-if SoS scenario: adjust brand spend weights and recalculate Share of Spend.

    POST body:
      {"adjustments": {"BrandA": 1.5, "BrandB": 0.7}}
      Multipliers: 1.0 = baseline, 2.0 = doubled, 0.5 = halved.
      Brands not listed default to 1.0 (no change).

    Returns:
      Full competitors array with updated sos_pct, sos_delta_pct, and scenario notes.
      Includes methodology block and per-brand mover summary.

    Methodology: ceteris paribus — other brands hold spend constant.
    Consistent with Nielsen Optimizer / Kantar XTEL scenario planning approaches.
    """
    report_path = _PROJECT_ROOT / "data" / "report.json"
    if not report_path.exists():
        return {"error": "No report available. Run an analysis first."}

    import json
    from core.scenario_sim import scenario_summary
    report = json.loads(report_path.read_text())
    competitors = report.get("competitors", [])
    if not competitors:
        return {"error": "Report contains no competitor data."}

    return scenario_summary(brands=competitors, adjustments=body.adjustments)


@app.get("/history")
async def get_sos_history(
    market:   Optional[str] = Query(default=None, max_length=100,  description="Filter by market name, e.g. 'Philippines'"),
    industry: Optional[str] = Query(default=None, max_length=50,   description="Filter by industry key, e.g. 'beauty'"),
    limit:    int           = Query(default=100,  ge=1, le=1000,   description="Max rows (1–1000)"),
):
    """
    Return time-series SoS snapshots from the SQLite history database.

    Each row represents one brand's SoS/SoV figures for a specific pipeline run.
    Use the run_date and run_id fields to group rows into a single analysis run.

    Query params:
      market   — filter by market name (e.g. "Philippines")
      industry — filter by industry key (e.g. "beauty")
      limit    — max rows (default 100)
    """
    from data.sos_db import SosDB
    rows = SosDB().get_history(market=market or "", industry=industry or "", limit=limit)
    return {
        "count": len(rows),
        "market_filter":   market,
        "industry_filter": industry,
        "snapshots": rows,
    }


# Serve dashboard static files
app.mount("/dashboard", StaticFiles(directory="dashboard", html=True), name="dashboard")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
