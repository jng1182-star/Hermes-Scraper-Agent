"""
Sentinel Observer — Nielsen Media Research Director + Code Reviewer.

Runs as a background thread alongside every pipeline phase. Watches for both
technical breaks (empty outputs, parse failures, timeouts) and methodological
breaks (insufficient baseline posts, circular contamination, missing signals,
confidence inflation). All observations stream to the Approval Gate terminal.

Authority hierarchy:
  Sentinel directive → binding per phase
  Approval Gate override → supersedes Sentinel for any flag
  Human operator override → via /sentinel-override POST endpoint

The Sentinel does NOT run as a CrewAI agent. It is a Python observer thread
that subscribes to CrewAI's event bus and reads from a shared observation queue
posted by crew.py phase runners.

Thinking output prefix:  [SENTINEL THINKING]
Flag prefix:             [SENTINEL FLAG]
Directive prefix:        [SENTINEL DIRECTIVE]
Agent response prefix:   [AGENT RESPONSE]
Gate override prefix:    [GATE OVERRIDE]
"""

import json
import queue
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

# ── Constants ────────────────────────────────────────────────────────────────

_MIN_BASELINE_POSTS   = 12    # below this, ER threshold is statistically unreliable
_CONSECUTIVE_EMPTY_THRESHOLD = 3   # three empty batches → likely scraper block
_PAUSE_GATE_TIMEOUT   = 120   # max seconds the phase waits for Sentinel resolution

# Nielsen-calibrated ER multiplier threshold per platform
_PLATFORM_ER_MULTIPLIER = {
    "tiktok":    1.5,   # TikTok organic ER variance is enormous — tighter base, higher percentile
    "facebook":  3.0,   # Facebook consistent organic ER; 3× is appropriate
    "instagram": 3.0,
    "youtube":   2.5,
}
_DEFAULT_ER_MULTIPLIER = 3.0

# Signal coverage floor below which confidence is hard-capped at Low
_SIGNAL_COVERAGE_LOW_CAP = 2   # fewer than 2 of 6 signals → Low regardless of LLM judgment


# ── Data contracts ───────────────────────────────────────────────────────────

@dataclass
class SentinelEvent:
    """Posted by crew.py phase runners into the observation queue."""
    event_type:  str    # phase_start | phase_heartbeat | output_sample |
                        # phase_complete | phase_timeout_warning | data_quality_check
    phase_name:  str    # researcher | profile | feed | analyst | reporter
    timestamp:   float  # time.monotonic()
    payload:     dict   # content varies by event_type


@dataclass
class SentinelFlag:
    """Raised by Sentinel when a technical or methodological issue is detected."""
    flag_id:       str
    phase:         str
    brand:         str
    platform:      str
    issue:         str
    technical:     str   # technical failure description (code reviewer lens)
    methodological: str  # downstream consequence for SOV validity (Nielsen Director lens)
    recommendation: str
    confidence:    str   # HIGH | MEDIUM | LOW — how confident Sentinel is in the flag
    severity:      str   # CRITICAL | WARNING | INFO
    pause_gate:    threading.Event = field(default_factory=threading.Event)
    override_gate: threading.Event = field(default_factory=threading.Event)
    resolved:      bool  = False
    overridden:    bool  = False


# ── Singleton ────────────────────────────────────────────────────────────────

_sentinel_instance: Optional["SentinelObserver"] = None
_sentinel_lock = threading.Lock()


def get_sentinel() -> Optional["SentinelObserver"]:
    return _sentinel_instance


def init_sentinel(
    gate_log_fn: Callable[[str], None],
    flag_fn: Optional[Callable[[dict], None]] = None,
) -> "SentinelObserver":
    global _sentinel_instance
    with _sentinel_lock:
        if _sentinel_instance:
            _sentinel_instance.stop()
        _sentinel_instance = SentinelObserver(gate_log_fn, flag_fn=flag_fn)
    return _sentinel_instance


def reset_sentinel() -> None:
    global _sentinel_instance
    with _sentinel_lock:
        if _sentinel_instance:
            _sentinel_instance.stop()
        _sentinel_instance = None


# ── Role → phase mapping (mirrors server.py _ROLE_TO_NODE) ──────────────────

_ROLE_TO_PHASE = {
    "profile scraper":            "profile",
    "profile baseline scraper":   "profile",
    "brand profile collector":    "profile",
    "feed scroller":              "feed",
    "in-feed ad collector":       "feed",
    "feed ad capture agent":      "feed",
    "ad library collector":       "feed",   # current role name after feed scroller retirement
    "social data researcher":     "researcher",
    "social data scraper":        "researcher",
    "share-of-voice analyst":     "analyst",
    "engagement analyst":         "analyst",
    "sov intelligence reporter":  "reporter",
    "intelligence reporter":      "reporter",
}


# ── Main observer class ──────────────────────────────────────────────────────

class SentinelObserver:
    """
    Background thread observer. Subscribes to CrewAI event bus for LLM stream
    chunks and tool usage events. Receives structured SentinelEvents from
    crew.py phase runners via the observation queue. Emits thinking, flags, and
    directives to the Approval Gate terminal.
    """

    def __init__(
        self,
        gate_log_fn: Callable[[str], None],
        flag_fn: Optional[Callable[[dict], None]] = None,
    ):
        self._gate_log  = gate_log_fn
        self._flag_fn   = flag_fn   # called with serialised flag dict on every _raise_flag
        self._obs_queue: queue.Queue = queue.Queue()
        self._stop_evt  = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # Per-phase pause gates — phase runners call gate.wait() before passing output
        self._pause_gates: dict[str, threading.Event] = {}
        self._pause_gates_lock = threading.Lock()  # guards _pause_gates mutations

        # Active unresolved flags
        self._flags: dict[str, SentinelFlag] = {}
        self._flags_lock = threading.Lock()

        # Consecutive empty batch counters per (phase, brand, platform)
        self._empty_counts: dict[tuple, int] = {}

        # Accumulated stream buffer per phase (for partial output scanning)
        self._stream_bufs: dict[str, list] = {}

        # Phase timing tracker
        self._phase_start: dict[str, float] = {}

        # CrewAI event bus handles for deregistration
        self._eb_handles: list = []

        # Run params (injected at start)
        self._run_params: dict = {}

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def start(self, run_params: dict = None) -> None:
        """Start the Sentinel thread and register CrewAI event listeners."""
        self._run_params = run_params or {}
        self._stop_evt.clear()
        self._pause_gates.clear()
        self._flags.clear()
        self._empty_counts.clear()
        self._stream_bufs.clear()
        self._phase_start.clear()

        # Register CrewAI event bus listeners
        self._register_event_listeners()

        self._thread = threading.Thread(
            target=self._run_loop, daemon=True, name="sentinel-observer"
        )
        self._thread.start()

        self._gate_write(
            "\n" + "═" * 60 + "\n"
            "[SENTINEL] Nielsen Media Research Director + Code Reviewer\n"
            "[SENTINEL] Observer active — monitoring all pipeline phases.\n"
            "[SENTINEL] Authority: flags are binding per phase.\n"
            "[SENTINEL] Approval Gate retains override authority.\n"
            + "═" * 60
        )

    def stop(self) -> None:
        """Stop the Sentinel thread and deregister event listeners."""
        self._stop_evt.set()
        self._obs_queue.put(None)   # unblock queue.get()

        # Deregister CrewAI event listeners — use off(event_type, handler)
        try:
            from crewai.events.event_bus import crewai_event_bus as _eb
            from crewai.events import (
                LLMCallStartedEvent,
                ToolUsageFinishedEvent,
                AgentExecutionCompletedEvent,
            )
            _evt_types = [LLMCallStartedEvent, ToolUsageFinishedEvent, AgentExecutionCompletedEvent]
            for evt_type, handler in zip(_evt_types, self._eb_handles):
                try:
                    _eb.off(evt_type, handler)
                except Exception:
                    pass
        except Exception:
            pass
        self._eb_handles.clear()

        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

        # Release all pending pause gates so phases don't hang
        with self._pause_gates_lock:
            for gate in self._pause_gates.values():
                gate.set()

        self._gate_write("[SENTINEL] Observer stopped.")

    def post(self, event: SentinelEvent) -> None:
        """Thread-safe: push a SentinelEvent onto the observation queue."""
        if not self._stop_evt.is_set():
            self._obs_queue.put(event)

    def get_pause_gate(self, phase_name: str) -> threading.Event:
        """Return (creating if absent) the pause gate for a phase. Thread-safe."""
        with self._pause_gates_lock:
            if phase_name not in self._pause_gates:
                evt = threading.Event()
                evt.set()   # starts open; Sentinel closes it on CRITICAL flag
                self._pause_gates[phase_name] = evt
            return self._pause_gates[phase_name]

    def receive_agent_response(self, phase_name: str, response_text: str) -> None:
        """Called by crew.py when a phase exception/completion produces a response
        to a pending Sentinel flag for that phase."""
        with self._flags_lock:
            pending = [f for f in self._flags.values()
                       if f.phase == phase_name and not f.resolved and not f.overridden]
        if not pending:
            return
        flag = pending[-1]   # respond to most recent flag for this phase
        self._gate_write(
            f"\n[AGENT RESPONSE] {phase_name} → sentinel (flag {flag.flag_id})\n"
            f"  {response_text[:300]}"
        )
        # Auto-resolve: agent has acknowledged — issue final directive
        self._issue_directive(
            flag,
            "Agent acknowledged. Proceeding per recommendation. "
            "Gap will be surfaced in coverage table.",
        )

    def override(self, flag_id: str, gate_reasoning: str) -> None:
        """Called by approval_gate.register_override() to bypass a Sentinel directive."""
        with self._flags_lock:
            flag = self._flags.get(flag_id)
        if not flag:
            self._gate_write(
                f"[GATE OVERRIDE] flag {flag_id} not found — may already be resolved."
            )
            return
        flag.overridden = True
        flag.resolved   = True
        flag.override_gate.set()
        flag.pause_gate.set()   # release the phase
        self._gate_write(
            f"\n[GATE OVERRIDE] flag {flag_id} — Approval Gate supersedes Sentinel directive.\n"
            f"  Gate reasoning: {gate_reasoning}\n"
            f"  Phase released. Proceeding without Sentinel constraint."
        )

    # ── Core event loop ────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        while not self._stop_evt.is_set():
            try:
                event = self._obs_queue.get(timeout=2.0)
            except queue.Empty:
                continue
            if event is None:
                break
            try:
                self._analyze_event(event)
            except Exception as exc:
                self._gate_write(f"[SENTINEL ERROR] Event analysis failed: {exc!s:.120}")

    def _analyze_event(self, event: SentinelEvent) -> None:
        p = event.event_type
        if p == "phase_start":
            self._on_phase_start(event)
        elif p == "phase_heartbeat":
            self._on_heartbeat(event)
        elif p == "phase_timeout_warning":
            self._on_timeout_warning(event)
        elif p == "output_sample":
            self._on_output_sample(event)
        elif p == "phase_complete":
            self._on_phase_complete(event)
        elif p == "data_quality_check":
            self._on_data_quality(event)

    # ── Phase event handlers ───────────────────────────────────────────────

    def _on_phase_start(self, event: SentinelEvent) -> None:
        self._phase_start[event.phase_name] = event.timestamp
        timeout = event.payload.get("timeout_secs", "?")
        self._think(
            f"Phase '{event.phase_name}' started. "
            f"Timeout cap: {timeout}s. Monitoring for blocks, empty outputs, "
            f"and signal coverage issues."
        )
        # Open (reset) the pause gate for this phase
        gate = self.get_pause_gate(event.phase_name)
        gate.set()

    def _on_heartbeat(self, event: SentinelEvent) -> None:
        elapsed  = event.payload.get("elapsed_secs", 0)
        timeout  = event.payload.get("timeout_secs", 1)
        pct      = elapsed / max(timeout, 1)
        if pct >= 0.60:
            self._think(
                f"Phase '{event.phase_name}' at {pct:.0%} of timeout "
                f"({elapsed:.0f}s / {timeout}s). "
                f"{'Watching closely — approaching limit.' if pct >= 0.80 else 'Normal pace.'}"
            )

    def _on_timeout_warning(self, event: SentinelEvent) -> None:
        elapsed = event.payload.get("elapsed_secs", 0)
        timeout = event.payload.get("timeout_secs", 1)
        phase   = event.phase_name
        self._think(
            f"[WARNING] Phase '{phase}' at 80%+ of timeout "
            f"({elapsed:.0f}s of {timeout}s). "
            f"If it completes with empty output, I will flag it as a likely scraper block."
        )
        # Autonomous fix for analyst stalls: switch to compact context so next retry succeeds
        if phase == "analyst":
            self._think(
                "Analyst approaching timeout — autonomously enabling compact context mode "
                "so the next retry uses a 2000-char input cap instead of 4000-char. "
                "This prevents gemma4:26b from stalling on large profile map JSON."
            )
            self._action_switch_analyst_to_compact()

    def _on_output_sample(self, event: SentinelEvent) -> None:
        phase  = event.phase_name
        sample = event.payload.get("sample", "")
        buf    = self._stream_bufs.setdefault(phase, [])
        buf.append(sample)

        lower = sample.lower()

        # Detect JSON parse errors in LLM stream — autonomous fix: strip fences + save checkpoint
        if any(x in lower for x in ("json.decode", "valueerror", "expecting value",
                                     "unterminated string", "invalid \\escape")):
            _raw_capture = sample
            _phase_cap   = phase
            _self_ref    = self

            def _auto_strip():
                _self_ref._action_strip_json_fence(_phase_cap, _raw_capture)

            flag_json = SentinelFlag(
                flag_id=self._new_id(), phase=phase, brand="", platform="",
                issue="JSON parse error in LLM output — Sentinel auto-fixing: stripping fences",
                technical=(
                    "LLM output contains a JSON parse error signal. Sentinel will automatically "
                    "strip markdown code fences and re-validate before writing the checkpoint."
                ),
                methodological=(
                    "Empty baselines disable ER outlier scoring. TikTok and Facebook paid detection "
                    "degrades to DOM labels only — ~30–40% miss rate in SEA markets."
                ),
                recommendation="AUTO-FIX executing: _strip_fences() + checkpoint rewrite.",
                confidence="HIGH", severity="CRITICAL",
            )
            self._raise_flag(flag_json)
            self._issue_directive(
                flag_json,
                "Sentinel auto-fixing JSON parse error: stripping fences and rewriting checkpoint.",
                action=_auto_strip,
            )

        # Detect selector failures — autonomous fix: log coverage gap + cap confidence
        if "queryselector" in lower and ("none" in lower or "not found" in lower):
            _self_ref = self
            _phase_c  = phase

            def _auto_selector():
                _self_ref._action_set_directive(f"selector_stale_{_phase_c}", True)
                _self_ref._action_log_coverage_gap("", "", f"selector_stale in {_phase_c} — confidence capped at Low")

            flag_sel = SentinelFlag(
                flag_id=self._new_id(), phase=phase, brand="", platform="",
                issue="DOM selector returned None — Sentinel logging selector_stale directive",
                technical=(
                    "page.query_selector() returned None. Platform DOM may have changed or "
                    "selectors.json is stale. Sentinel will flag this phase as selector_stale."
                ),
                methodological=(
                    "If follower_count selector fails, ER denominator falls to max(avg_views, 1). "
                    "For brands with low view counts, ER becomes 100%+ and every post gets flagged "
                    "as baseline_outlier. SOV paid volume will be massively overstated."
                ),
                recommendation="AUTO-FIX: setting selector_stale directive. Confidence capped at Low in output.",
                confidence="HIGH", severity="WARNING",
            )
            self._raise_flag(flag_sel)
            self._issue_directive(
                flag_sel,
                "Selector failure recorded. This phase's confidence is capped at Low.",
                action=_auto_selector,
            )

        # Detect ngrok tunnel errors — autonomous fix: switch analyst to compact + clear checkpoint
        if "err_ngrok" in lower or "ngrok" in lower and "error" in lower:
            _self_ref = self
            _phase_c  = phase

            def _auto_ngrok():
                _self_ref._action_switch_analyst_to_compact()
                _self_ref._action_clear_phase_checkpoint(_phase_c)

            self._gate_write(
                f"[SENTINEL] AUTO-FIX: ngrok error detected in {phase} — "
                "switching analyst to compact mode and clearing phase checkpoint for retry."
            )
            threading.Thread(target=_auto_ngrok, daemon=True,
                             name="sentinel-ngrok-fix").start()

        # Detect OOM / target crashed — autonomous fix: log coverage gap
        if "target crashed" in lower or "out of memory" in lower or "oom" in lower:
            _self_ref = self

            def _auto_oom():
                _self_ref._action_set_directive("scraper_oom_detected", True)
                _self_ref._action_log_coverage_gap("", "", "OOM/target_crashed — partial scraper results only")

            self._gate_write(
                "[SENTINEL] AUTO-FIX: OOM/target crash detected — logging coverage gap. "
                "Scraper results for affected brand/platform may be partial."
            )
            threading.Thread(target=_auto_oom, daemon=True,
                             name="sentinel-oom-fix").start()

    def _on_phase_complete(self, event: SentinelEvent) -> None:
        phase       = event.phase_name
        output_len  = event.payload.get("output_len", 0)
        sample      = event.payload.get("output_sample", "")

        elapsed = 0.0
        if phase in self._phase_start:
            elapsed = time.monotonic() - self._phase_start[phase]

        self._think(
            f"Phase '{phase}' complete in {elapsed:.1f}s. "
            f"Output length: {output_len} chars. "
            f"{'Output looks populated.' if output_len > 100 else 'Output is very short — checking for empty JSON.'}"
        )

        # Check for empty/stub output — autonomous fix: clear stale checkpoint
        stripped = sample.strip()
        if stripped in ("{}", "[]", '""', "", "None", "null"):
            _phase_cap = phase
            _self_ref  = self

            def _auto_clear():
                _self_ref._action_clear_phase_checkpoint(_phase_cap)
                _self_ref._action_set_directive(f"phase_empty_{_phase_cap}", True)

            flag_empty = SentinelFlag(
                flag_id=self._new_id(), phase=phase, brand="", platform="",
                issue=f"Phase '{phase}' completed with empty output — Sentinel clearing checkpoint",
                technical=(
                    f"Phase runner returned '{stripped}'. Sentinel is clearing the stale checkpoint "
                    "so the next retry re-runs this phase rather than reading the empty cached result."
                ),
                methodological=(
                    "The analyst will receive no signal data for this phase. Without intervention, "
                    "it will either produce zero SOV scores or hallucinate plausible-looking indices."
                ),
                recommendation="AUTO-FIX: clearing checkpoint so next retry re-runs this phase.",
                confidence="HIGH", severity="CRITICAL",
            )
            self._raise_flag(flag_empty)
            self._issue_directive(
                flag_empty,
                f"Checkpoint for '{phase}' cleared. Next retry will re-run this phase from scratch.",
                action=_auto_clear,
            )

    def _on_data_quality(self, event: SentinelEvent) -> None:
        phase    = event.phase_name
        brand    = event.payload.get("brand", "")
        platform = event.payload.get("platform", "")
        posts    = event.payload.get("post_count", 0)
        baseline = event.payload.get("baseline_available", True)
        emp_key  = (phase, brand, platform)

        label = f"{brand}/{platform}" if brand else phase

        # Track consecutive empty batches
        if posts == 0:
            self._empty_counts[emp_key] = self._empty_counts.get(emp_key, 0) + 1
        else:
            self._empty_counts[emp_key] = 0

        consecutive_empty = self._empty_counts.get(emp_key, 0)

        if consecutive_empty >= _CONSECUTIVE_EMPTY_THRESHOLD:
            self._think(
                f"{label}: {consecutive_empty} consecutive empty batches. "
                f"Pattern is consistent with a scraper block, rate-limit, or login redirect — "
                f"not an empty brand presence."
            )
            _brand_cap    = brand
            _platform_cap = platform
            _self_ref     = self

            def _auto_gap():
                _self_ref._action_log_coverage_gap(
                    _brand_cap, _platform_cap,
                    f"scraper_blocked — {consecutive_empty} consecutive empty batches"
                )
                _self_ref._action_set_directive(
                    f"scraper_block_{_brand_cap}_{_platform_cap}", True
                )

            flag_block = SentinelFlag(
                flag_id=self._new_id(), phase=phase, brand=brand, platform=platform,
                issue=f"{label} — {consecutive_empty} consecutive empty batches (likely scraper block)",
                technical=(
                    f"platform={platform or 'all'}: {consecutive_empty} batches returned 0 posts. "
                    "Common causes: rate-limit (HTTP 429), anti-bot redirect to login page, "
                    "or IP/geo block. Exception is swallowed; empty result looks like valid data."
                ),
                methodological=(
                    f"Without {platform or 'any'} posts, baseline cannot be computed. "
                    f"Feed scroller receives baseline_available=False for {brand or 'all brands'} "
                    f"on {platform or 'all platforms'}. ER outlier scoring is disabled. "
                    "Paid detection is DOM-labels only. In SEA markets where TikTok and Facebook "
                    "apply 'Sponsored' labels inconsistently, this means heavy paid activity "
                    "will not be surfaced. SOV will structurally under-report paid presence."
                ),
                recommendation=(
                    "AUTO-FIX: logging coverage gap + setting scraper_block directive. "
                    "Reporter will surface this as 'data unavailable' with Low confidence."
                ),
                confidence="HIGH", severity="CRITICAL",
            )
            self._raise_flag(flag_block)
            self._issue_directive(
                flag_block,
                f"Coverage gap logged for {label}. Confidence capped at Low. "
                "Reporter will surface as 'scraper_blocked' in output.",
                action=_auto_gap,
            )
        elif posts > 0 and posts < _MIN_BASELINE_POSTS and phase == "profile":
            self._think(
                f"{label}: only {posts} posts in scope (minimum for reliable baseline: "
                f"{_MIN_BASELINE_POSTS}). "
                f"ER threshold computed from {posts} posts is statistically unreliable. "
                f"A single viral post can dominate the trimmed mean at this sample size."
            )
            self._raise_flag(SentinelFlag(
                flag_id=self._new_id(), phase=phase, brand=brand, platform=platform,
                issue=f"{label} — only {posts} posts (below minimum {_MIN_BASELINE_POSTS} for reliable baseline)",
                technical=(
                    f"_build_baseline() called with {posts} posts. "
                    f"_trimmed() at N={posts}: int({posts}×0.75)={int(posts*0.75)} is the cutoff index. "
                    "For N≤4, this equals the last index — no trimming occurs. "
                    "The 'trimmed' mean is the full mean. The 3× threshold is set by a single outlier post."
                ),
                methodological=(
                    "Professional baseline methodology (Nielsen, Kantar) requires minimum N=12 "
                    "posts for a statistically defensible ER threshold. Below this, a brand "
                    "that ran one viral organic post will have its threshold inflated, "
                    "causing all subsequent paid posts to be classified as organic. "
                    f"Recommendation: set baseline_available=False for {label} "
                    f"when post_count < {_MIN_BASELINE_POSTS}."
                ),
                recommendation=(
                    f"Set baseline_available=False for {label}. "
                    "Disable Phase 2 baseline scoring. Return posts as unclassified. "
                    "Cap confidence at Low in output."
                ),
                confidence="HIGH", severity="WARNING",
            ))

        if not baseline and phase == "profile":
            self._think(
                f"{label}: baseline_available=False. Feed scroller will use DOM labels only "
                f"for {platform or 'all platforms'}. "
                f"In non-EU markets, TikTok CCL reach data is also null. "
                f"This means TikTok SOV may be computed from 1 of 6 signals."
            )

    # ── Flag + Directive mechanics ─────────────────────────────────────────

    def _raise_flag(self, flag: SentinelFlag) -> None:
        with self._flags_lock:
            self._flags[flag.flag_id] = flag

        # Push to dashboard active_flags via injected callback
        if self._flag_fn:
            try:
                self._flag_fn({
                    "flag_id":         flag.flag_id,
                    "phase":           flag.phase,
                    "brand":           flag.brand,
                    "platform":        flag.platform,
                    "issue":           flag.issue,
                    "severity":        flag.severity,
                    "methodological":  flag.methodological,
                    "recommendation":  flag.recommendation,
                    "confidence":      flag.confidence,
                    "resolved":        flag.resolved,
                    "overridden":      flag.overridden,
                })
            except Exception:
                pass

        # Close the pause gate for this phase
        gate = self.get_pause_gate(flag.phase)
        gate.clear()

        # Format and write to gate terminal
        brand_str    = f" | Brand: {flag.brand}" if flag.brand else ""
        platform_str = f" | Platform: {flag.platform}" if flag.platform else ""

        self._gate_write(
            f"\n{'─'*60}\n"
            f"[SENTINEL FLAG] Phase: {flag.phase}{brand_str}{platform_str}\n"
            f"  Severity: {flag.severity} | Sentinel Confidence: {flag.confidence}\n"
            f"\n"
            f"  [TECHNICAL]      {flag.technical}\n"
            f"\n"
            f"  [METHODOLOGY]    {flag.methodological}\n"
            f"\n"
            f"  [RECOMMENDATION] {flag.recommendation}\n"
            f"  Flag ID: {flag.flag_id}\n"
            f"  (Approval Gate can override with: POST /sentinel-override "
            f"{{\"flag_id\": \"{flag.flag_id}\", \"reason\": \"...\"}})\n"
            f"{'─'*60}"
        )

        # For INFO/WARNING flags: auto-resolve after a short window
        # For CRITICAL flags: hold until agent response or Gate override
        if flag.severity == "INFO":
            flag.resolved = True
            gate.set()
        elif flag.severity == "WARNING":
            # Issue directive immediately for warnings — don't hold the phase
            self._issue_directive(
                flag,
                f"Proceeding with caution. Gap logged. "
                f"Coverage table will reflect {flag.brand or flag.phase}/{flag.platform or 'all'} "
                f"as limited_data. Confidence capped at Medium.",
            )
        # CRITICAL: phase stays paused until receive_agent_response() or override()

    def _issue_directive(self, flag: SentinelFlag, directive: str,
                         action: Optional[Callable] = None) -> None:
        """Issue a directive and optionally execute an autonomous fix action.

        action: callable that will be invoked in a daemon thread immediately after
                the directive is issued. Must not block the Sentinel thread long.
                Any exception is caught and logged — it must never crash the observer.
        """
        flag.resolved = True
        gate = self.get_pause_gate(flag.phase)
        gate.set()   # release the phase

        # Sync resolved state to dashboard
        if self._flag_fn:
            try:
                self._flag_fn({
                    "flag_id": flag.flag_id, "phase": flag.phase,
                    "brand": flag.brand, "platform": flag.platform,
                    "issue": flag.issue, "severity": flag.severity,
                    "methodological": flag.methodological,
                    "recommendation": flag.recommendation,
                    "confidence": flag.confidence,
                    "resolved": True, "overridden": False,
                })
            except Exception:
                pass

        self._gate_write(
            f"\n[SENTINEL DIRECTIVE] flag {flag.flag_id} → {flag.phase}\n"
            f"  {directive}"
        )

        if action is not None:
            def _run_action():
                try:
                    action()
                except Exception as exc:
                    self._gate_write(f"[SENTINEL] Autonomous action failed: {exc!s:.200}")
            threading.Thread(target=_run_action, daemon=True,
                             name=f"sentinel-action-{flag.flag_id}").start()

    # ── Autonomous fix actions ─────────────────────────────────────────────

    def _action_strip_json_fence(self, phase: str, raw_output: str) -> Optional[str]:
        """Strip markdown fences from LLM output, validate JSON, write a fixed checkpoint."""
        import re as _re, json as _json, pathlib as _p
        cleaned = _re.sub(r"```(?:json)?\s*|```", "", raw_output).strip()
        start   = cleaned.find("{")
        end     = cleaned.rfind("}") + 1
        if start == -1 or end <= start:
            return None
        candidate = cleaned[start:end]
        try:
            _json.loads(candidate)
        except Exception:
            return None
        # Write fixed checkpoint so next phase picks it up
        cp_dir = _p.Path("data/checkpoints")
        cp_dir.mkdir(parents=True, exist_ok=True)
        cp_path = cp_dir / f"{phase}.json"
        import json as _json2
        cp_path.write_text(_json2.dumps({"output": candidate}), encoding="utf-8")
        self._gate_write(
            f"[SENTINEL] AUTO-FIX: Stripped JSON fences in {phase} output and wrote fixed checkpoint."
        )
        return candidate

    def _action_switch_analyst_to_compact(self) -> None:
        """Notify crew.py that the analyst should use compact context on next retry."""
        try:
            try:
                from server import _run_state, _state_lock
            except ImportError:
                from api import _run_state, _state_lock
            with _state_lock:
                _run_state.setdefault("sentinel_directives", {})["analyst_compact"] = True
            self._gate_write(
                "[SENTINEL] AUTO-FIX: Set analyst_compact=True — next retry will use "
                "a 2000-char context cap to prevent LLM stall on large profile map."
            )
        except Exception as exc:
            self._gate_write(f"[SENTINEL] compact directive failed: {exc!s:.80}")

    def _action_set_directive(self, key: str, value) -> None:
        """Generic: write an arbitrary key/value into _run_state['sentinel_directives']."""
        try:
            try:
                from server import _run_state, _state_lock
            except ImportError:
                from api import _run_state, _state_lock
            with _state_lock:
                _run_state.setdefault("sentinel_directives", {})[key] = value
            self._gate_write(f"[SENTINEL] AUTO-FIX: directive set — {key}={value!r}")
        except Exception as exc:
            self._gate_write(f"[SENTINEL] directive set failed: {exc!s:.80}")

    def _action_clear_phase_checkpoint(self, phase: str) -> None:
        """Delete a phase checkpoint so the next retry re-runs that phase from scratch."""
        import pathlib as _p
        cp = _p.Path("data/checkpoints") / f"{phase}.json"
        if cp.exists():
            try:
                cp.unlink()
                self._gate_write(f"[SENTINEL] AUTO-FIX: Cleared stale checkpoint for phase '{phase}'.")
            except Exception as exc:
                self._gate_write(f"[SENTINEL] checkpoint clear failed: {exc!s:.80}")

    def _action_log_coverage_gap(self, brand: str, platform: str, reason: str) -> None:
        """Record a coverage gap in _run_state so the reporter can surface it in output."""
        try:
            try:
                from server import _run_state, _state_lock
            except ImportError:
                from api import _run_state, _state_lock
            with _state_lock:
                gaps = _run_state.setdefault("sentinel_coverage_gaps", [])
                gaps.append({"brand": brand, "platform": platform, "reason": reason})
            self._gate_write(
                f"[SENTINEL] AUTO-FIX: Coverage gap logged — {brand}/{platform}: {reason}"
            )
        except Exception as exc:
            self._gate_write(f"[SENTINEL] coverage gap log failed: {exc!s:.80}")

    # ── CrewAI event bus integration ───────────────────────────────────────

    def _register_event_listeners(self) -> None:
        """Register handlers on CrewAI's event bus for LLM stream + tool events.

        Uses crewai.events (crewai >= 1.x) with correct class names:
          LLMCallStartedEvent, ToolUsageFinishedEvent, AgentExecutionCompletedEvent.
        Deregistration uses eb.off(event_type, handler) — see stop().
        """
        try:
            from crewai.events.event_bus import crewai_event_bus as _eb
            from crewai.events import (
                LLMCallStartedEvent,
                ToolUsageFinishedEvent,
                AgentExecutionCompletedEvent,
            )

            # Handler: LLM call starts → thinking stream
            # crewai 1.x: LLMCallStartedEvent has no .agent; use agent_role string field directly.
            @_eb.on(LLMCallStartedEvent)
            def _on_llm_start(source, event):
                role  = (getattr(event, "agent_role", None)
                         or getattr(getattr(event, "agent", None), "role", None)
                         or "")
                phase = _ROLE_TO_PHASE.get(role.lower().strip(), "")
                if phase:
                    self._gate_write(f"[AGENT THINKING: {role}] Starting LLM inference...")

            # Handler: tool usage finished → parse output for data quality signals
            # crewai 1.x: ToolUsageFinishedEvent exposes agent_role as a plain string.
            @_eb.on(ToolUsageFinishedEvent)
            def _on_tool_done(source, event):
                tool_name   = getattr(event, "tool_name", "") or ""
                tool_output = str(getattr(event, "output", "") or "")
                role        = (getattr(event, "agent_role", None)
                               or getattr(getattr(event, "agent", None), "role", None)
                               or "")
                phase       = _ROLE_TO_PHASE.get(role.lower().strip(), "")
                if not phase:
                    return
                self._gate_write(
                    f"[AGENT THINKING: {role}] Tool '{tool_name}' completed. "
                    f"Output length: {len(tool_output)} chars."
                )
                self._parse_tool_output(phase, tool_name, tool_output)

            # Handler: agent execution complete → emit thinking summary + phase_complete
            # crewai 1.x: AgentExecutionCompletedEvent has both .agent (object) and agent_role (str).
            @_eb.on(AgentExecutionCompletedEvent)
            def _on_agent_done(source, event):
                role   = (getattr(event, "agent_role", None)
                          or getattr(getattr(event, "agent", None), "role", None)
                          or "")
                phase  = _ROLE_TO_PHASE.get(role.lower().strip(), "")
                output = str(getattr(event, "output", "") or "")
                if phase:
                    self._gate_write(
                        f"[AGENT THINKING: {role}] Task complete. "
                        f"Output: {output[:200]}{'...' if len(output) > 200 else ''}"
                    )
                    self.post(SentinelEvent(
                        event_type="phase_complete",
                        phase_name=phase,
                        timestamp=time.monotonic(),
                        payload={"output_len": len(output), "output_sample": output[:500]},
                    ))

            # Store in registration order — must match _evt_types list in stop()
            self._eb_handles.extend([_on_llm_start, _on_tool_done, _on_agent_done])

        except Exception as exc:
            self._gate_write(
                f"[SENTINEL] CrewAI event bus registration failed: {exc!s:.120}\n"
                f"  Falling back to observation queue only (crew.py manual posts)."
            )

    def _parse_tool_output(self, phase: str, tool_name: str, output: str) -> None:
        """Parse a tool's JSON output for data quality signals and post data_quality_check events."""
        try:
            # Strip markdown fences before parsing
            cleaned = re.sub(r"```(?:json)?\s*|```", "", output).strip()
            data = json.loads(cleaned) if cleaned.startswith(("{", "[")) else {}
        except Exception:
            data = {}

        if not data:
            return

        # Profile scraper output
        if "profiles" in data:
            for entry in data.get("profiles", []):
                brand    = entry.get("brand", "")
                platform = entry.get("platform", "")
                n_posts  = entry.get("posts_in_scope", 0)
                baseline = entry.get("baseline_available", False)
                er_thresh = entry.get("er_threshold", 0.0)

                # Emit thinking
                self._gate_write(
                    f"[AGENT THINKING: Profile Scraper] "
                    f"{brand}/{platform}: {n_posts} posts in scope. "
                    f"Baseline: {'available' if baseline else 'NOT available'}. "
                    f"ER threshold: {er_thresh:.3f}%."
                )

                self.post(SentinelEvent(
                    event_type="data_quality_check",
                    phase_name=phase,
                    timestamp=time.monotonic(),
                    payload={
                        "brand":              brand,
                        "platform":           platform,
                        "post_count":         n_posts,
                        "baseline_available": baseline,
                        "er_threshold":       er_thresh,
                        "consecutive_empty":  0,
                    },
                ))

                # Nielsen Director check: is ER threshold suspiciously low or zero?
                if baseline and er_thresh == 0.0:
                    self._think(
                        f"{brand}/{platform}: baseline_available=True but er_threshold=0.0. "
                        f"This means avg_er_pct=0 — all metric selectors likely returned 0. "
                        f"ER denominator may have collapsed to 1 (selector miss). "
                        f"SOV engagement corroboration signal will be zero for this brand."
                    )

                # Nielsen Director check: platform-specific ER multiplier calibration
                plat_lower = platform.lower()
                recommended_mult = _PLATFORM_ER_MULTIPLIER.get(plat_lower, _DEFAULT_ER_MULTIPLIER)
                if plat_lower == "tiktok" and baseline and er_thresh > 0:
                    self._think(
                        f"{brand}/TikTok: NOTE — TikTok organic ER variance is structurally "
                        f"high. A 3× multiplier over a mean ER baseline produces more false "
                        f"positives than 1.5× over the 90th percentile (Nielsen/Kantar standard). "
                        f"Flagging for Approval Gate review: TikTok paid detection "
                        f"threshold may be miscalibrated."
                    )

        # Feed scroller output
        if "total_posts_scrolled" in data:
            total_posts  = data.get("total_posts_scrolled", 0)
            baselines_ok = data.get("baselines_applied", False)
            dom_ads      = data.get("total_dom_ads", 0)
            bl_outliers  = data.get("total_baseline_outliers", 0)

            self._gate_write(
                f"[AGENT THINKING: Ad Library Collector] "
                f"Ad library collection complete. Posts scrolled: {total_posts}. "
                f"DOM-confirmed ads: {dom_ads}. "
                f"Baseline outliers: {bl_outliers}. "
                f"Baselines applied: {baselines_ok}."
            )

            if not baselines_ok:
                self._think(
                    f"Feed scroller completed with baselines_applied=False. "
                    f"All paid detection was DOM-labels only. "
                    f"In SEA markets (PH, TH, ID, VN), Meta and TikTok 'Sponsored' label "
                    f"consistency is ~60–70%. Approximately 30–40% of paid posts in this "
                    f"feed will not have been detected. SOV will structurally under-report "
                    f"paid presence. Confidence for feed-sourced signals must be capped at Medium."
                )

            self.post(SentinelEvent(
                event_type="data_quality_check",
                phase_name=phase,
                timestamp=time.monotonic(),
                payload={
                    "brand":              "",
                    "platform":           "",
                    "post_count":         total_posts,
                    "baseline_available": baselines_ok,
                    "consecutive_empty":  0 if total_posts > 0 else 1,
                },
            ))

    # ── Utility ────────────────────────────────────────────────────────────

    def _think(self, line: str) -> None:
        """Emit a Sentinel thinking line to the gate terminal."""
        self._gate_write(f"[SENTINEL THINKING] {line}")

    def _gate_write(self, text: str) -> None:
        """Write text to the Approval Gate terminal. Thread-safe via gate_log_fn."""
        try:
            for line in text.split("\n"):
                self._gate_log(line)
        except Exception:
            pass

    @staticmethod
    def _new_id() -> str:
        return "f-" + uuid.uuid4().hex[:6]


# ── SOV post-processing utilities (called by crew.py after reporter) ─────────

_METHODOLOGY_DISCLAIMER = (
    "All values are directional Share-of-Voice indices (0–100 scale, "
    "Directional / Indexed – Not Actual Spend) reflecting relative advertising "
    "presence within the selected competitive set. These are estimates based on "
    "observable data (ad counts, reach proxies, presence signals) and do not "
    "represent actual spend figures. All indices are calculated within the context "
    "of the selected competitor group and represent share of voice among these "
    "competitors only, not an entire industry or market."
)

_CONFIDENCE_RULES = {
    # (baseline_available_primary, signal_coverage, scrape_blocked) → confidence_cap
    # Applied after LLM assigns confidence; these are hard overrides downward only.
}


def normalize_sov(report: dict) -> dict:
    """
    Post-process reporter JSON:
      1. Normalize sov_index per platform to sum to 100 (eliminates LLM arithmetic errors).
      2. Inject methodology_disclaimer if absent or empty.
      3. Apply hard confidence caps based on data quality rules.
      4. Copy Facebook signals to Instagram entry (Instagram modelled from Facebook).
    Returns a new dict (does not mutate input).
    """
    import copy
    result = copy.deepcopy(report)

    brands = result.get("brands") or result.get("competitors") or []

    # ── 1. SOV normalization per platform ──────────────────────────────────
    platforms = set()
    for b in brands:
        for k in (b.get("platforms") or {}).keys():
            platforms.add(k.lower())

    for plat in platforms:
        total = sum(
            float((b.get("platforms") or {}).get(plat, {}).get("sov_index", 0) or 0)
            for b in brands
        )
        if total > 0:
            for b in brands:
                plat_data = (b.get("platforms") or {}).get(plat)
                if plat_data and "sov_index" in plat_data:
                    raw = float(plat_data["sov_index"] or 0)
                    plat_data["sov_index"] = round((raw / total) * 100, 1)

    # ── 2. Methodology disclaimer ───────────────────────────────────────────
    if not result.get("methodology_disclaimer"):
        result["methodology_disclaimer"] = _METHODOLOGY_DISCLAIMER

    # ── 3. Confidence caps ──────────────────────────────────────────────────
    for b in brands:
        for plat, plat_data in (b.get("platforms") or {}).items():
            if not isinstance(plat_data, dict):
                continue

            current_conf = plat_data.get("confidence", "Low")
            signal_count = len([
                v for v in (plat_data.get("signals") or {}).values()
                if v not in (None, 0, "missing", "null")
            ])
            baseline_ok = plat_data.get("baseline_available", True)

            # Hard cap: no baseline → no higher than Medium
            if not baseline_ok and current_conf == "High":
                plat_data["confidence"] = "Medium"
                plat_data["confidence_note"] = (
                    "Downgraded from High: baseline_available=False. "
                    "ER corroboration signal unavailable."
                )

            # Hard cap: fewer than 2 signals → Low
            if signal_count < _SIGNAL_COVERAGE_LOW_CAP:
                plat_data["confidence"] = "Low"
                plat_data["confidence_note"] = (
                    f"Hard cap at Low: only {signal_count} of 6 signals populated."
                )

            # Hard cap: TikTok in non-EU market → reach signal null → max Medium
            if plat.lower() == "tiktok":
                country = result.get("country") or result.get("market") or ""
                eu_countries = {"gb", "de", "fr", "nl", "se", "dk", "fi", "no",
                                "it", "es", "pl", "be", "at", "pt", "ie"}
                # Re-read after earlier caps — avoids stale current_conf upgrading a Low
                live_conf = plat_data.get("confidence", "Low")
                if country.lower() not in eu_countries and live_conf == "High":
                    plat_data["confidence"] = "Medium"
                    plat_data["confidence_note"] = (
                        "Downgraded from High: TikTok EU Ad Library reach data unavailable "
                        f"for market '{country}'. Reach bucket signal is null."
                    )

    # ── 4. Instagram modelled from Facebook ────────────────────────────────
    for b in brands:
        plats = b.get("platforms") or {}
        fb    = plats.get("facebook")
        ig    = plats.get("instagram")
        if fb and isinstance(fb, dict):
            if not ig or not isinstance(ig, dict):
                plats["instagram"] = copy.deepcopy(fb)
                plats["instagram"]["data_source"] = "modelled_from_facebook"
                plats["instagram"]["modelling_note"] = (
                    "Instagram signals modelled from Facebook (Meta Ad Library covers both surfaces). "
                    "Feed-level Instagram data not independently scraped."
                )

    # ── 5. Schema version ───────────────────────────────────────────────────
    result.setdefault("schema_version", "4.0.0")

    return result
