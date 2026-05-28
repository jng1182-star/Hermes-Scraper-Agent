from contextlib import asynccontextmanager
from fastapi import FastAPI, BackgroundTasks, Query, Request
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field, field_validator
from main import run_pipeline
import json, threading, datetime
from pathlib import Path
from collections import deque
from typing import Annotated, Dict, List, Optional

_PROJECT_ROOT = Path(__file__).parent


def _should_refresh_benchmarks() -> bool:
    bench_path = _PROJECT_ROOT / "data" / "benchmarks.json"
    if not bench_path.exists():
        return True
    try:
        data = json.loads(bench_path.read_text())
        updated_at = data.get("updated_at", "")
        if not updated_at:
            return True
        dt = datetime.datetime.fromisoformat(updated_at)
        age_days = (datetime.datetime.now(datetime.timezone.utc) - dt).days
        return age_days > 7
    except Exception:
        return True


def _bg_refresh_benchmarks():
    try:
        from scripts.refresh_benchmarks import run_refresh
        run_refresh()
        print("[BenchmarkRefresh] Startup refresh complete.", flush=True)
    except Exception as e:
        print(f"[BenchmarkRefresh] Startup refresh failed: {e}", flush=True)


@asynccontextmanager
async def lifespan(app):
    if _should_refresh_benchmarks():
        threading.Thread(target=_bg_refresh_benchmarks, daemon=True).start()
    yield


app = FastAPI(lifespan=lifespan)

# Allow Railway public URL + localhost. Wildcard is safe here because the Railway
# app itself is the consumer — there's no sensitive user auth cookie to steal.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type"],
)

# Shared state
_run_state = {
    "running": False,
    "logs": [],                          # plain list — must stay list; tap thread slices it
    "sentinel_logs": deque(maxlen=500),
    "active_flags": {},
    "sentinel_directives": {},           # written by sentinel actions, read by crew phases
    "sentinel_coverage_gaps": [],
    "active_phase": "",
    "scraped_baselines": {},             # brand:platform → er_threshold from successful scrapes
    "synthetic_baselines": {},           # brand:platform → sentinel-derived baseline
    "error": None,
    "agent_states": {},
    "timed_out": False,
    "retry_count": 0,
    "start_ts": None,
}
_state_lock   = threading.Lock()
_stop_flag    = threading.Event()        # set by /stop-analysis
_worker_thread: Optional[threading.Thread] = None  # reference to running pipeline thread

# Map CrewAI agent roles → dashboard node IDs (current + legacy aliases)
_ROLE_TO_NODE = {
    # Current role strings (agents.py v4.2)
    "profile scraper":           "profile",
    "ad library collector":      "feed",
    "social data researcher":    "scraper",
    "share-of-voice analyst":    "analyst",
    "sov intelligence reporter": "reporter",
    "approval gate":             "gate",
    # Legacy / alias names kept for backwards compat
    "profile baseline scraper":  "profile",
    "brand profile collector":   "profile",
    "feed ad capture agent":     "feed",
    "feed scroller":             "feed",
    "in-feed ad collector":      "feed",
    "social data scraper":       "scraper",
    "engagement analyst":        "analyst",
    "intelligence reporter":     "reporter",
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
        from crewai import Agent as _Agent
    except (ImportError, AttributeError):
        try:
            from crewai.agent.agent import Agent as _Agent
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
    global _worker_thread
    import io, os as _os, time as _time
    import logging as _logging

    _stop_flag.clear()
    _worker_thread = threading.current_thread()

    # Re-assert Ollama routing — Railway injects OPENAI_BASE_URL into the process env
    # after startup; litellm reads env at call-time, not import-time.
    _wk_ollama = _os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
    _wk_v1     = _wk_ollama + "/v1"
    for _stale in ("OPENAI_BASE_URL", "OPENAI_API_BASE", "BASE_URL", "API_BASE",
                   "http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                   "ALL_PROXY", "all_proxy"):
        _os.environ.pop(_stale, None)
    _os.environ["OPENAI_API_KEY"]  = "ollama"
    _os.environ["OPENAI_BASE_URL"] = _wk_v1
    _os.environ["OPENAI_API_BASE"] = _wk_v1

    with _state_lock:
        _run_state["running"]               = True
        _run_state["error"]                 = None
        _run_state["timed_out"]             = False
        _run_state["retry_count"]           = 0
        _run_state["start_ts"]              = _time.monotonic()
        _run_state["logs"].clear()
        _run_state["sentinel_logs"]         = deque(maxlen=500)
        _run_state["active_flags"]          = {}
        _run_state["sentinel_directives"]   = {}
        _run_state["sentinel_coverage_gaps"] = []
        _run_state["active_phase"]          = ""
        _run_state["scraped_baselines"]     = {}
        _run_state["synthetic_baselines"]   = {}
        _run_state["agent_states"] = {
            "profile":  "idle",
            "feed":     "idle",
            "scraper":  "idle",   # fallback — only activates if DOM scrapes yield nothing
            "analyst":  "idle",
            "reporter": "idle",
            "gate":     "idle",
        }

    # Capture stdout/stderr (CrewAI verbose print() output) into _run_state["logs"]
    import sys as _sys, re as _re
    _ANSI_RE = _re.compile(r"\x1b\[[0-9;]*m")

    class _Cap(io.TextIOBase):
        def write(self, s):
            clean = _ANSI_RE.sub("", s).rstrip()
            if clean:
                with _state_lock:
                    _run_state["logs"].append(clean)
            if _stop_flag.is_set():
                raise RuntimeError("Analysis stopped by user.")
            return len(s)
        def flush(self): pass

    _old_out, _old_err = _sys.stdout, _sys.stderr
    _sys.stdout = _sys.stderr = _Cap()

    # Also hook Python logging → same bucket
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
        if _stop_flag.is_set() or "stopped by user" in str(e).lower():
            with _state_lock:
                _run_state["logs"].append("[STOP] Analysis cancelled by user.")
                for aid in _run_state["agent_states"]:
                    _run_state["agent_states"][aid] = "idle"
        else:
            with _state_lock:
                _run_state["error"] = str(e)
                _run_state["logs"].append(f"ERROR: {e}")
                for aid in _run_state["agent_states"]:
                    _run_state["agent_states"][aid] = "idle"
    finally:
        _sys.stdout = _old_out
        _sys.stderr = _old_err
        _root_logger.removeHandler(_handler)
        _main_module._state_hook = None
        _stop_flag.clear()
        _worker_thread = None
        with _state_lock:
            _run_state["running"] = False


@app.get("/")
async def root():
    return RedirectResponse(url="/dashboard/index.html")


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
            "running":       _run_state["running"],
            "report_ready":  report_path.exists(),
            "error":         _run_state["error"],
            "logs":          list(_run_state["logs"])[-100:],
            "sentinel_logs": list(_run_state.get("sentinel_logs", []))[-200:],
            "active_flags":  dict(_run_state.get("active_flags", {})),
            "agent_states":  dict(_run_state["agent_states"]),
            "timed_out":     _run_state["timed_out"],
            "retry_count":   _run_state["retry_count"],
            "elapsed_secs":  elapsed,
        }


class SentinelOverrideRequest(BaseModel):
    flag_id: str
    reason: str = "Approval Gate override via dashboard."


@app.post("/sentinel-override")
async def sentinel_override(req: SentinelOverrideRequest):
    if not req.flag_id:
        return JSONResponse(status_code=400, content={"error": "flag_id required"})
    try:
        from approval_gate import register_override
        register_override(req.flag_id, req.reason)
        with _state_lock:
            flags = _run_state.get("active_flags", {})
            if req.flag_id in flags:
                flags[req.flag_id]["resolved"]   = True
                flags[req.flag_id]["overridden"]  = True
        return {"status": "override_sent", "flag_id": req.flag_id}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


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


_SAVED_RUNS_PATH = _PROJECT_ROOT / "data" / "saved_runs.json"
_saved_runs_lock = threading.Lock()


def _load_saved_runs() -> dict:
    with _saved_runs_lock:
        if _SAVED_RUNS_PATH.exists():
            try:
                return json.loads(_SAVED_RUNS_PATH.read_text())
            except Exception:
                pass
        return {"runs": []}


def _save_saved_runs(payload: dict):
    with _saved_runs_lock:
        _SAVED_RUNS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _SAVED_RUNS_PATH.write_text(json.dumps(payload))


@app.get("/uploaded-files")
async def get_uploaded_files():
    up_dir = _PROJECT_ROOT / "data" / "uploads"
    files = []
    if up_dir.exists():
        for f in sorted(up_dir.iterdir()):
            if f.is_file() and not f.name.startswith("."):
                files.append({"name": f.name, "size": f.stat().st_size})
    return {"files": files}


@app.get("/saved-runs")
async def get_saved_runs():
    return _load_saved_runs()


@app.post("/saved-runs")
async def save_run(request: Request):
    entry = await request.json()
    if not entry.get("id") or not entry.get("report"):
        return JSONResponse(status_code=400, content={"error": "missing id or report"})
    runs = _load_saved_runs().get("runs", [])
    runs = [r for r in runs if r.get("id") != entry["id"]]
    runs.insert(0, entry)
    if len(runs) > 50:
        runs = runs[:50]
    _save_saved_runs({"runs": runs})
    return {"status": "ok", "count": len(runs)}


@app.delete("/saved-runs")
async def clear_saved_runs():
    _save_saved_runs({"runs": []})
    return {"status": "ok"}


@app.delete("/saved-runs/{run_id}")
async def delete_saved_run(run_id: str):
    runs = [r for r in _load_saved_runs().get("runs", []) if str(r.get("id")) != run_id]
    _save_saved_runs({"runs": runs})
    return {"status": "ok", "count": len(runs)}


@app.post("/refresh-benchmarks")
async def refresh_benchmarks_endpoint(background_tasks: BackgroundTasks):
    background_tasks.add_task(_bg_refresh_benchmarks)
    return {"status": "refresh_started"}


@app.get("/benchmarks-status")
async def benchmarks_status():
    bench_path = _PROJECT_ROOT / "data" / "benchmarks.json"
    if not bench_path.exists():
        return {"updated_at": None, "age_days": None, "sources": [], "status": "never_refreshed"}
    try:
        data = json.loads(bench_path.read_text())
        updated_at = data.get("updated_at")
        age_days = None
        if updated_at:
            dt = datetime.datetime.fromisoformat(updated_at)
            age_days = (datetime.datetime.now(datetime.timezone.utc) - dt).days
        return {
            "updated_at": updated_at,
            "age_days":   age_days,
            "sources":    data.get("sources", []),
            "status":     "ok",
        }
    except Exception as e:
        return {"updated_at": None, "age_days": None, "sources": [], "status": f"error: {e}"}


# ── Railway-aware stubs for features only available in local server.py deploy ──

_NOT_AVAILABLE = JSONResponse(
    status_code=200,
    content={"ok": False, "error": "Not available in Railway deploy."},
)

@app.post("/stop-analysis")
async def stop_analysis():
    if not _run_state.get("running"):
        return {"ok": True, "status": "not_running"}

    # 1. Set flag — _Cap.write() raises RuntimeError on next print() call
    _stop_flag.set()

    # 2. ctypes interrupt — raises RuntimeError inside the worker thread immediately,
    #    even if it's blocked in a Python call (LLM wait, asyncio, etc.)
    t = _worker_thread
    if t and t.is_alive():
        try:
            import ctypes
            ctypes.pythonapi.PyThreadState_SetAsyncExc(
                ctypes.c_ulong(t.ident),
                ctypes.py_object(RuntimeError),
            )
        except Exception:
            pass

    with _state_lock:
        _run_state["logs"].append("[STOP] Stop requested by user.")

    return {"ok": True, "status": "stop_requested"}


@app.get("/proxy-status")
@app.post("/start-proxy")
@app.post("/stop-proxy")
@app.get("/tunnel-status")
@app.post("/start-tunnel")
@app.post("/stop-tunnel")
async def proxy_tunnel_stub():
    """Proxy/tunnel management is local-only. Not available on Railway."""
    return _NOT_AVAILABLE


@app.post("/upload-file")
async def upload_file_stub():
    """File upload not available on Railway deploy."""
    return _NOT_AVAILABLE


# Serve dashboard static files
app.mount("/dashboard", StaticFiles(directory="dashboard", html=True), name="dashboard")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
