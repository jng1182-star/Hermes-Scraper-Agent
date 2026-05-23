"""
Hermes — HTTP server (stdlib only, no FastAPI/uvicorn).
Serves the dashboard at /dashboard/ and provides a JSON API.
Runs the HTTP server in a daemon thread; main thread stays free for signals.
"""
import json
import os
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Optional

# ── Set Ollama base BEFORE litellm/crewai are imported ───────────────────────
# litellm reads OLLAMA_API_BASE at import time; must be set early.
_ollama_host = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
os.environ["OLLAMA_API_BASE"] = _ollama_host
# Also suppress OpenAI key warnings — we are not using OpenAI
os.environ.setdefault("OPENAI_API_KEY", "sk-placeholder-not-used")

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))

# ── Shared run state ──────────────────────────────────────────────────────────
_run_state = {
    "running":      False,
    "logs":         deque(maxlen=300),
    "error":        None,
    "agent_states": {},
    "timed_out":    False,
    "retry_count":  0,
    "start_ts":     None,
}
_state_lock = threading.Lock()
_stop_flag  = threading.Event()   # set to request graceful abort
_worker_thread_id: Optional[int] = None
_worker_thread: Optional[threading.Thread] = None   # reference for join/kill


def _kill_ollama_now():
    """Kill the Ollama runner process to unblock any in-flight LLM call."""
    import subprocess
    try:
        r = subprocess.run(["pgrep", "-f", "ollama runner"], capture_output=True, text=True)
        for pid_str in r.stdout.strip().splitlines():
            try:
                os.kill(int(pid_str), 9)   # SIGKILL — instant
            except (ValueError, ProcessLookupError):
                pass
    except Exception:
        pass
    # Also stop model via ollama CLI (best-effort)
    try:
        subprocess.run(["ollama", "stop", "gemma4:e4b"],  capture_output=True, timeout=3)
        subprocess.run(["ollama", "stop", "gemma4:26b"], capture_output=True, timeout=3)
    except Exception:
        pass

# Map CrewAI Agent.role → dashboard node IDs
_ROLE_TO_NODE = {
    "social data scraper": "scraper",
    "engagement analyst":  "analyst",
    "intelligence reporter": "reporter",
}


def _set_agent_active(node_id: str, label: str = ""):
    with _state_lock:
        for aid, s in _run_state["agent_states"].items():
            if s == "active":
                _run_state["agent_states"][aid] = "done"
        _run_state["agent_states"][node_id] = "active"
        _run_state["logs"].append(f"[Agent] {label or node_id} → active")


def _set_agent_done(node_id: str, label: str = ""):
    with _state_lock:
        _run_state["agent_states"][node_id] = "done"
        _run_state["logs"].append(f"[Agent] {label or node_id} → done")


def _patch_crewai_agent():
    """Monkey-patch Agent.execute_task to fire state hooks synchronously."""
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


# ── Pipeline runner ───────────────────────────────────────────────────────────

def _pipeline_hook(node_id: str, state: str):
    """Called from main.py for gate state and stall events."""
    if node_id == "__stall__":
        retry_n = int(state.split(":")[1]) if ":" in state else 1
        with _state_lock:
            _run_state["timed_out"]   = True
            _run_state["retry_count"] = retry_n
            for aid in _run_state["agent_states"]:
                _run_state["agent_states"][aid] = "idle"
            _run_state["logs"].append(
                f"[WATCHDOG] Stall detected — retry {retry_n}. Restarting…"
            )
    else:
        with _state_lock:
            _run_state["agent_states"][node_id] = state
            _run_state["logs"].append(
                f"[Gate] Approval Gate → {state}"
            )


def _run_worker(params: dict):
    import sys
    import io
    import ctypes
    import main as _main

    global _worker_thread_id, _worker_thread
    _stop_flag.clear()
    _worker_thread_id  = threading.current_thread().ident
    _worker_thread     = threading.current_thread()

    with _state_lock:
        _run_state.update(
            running=True, error=None, timed_out=False, retry_count=0,
            start_ts=time.monotonic(),
            agent_states={"scraper": "idle", "analyst": "idle",
                          "reporter": "idle", "gate": "idle"},
        )
        _run_state["logs"].clear()

    class _Cap(io.TextIOBase):
        def write(self, s):
            if s.strip():
                with _state_lock:
                    _run_state["logs"].append(s.rstrip())
                # Check stop flag on every write
                if _stop_flag.is_set():
                    raise RuntimeError("Analysis stopped by user.")
            return len(s)
        def flush(self): pass

    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = _Cap()
    _main._state_hook = _pipeline_hook
    try:
        _main.run_pipeline(params)
        with _state_lock:
            for aid, s in _run_state["agent_states"].items():
                if s == "active":
                    _run_state["agent_states"][aid] = "done"
    except (RuntimeError, SystemExit) as e:
        if _stop_flag.is_set() or "stopped by user" in str(e).lower():
            with _state_lock:
                _run_state["logs"].append("[STOP] Analysis cancelled by user.")
                for aid in _run_state["agent_states"]:
                    _run_state["agent_states"][aid] = "idle"
        else:
            with _state_lock:
                _run_state["error"] = str(e)
                _run_state["logs"].append(f"ERROR: {e}")
    except Exception as e:
        if _stop_flag.is_set():
            with _state_lock:
                _run_state["logs"].append("[STOP] Analysis cancelled by user.")
                for aid in _run_state["agent_states"]:
                    _run_state["agent_states"][aid] = "idle"
        else:
            with _state_lock:
                _run_state["error"] = str(e)
                _run_state["logs"].append(f"ERROR: {e}")
    finally:
        sys.stdout, sys.stderr = old_out, old_err
        _main._state_hook = None
        _worker_thread_id = None
        _worker_thread    = None
        _stop_flag.clear()
        with _state_lock:
            _run_state["running"] = False


# ── HTTP server ───────────────────────────────────────────────────────────────

DASHBOARD_DIR = Path(__file__).parent / "dashboard"


class _ThreadingServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):

    def do_GET(self):
        p = self.path.split("?")[0]

        if p == "/status":
            report_path = Path("data/report.json")
            with _state_lock:
                start = _run_state["start_ts"]
                elapsed = round(time.monotonic() - start) if start else 0
                payload = {
                    "running":      _run_state["running"],
                    "report_ready": report_path.exists(),
                    "error":        _run_state["error"],
                    "logs":         list(_run_state["logs"])[-100:],
                    "agent_states": dict(_run_state["agent_states"]),
                    "timed_out":    _run_state["timed_out"],
                    "retry_count":  _run_state["retry_count"],
                    "elapsed_secs": elapsed,
                }
            self._json(200, payload)

        elif p == "/report":
            rp = Path("data/report.json")
            if rp.exists():
                self._json(200, json.loads(rp.read_text()))
            else:
                self._json(200, {})

        elif p.startswith("/dashboard/") or p == "/dashboard":
            rel = p.removeprefix("/dashboard/") or "index.html"
            file_path = DASHBOARD_DIR / rel
            if file_path.exists() and file_path.is_file():
                ct = _mime(file_path.suffix)
                data = file_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", ct)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                self.send_error(404)
        else:
            self.send_error(404)

    def do_POST(self):
        p = self.path.split("?")[0]
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length).decode() if length else "{}"
        try:
            data = json.loads(body)
        except Exception:
            self._json(400, {"error": "bad json"}); return

        if p == "/run-analysis":
            with _state_lock:
                if _run_state["running"]:
                    self._json(200, {"status": "already_running"}); return
            t = threading.Thread(target=_run_worker, args=(data,), daemon=True)
            t.start()
            self._json(200, {"status": "started"})

        elif p == "/stop-analysis":
            import ctypes
            with _state_lock:
                is_running = _run_state["running"]
            if is_running:
                _stop_flag.set()
                # 1. Kill Ollama runner to unblock any in-flight LLM call
                _kill_ollama_now()
                # 2. Inject async exception into the worker thread
                tid = _worker_thread_id
                if tid:
                    try:
                        ctypes.pythonapi.PyThreadState_SetAsyncExc(
                            ctypes.c_ulong(tid),
                            ctypes.py_object(SystemExit),  # SystemExit is harder to swallow
                        )
                    except Exception:
                        pass
                # 3. Keep injecting every 500ms in background until thread dies
                wt = _worker_thread
                def _force_stop(tid=tid, wt=wt):
                    for _ in range(20):   # up to 10s
                        time.sleep(0.5)
                        if wt is None or not wt.is_alive():
                            break
                        if tid:
                            try:
                                ctypes.pythonapi.PyThreadState_SetAsyncExc(
                                    ctypes.c_ulong(tid),
                                    ctypes.py_object(SystemExit),
                                )
                            except Exception:
                                pass
                    # Final state cleanup if thread still hasn't ended
                    with _state_lock:
                        if _run_state["running"]:
                            _run_state["running"] = False
                            _run_state["logs"].append("[STOP] Force-stopped by user.")
                            for aid in _run_state["agent_states"]:
                                _run_state["agent_states"][aid] = "idle"
                threading.Thread(target=_force_stop, daemon=True).start()
                self._json(200, {"status": "stopping"})
            else:
                self._json(200, {"status": "not_running"})
        else:
            self.send_error(404)

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def _json(self, code: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def log_message(self, *_):
        pass  # suppress request logging


def _mime(suffix: str) -> str:
    return {
        ".html": "text/html; charset=utf-8",
        ".css":  "text/css",
        ".js":   "application/javascript",
        ".json": "application/json",
        ".png":  "image/png",
        ".svg":  "image/svg+xml",
    }.get(suffix.lower(), "application/octet-stream")


def start(port: int = PORT):
    httpd = _ThreadingServer((HOST, port), Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True, name="http-server")
    t.start()
    print(f"Hermes running at http://{HOST}:{port}/dashboard/index.html", flush=True)
    return httpd


if __name__ == "__main__":
    import webbrowser
    httpd = start()
    webbrowser.open(f"http://{HOST}:{PORT}/dashboard/index.html")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down.")
        httpd.shutdown()
