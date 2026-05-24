import os
import shutil
import subprocess
import threading
import time
import json
from pathlib import Path
from dotenv import load_dotenv

# Load .env FIRST so OLLAMA_HOST is available before any crewai/litellm import
load_dotenv()

# Ensure OLLAMA_HOST is set before crewai is imported (native provider reads it)
_ollama_host = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
os.environ["OLLAMA_HOST"] = _ollama_host

from crew import SocialListeningCrew, clear_checkpoints
from approval_gate import ApprovalGate

# Injected by server.py before each run
_state_hook = None  # callable(node_id: str, state: str) | None

STALL_TIMEOUT  = int(os.getenv("STALL_TIMEOUT",    "180"))
MAX_RETRIES    = int(os.getenv("STALL_MAX_RETRIES", "2"))
GATE_TIMEOUT   = int(os.getenv("GATE_TIMEOUT",      "120"))

# Resolve ollama binary path once at startup — avoids PATH issues when running via venv.
# Falls back gracefully: heartbeat and watchdog will skip CLI calls if binary not found.
_OLLAMA_BIN = (
    shutil.which("ollama")
    or "/usr/local/bin/ollama"    # homebrew default
    or "/opt/homebrew/bin/ollama" # Apple Silicon homebrew
)
if not Path(_OLLAMA_BIN).exists():
    _OLLAMA_BIN = None


SUPPORTED_PLATFORMS = ["TikTok", "Instagram", "YouTube", "Facebook"]


def _build_query(params: dict) -> str:
    # Support both single advertiser and list of advertisers
    advertisers  = params.get("advertisers") or []
    if not advertisers and params.get("advertiser"):
        advertisers = [params["advertiser"].strip()]
    advertisers = [a.strip() for a in advertisers if str(a).strip()]

    competitors = params.get("competitors", [])
    country     = params.get("country", "").strip()
    platforms   = [p for p in params.get("platforms", []) if p in SUPPORTED_PLATFORMS]
    date_range  = params.get("date_range", "Last 30 days").strip()
    keywords    = params.get("keywords", "").strip()
    post_type   = params.get("post_type", "both")

    parts = []
    if advertisers:
        parts.append(", ".join(advertisers))
    if competitors:
        s = ", ".join(c.strip() for c in competitors if c.strip())
        if s:
            parts.append(f"vs {s}")
    if country:
        parts.append(f"in {country}")
    if platforms:
        parts.append(f"on {', '.join(platforms)}")
    if date_range:
        parts.append(f"({date_range})")
    if keywords:
        parts.append(f"[topics: {keywords}]")
    if post_type and post_type != "both":
        parts.append(f"[{post_type} posts only]")

    return " ".join(parts) if parts else "Top Brands"


def _kill_ollama_runner():
    try:
        r = subprocess.run(["pgrep", "-f", "ollama runner"], capture_output=True, text=True)
        for pid_str in r.stdout.strip().splitlines():
            try:
                os.kill(int(pid_str), 2)  # SIGINT=2, no signal module
                print(f"[WATCHDOG] Sent SIGINT to Ollama runner PID {pid_str}.", flush=True)
            except (ValueError, ProcessLookupError):
                pass
    except Exception as e:
        print(f"[WATCHDOG] pgrep failed: {e}", flush=True)
    if _OLLAMA_BIN:
        try:
            subprocess.run([_OLLAMA_BIN, "stop", "gemma4:e4b"],  capture_output=True, timeout=10)
            subprocess.run([_OLLAMA_BIN, "stop", "gemma4:26b"], capture_output=True, timeout=10)
        except Exception:
            pass


# No hard deadline — pipeline runs until complete. Stall watchdog handles frozen LLMs.


def run_pipeline(params: dict):
    query     = _build_query(params)
    cpm_rate  = float(params.get("cpm_rate", 0)) or None  # None → auto (market×industry×seasonal)
    depth     = params.get("depth", "deep")
    post_type = params.get("post_type", "both")
    country   = params.get("country", "").strip()
    industry  = params.get("industry", "").strip()

    # Normalise advertisers list
    advertisers = params.get("advertisers") or []
    if not advertisers and params.get("advertiser"):
        advertisers = [params["advertiser"].strip()]
    params["advertisers"] = [a.strip() for a in advertisers if str(a).strip()]

    # Enforce supported platforms only
    params["platforms"] = [p for p in params.get("platforms", []) if p in SUPPORTED_PLATFORMS]

    # Load any parsed uploaded files and inject as context
    parsed_dir = Path("data/parsed")
    uploaded_context = []
    if parsed_dir.exists():
        for f in sorted(parsed_dir.iterdir()):
            if f.is_file() and f.suffix == ".txt":
                try:
                    content = f.read_text(encoding="utf-8")[:4000]  # cap per file
                    uploaded_context.append({"filename": f.name, "content": content})
                except Exception:
                    pass
    if uploaded_context:
        params["uploaded_context"] = uploaded_context

    print(f"Starting Analysis: {query}", flush=True)
    # Fresh run — clear any stale checkpoints from a previous session
    clear_checkpoints()

    # No hard deadline — let the pipeline run as long as needed.
    # Stall watchdog (below) handles genuinely frozen LLM calls.
    _pipeline_start  = time.monotonic()
    _crew_thread_id  = [threading.current_thread().ident]

    # ── Ollama heartbeat — ping HTTP health endpoint; restart CLI only if binary available ──
    _heartbeat_stop = threading.Event()

    def _ollama_heartbeat(stop_evt: threading.Event):
        import urllib.request
        import urllib.error
        consecutive_fails = 0
        while not stop_evt.wait(timeout=60):
            try:
                # Prefer HTTP health check — works regardless of PATH / binary location.
                # Ollama exposes GET / → 200 "Ollama is running" when healthy.
                with urllib.request.urlopen(
                    f"{_ollama_host}/", timeout=8
                ) as resp:
                    if resp.status == 200:
                        consecutive_fails = 0
                        continue
                consecutive_fails += 1
            except urllib.error.URLError:
                consecutive_fails += 1
            except Exception:
                consecutive_fails += 1

            if consecutive_fails >= 2:
                print("[HEARTBEAT] Ollama not responding — attempting restart…", flush=True)
                if _OLLAMA_BIN:
                    try:
                        subprocess.Popen(
                            [_OLLAMA_BIN, "serve"],
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                        time.sleep(5)
                    except Exception as e:
                        print(f"[HEARTBEAT] Restart failed: {e}", flush=True)
                else:
                    print(
                        "[HEARTBEAT] ollama binary not found — cannot auto-restart. "
                        "Start Ollama manually if it has crashed.",
                        flush=True,
                    )
                consecutive_fails = 0

    heartbeat_thread = threading.Thread(
        target=_ollama_heartbeat, args=(_heartbeat_stop,),
        daemon=True, name="ollama-heartbeat",
    )
    heartbeat_thread.start()

    # ── Stall watchdog ────────────────────────────────────────────────────────
    _last_token_ts  = [time.monotonic()]
    _watchdog_stop  = threading.Event()
    _stall_flag     = threading.Event()

    def _watchdog_loop(stop_evt: threading.Event):
        while not stop_evt.wait(timeout=15):
            idle = time.monotonic() - _last_token_ts[0]
            if idle >= STALL_TIMEOUT:
                print(f"\n[WATCHDOG] No tokens for {idle:.0f}s — stalled. Aborting…\n", flush=True)
                _stall_flag.set()
                _kill_ollama_runner()
                tid = _crew_thread_id[0]
                if tid:
                    try:
                        import ctypes
                        ctypes.pythonapi.PyThreadState_SetAsyncExc(
                            ctypes.c_ulong(tid),
                            ctypes.py_object(RuntimeError),
                        )
                    except Exception:
                        pass
                return

    # Reset watchdog timer on every LLM token
    try:
        from crewai.events.event_bus import crewai_event_bus as _eb
        from crewai.events.types.llm_events import LLMStreamChunkEvent as _ChunkEvt

        @_eb.on(_ChunkEvt)
        def _on_chunk(source, event):
            _last_token_ts[0] = time.monotonic()
    except Exception:
        pass

    # ── Retry loop with checkpoint resume + model fallback ───────────────────
    raw_output     = None
    _attempt       = 0
    _current_depth = depth

    while _attempt <= MAX_RETRIES:
        # On second retry, fall back to faster model
        if _attempt > 0 and _current_depth == "deep":
            _current_depth = "quick"
            print(f"\n[WATCHDOG] Switching to quick model (e4b) for retry {_attempt}…\n", flush=True)

        _stall_flag.clear()
        _last_token_ts[0] = time.monotonic()
        _watchdog_stop.clear()

        watchdog = threading.Thread(
            target=_watchdog_loop, args=(_watchdog_stop,),
            daemon=True, name="stall-watchdog",
        )
        watchdog.start()

        # Resume from checkpoint on retries (skip agents that already completed)
        is_resume = _attempt > 0
        if is_resume:
            print(f"\n[WATCHDOG] Retry {_attempt}/{MAX_RETRIES} — resuming from last checkpoint…\n", flush=True)

        try:
            crew_instance = SocialListeningCrew(
                query, depth=_current_depth, params=params, resume=is_resume
            )
            raw_output    = crew_instance.run()
            _watchdog_stop.set()
            break

        except Exception as exc:
            _watchdog_stop.set()
            time.sleep(0.1)

            # Propagate user-stop immediately (no retry on explicit stop)
            if "stopped by user" in str(exc).lower():
                raise

            is_stall = (
                _stall_flag.is_set()
                or "None or empty" in str(exc)
                or "Invalid response from LLM" in str(exc)
                or "ngrok" in str(exc).lower()
                or "ERR_NGROK" in str(exc)
                or "incomplete HTTP response" in str(exc)
            )
            if is_stall:
                _attempt += 1
                if _attempt <= MAX_RETRIES:
                    if _state_hook:
                        _state_hook("__stall__", f"retry:{_attempt}")
                    time.sleep(3)
                    continue
                else:
                    raise RuntimeError(
                        f"Analysis stalled {MAX_RETRIES} times. "
                        "Try Quick mode or close other apps to free VRAM."
                    )
            raise

    _heartbeat_stop.set()  # stop Ollama heartbeat

    # ── Approval Gate ────────────────────────────────────────────────────────
    if _state_hook:
        _state_hook("gate", "active")
    print("Running Approval Gate…", flush=True)
    gate = ApprovalGate(cpm_rate=cpm_rate, post_type=post_type, country=country, industry=industry)

    import concurrent.futures as _cf
    with _cf.ThreadPoolExecutor(max_workers=1) as _pool:
        _fut = _pool.submit(gate.process_final_report, str(raw_output))
        try:
            final_json_str = _fut.result(timeout=GATE_TIMEOUT)
        except _cf.TimeoutError:
            raise RuntimeError(
                f"[PHASE TIMEOUT] Approval Gate exceeded {GATE_TIMEOUT}s. "
                "Raw output saved — check data/report_raw.txt."
            )

    if _state_hook:
        _state_hook("gate", "done")

    try:
        report = json.loads(final_json_str)
        report["scan_params"] = params
        final_json_str = json.dumps(report, indent=2)
    except Exception:
        pass

    os.makedirs("data", exist_ok=True)
    with open("data/report.json", "w") as f:
        f.write(final_json_str)

    print("Pipeline complete. Report saved.", flush=True)
    return True


if __name__ == "__main__":
    import sys
    p = {
        "advertiser": sys.argv[1] if len(sys.argv) > 1 else "Nike",
        "competitors": ["Adidas", "Puma"],
        "country":    "Singapore",
        "platforms":  ["TikTok", "Instagram"],
        "date_range": "Last 30 days",
        "cpm_rate":   15.0,
        "keywords":   "",
    }
    run_pipeline(p)
