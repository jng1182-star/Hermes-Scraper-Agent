import os
import subprocess
import threading
import time
import json
from dotenv import load_dotenv
from crew import SocialListeningCrew
from approval_gate import ApprovalGate

load_dotenv()

# Injected by server.py before each run
_state_hook = None  # callable(node_id: str, state: str) | None

STALL_TIMEOUT = int(os.getenv("STALL_TIMEOUT", "180"))
MAX_RETRIES   = int(os.getenv("STALL_MAX_RETRIES", "2"))


def _build_query(params: dict) -> str:
    advertiser  = params.get("advertiser", "").strip()
    competitors = params.get("competitors", [])
    country     = params.get("country", "").strip()
    platforms   = params.get("platforms", [])
    date_range  = params.get("date_range", "Last 30 days").strip()
    keywords    = params.get("keywords", "").strip()

    parts = []
    if advertiser:
        parts.append(advertiser)
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
    try:
        subprocess.run(["ollama", "stop", "gemma4:e4b"],  capture_output=True, timeout=10)
        subprocess.run(["ollama", "stop", "gemma4:26b"], capture_output=True, timeout=10)
    except Exception:
        pass


HARD_DEADLINE = int(os.getenv("HARD_DEADLINE", "570"))   # 9.5 min — leaves 30s for gate


def run_pipeline(params: dict):
    query    = _build_query(params)
    cpm_rate = float(params.get("cpm_rate", 15.0))
    depth    = params.get("depth", "deep")

    print(f"Starting Analysis: {query}", flush=True)

    # ── Hard deadline — kill everything after HARD_DEADLINE seconds ──────────
    _pipeline_start  = time.monotonic()
    _deadline_stop   = threading.Event()
    _deadline_hit    = threading.Event()
    _crew_thread_id  = [threading.current_thread().ident]

    def _deadline_loop(stop_evt: threading.Event):
        if stop_evt.wait(timeout=HARD_DEADLINE):
            return  # cancelled cleanly
        elapsed = time.monotonic() - _pipeline_start
        print(f"\n[DEADLINE] {elapsed:.0f}s elapsed — hard limit reached. Aborting.\n", flush=True)
        _deadline_hit.set()
        _kill_ollama_runner()
        tid = _crew_thread_id[0]
        if tid:
            try:
                import ctypes
                ctypes.pythonapi.PyThreadState_SetAsyncExc(
                    ctypes.c_ulong(tid), ctypes.py_object(RuntimeError),
                )
            except Exception:
                pass

    deadline_thread = threading.Thread(
        target=_deadline_loop, args=(_deadline_stop,),
        daemon=True, name="deadline-watchdog",
    )
    deadline_thread.start()

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

    # ── Retry loop with model fallback ────────────────────────────────────────
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

        try:
            crew_instance = SocialListeningCrew(query, depth=_current_depth, params=params)
            raw_output    = crew_instance.run()
            _watchdog_stop.set()
            break

        except Exception as exc:
            _watchdog_stop.set()
            time.sleep(0.1)

            # Propagate user-stop or deadline-hit immediately
            if _deadline_hit.is_set() or "stopped by user" in str(exc).lower():
                raise

            is_stall = (
                _stall_flag.is_set()
                or "None or empty" in str(exc)
                or "Invalid response from LLM" in str(exc)
            )
            if is_stall:
                _attempt += 1
                if _attempt <= MAX_RETRIES:
                    print(f"\n[WATCHDOG] Retry {_attempt}/{MAX_RETRIES}…\n", flush=True)
                    if _state_hook:
                        _state_hook("__stall__", f"retry:{_attempt}")
                    time.sleep(5)
                    continue
                else:
                    raise RuntimeError(
                        f"Analysis stalled {MAX_RETRIES} times. "
                        "Try Quick mode or close other apps to free VRAM."
                    )
            raise

    _deadline_stop.set()   # cancel deadline timer — we finished in time

    # ── Approval Gate ────────────────────────────────────────────────────────
    if _state_hook:
        _state_hook("gate", "active")
    print("Running Approval Gate…", flush=True)
    gate = ApprovalGate(cpm_rate=cpm_rate)
    final_json_str = gate.process_final_report(str(raw_output))
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
