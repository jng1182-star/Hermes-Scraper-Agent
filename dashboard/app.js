/* ─────────────────────────────────────────────────────────────────────────
   SocialListen — app.js
   Page router · Form state · Canvas neural graph · /status polling · Charts
   ───────────────────────────────────────────────────────────────────────── */

'use strict';

// ── State ────────────────────────────────────────────────────────────────────
const State = {
  competitors: [],            // tag chips
  dateRange: 'Last 30 days',
  customDateFrom: '',
  customDateTo: '',
  depth: 'deep',
  pollInterval: null,
  elapsedInterval: null,      // client-side 1s tick
  elapsedStart: null,         // Date.now() when run started
  reportData: null,
  agentStates: { scraper: 'idle', analyst: 'idle', reporter: 'idle', gate: 'idle' },
  lastLogs: [],
};

// ── Chart instances ──────────────────────────────────────────────────────────
const Charts = { spend: null, engage: null, platform: null, sentiment: null };

// ── Colour palette (Silicon Boardroom dark) ──────────────────────────────────
const C = {
  accent:    '#1f6feb',
  accentLt:  'rgba(31,111,235,0.15)',
  cyan:      '#38bdf8',
  green:     '#22c55e',
  greenLt:   'rgba(34,197,94,0.15)',
  amber:     '#f59e0b',
  red:       '#f85149',
  purple:    '#a78bfa',
  grid:      'rgba(30,144,255,0.07)',
  nodeIdle:  '#2e5080',
  textLight: '#8b949e',
  edge:      'rgba(31,111,235,0.20)',
  edgeAct:   '#1f6feb',
  edgeDone:  '#22c55e',
  packet:    '#1f6feb',
  chartGrid: 'rgba(30,144,255,0.08)',
  chartTick: '#3d6fa8',
};

const BRAND_COLORS = ['#1f6feb','#38bdf8','#a78bfa','#f59e0b','#f85149','#22c55e','#e879f9','#fb923c'];

// ═══════════════════════════════════════════════════════════════════════════════
// PAGE ROUTER
// ═══════════════════════════════════════════════════════════════════════════════

let _networkInit = false;

function showPage(name) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
  const page = document.getElementById('page-' + name);
  if (page) page.classList.add('active');
  document.querySelectorAll('[data-page="' + name + '"]').forEach(t => t.classList.add('active'));

  if (name === 'network') {
    if (!_networkInit) {
      setTimeout(() => { initNetworkCanvas(); drawNetworkLoop(); _networkInit = true; }, 80);
    } else {
      // Already initialised — just resize in case window changed
      setTimeout(resizeCanvas, 80);
    }
  } else {
    // Pause animation when not on network page to save CPU
    if (_rafId && name !== 'network') {
      cancelAnimationFrame(_rafId);
      _rafId = null;
      _networkInit = false;
    }
  }
  if (name === 'results' && State.reportData) {
    renderResults(State.reportData);
  }
}

document.querySelectorAll('.nav-tab').forEach(btn => {
  btn.addEventListener('click', () => showPage(btn.dataset.page));
});

// ═══════════════════════════════════════════════════════════════════════════════
// COMPETITOR TAG INPUT
// ═══════════════════════════════════════════════════════════════════════════════

const tagInput = document.getElementById('tagInput');
const tagWrap  = document.getElementById('tagWrap');

function addTag(val) {
  val = val.trim();
  if (!val || State.competitors.includes(val)) return;
  State.competitors.push(val);
  renderTags();
}

function removeTag(val) {
  State.competitors = State.competitors.filter(c => c !== val);
  renderTags();
}

function renderTags() {
  const chips = tagWrap.querySelectorAll('.tag-chip');
  chips.forEach(c => c.remove());
  State.competitors.forEach(val => {
    const chip = document.createElement('span');
    chip.className = 'tag-chip';
    chip.innerHTML = val + '<button type="button" aria-label="remove">×</button>';
    chip.querySelector('button').addEventListener('click', () => removeTag(val));
    tagWrap.insertBefore(chip, tagInput);
  });
}

tagInput.addEventListener('keydown', e => {
  if ((e.key === 'Enter' || e.key === ',') && tagInput.value.trim()) {
    e.preventDefault();
    addTag(tagInput.value);
    tagInput.value = '';
  }
  if (e.key === 'Backspace' && !tagInput.value && State.competitors.length) {
    removeTag(State.competitors[State.competitors.length - 1]);
  }
});
tagWrap.addEventListener('click', () => tagInput.focus());

// ── Platform checkboxes ──────────────────────────────────────────────────────
document.querySelectorAll('.platform-option').forEach(label => {
  label.addEventListener('click', () => {
    const cb = label.querySelector('input[type=checkbox]');
    cb.checked = !cb.checked;
    label.classList.toggle('checked', cb.checked);
  });
  label.querySelector('input').addEventListener('click', e => e.stopPropagation());
});

// ── Date range presets ───────────────────────────────────────────────────────
document.querySelectorAll('.date-preset').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.date-preset').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    const days = parseInt(btn.dataset.days);
    if (days === 0) {
      State.dateRange = 'custom';
      document.getElementById('dateCustom').classList.add('show');
    } else {
      document.getElementById('dateCustom').classList.remove('show');
      const now = new Date();
      const from = new Date(now);
      from.setDate(now.getDate() - days);
      State.dateRange = `Last ${days} days`;
    }
  });
});

document.getElementById('dateFrom').addEventListener('change', e => { State.customDateFrom = e.target.value; });
document.getElementById('dateTo').addEventListener('change', e => { State.customDateTo = e.target.value; });

// ── Depth radio ──────────────────────────────────────────────────────────────
document.querySelectorAll('.depth-option').forEach(label => {
  label.addEventListener('click', () => {
    document.querySelectorAll('.depth-option').forEach(l => l.classList.remove('active'));
    label.classList.add('active');
    State.depth = label.querySelector('input').value;
  });
});

// ── File upload ──────────────────────────────────────────────────────────────
document.getElementById('reportUpload').addEventListener('change', function(e) {
  const file = e.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = ev => {
    try {
      const data = JSON.parse(ev.target.result);
      State.reportData = data;
      showPage('results');
    } catch(err) {
      setLogStatus('error', 'JSON parse error: ' + err.message);
    }
  };
  reader.readAsText(file);
});

// ═══════════════════════════════════════════════════════════════════════════════
// RUN ANALYSIS
// ═══════════════════════════════════════════════════════════════════════════════

function getFormParams() {
  const platforms = [];
  document.querySelectorAll('.platform-option input:checked').forEach(cb => platforms.push(cb.value));

  let dateRange = State.dateRange;
  if (dateRange === 'custom') {
    const f = document.getElementById('dateFrom').value;
    const t = document.getElementById('dateTo').value;
    if (f && t) dateRange = `${f} to ${t}`;
  }

  return {
    advertiser:  document.getElementById('advertiser').value.trim(),
    competitors: [...State.competitors],
    country:     document.getElementById('country').value,
    platforms,
    date_range:  dateRange,
    date_from:   document.getElementById('dateFrom').value || null,
    date_to:     document.getElementById('dateTo').value || null,
    cpm_rate:    parseFloat(document.getElementById('cpmRate').value) || 15.0,
    keywords:    document.getElementById('keywords').value.trim(),
    depth:       State.depth,
  };
}

document.getElementById('runBtn').addEventListener('click', async () => {
  const btn = document.getElementById('runBtn');
  btn.disabled = true;
  document.getElementById('runBtnIcon').textContent = '⟳';
  document.getElementById('runBtnIcon').classList.add('spinner-icon');
  document.getElementById('runBtnText').textContent = 'Running…';

  showLogPanel(true);
  setLogStatus('running', 'Analysis running…');
  document.getElementById('logOutput').textContent = '';

  // Reset agent states
  State.agentStates = { scraper: 'idle', analyst: 'idle', reporter: 'idle', gate: 'idle' };
  resetAgentCards();
  updateDots('running');

  // Show elapsed timer from 0 — tick every second client-side
  const elapsedBadge = document.getElementById('elapsedBadge');
  if (elapsedBadge) { elapsedBadge.style.display = 'flex'; }
  document.getElementById('elapsedTime').textContent = '0:00';
  State.elapsedStart = Date.now();
  if (State.elapsedInterval) clearInterval(State.elapsedInterval);
  State.elapsedInterval = setInterval(() => {
    const secs = Math.floor((Date.now() - State.elapsedStart) / 1000);
    const m = Math.floor(secs / 60);
    const s = secs % 60;
    const el = document.getElementById('elapsedTime');
    if (el) el.textContent = `${m}:${String(s).padStart(2,'0')}`;
  }, 1000);
  const banner = document.getElementById('timeoutBanner');
  if (banner) banner.style.display = 'none';

  const params = getFormParams();

  try {
    const res = await fetch('/run-analysis', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(params),
    });
    const data = await res.json();
    if (data.status === 'already_running') {
      setLogStatus('running', 'Analysis already in progress…');
    }
  } catch(e) {
    setLogStatus('error', 'Could not reach server. Is uvicorn running?');
    resetRunBtn();
    updateDots('idle');
    return;
  }

  State.pollInterval = setInterval(pollStatus, 2000);
  // Enable stop button
  const stopBtn = document.getElementById('stopBtn');
  if (stopBtn) stopBtn.disabled = false;
  // Auto-switch to network page so user can watch
  showPage('network');
});

document.getElementById('stopBtn').addEventListener('click', async () => {
  const stopBtn = document.getElementById('stopBtn');
  stopBtn.disabled = true;
  stopBtn.textContent = '■ Stopping…';
  try {
    await fetch('/stop-analysis', { method: 'POST' });
  } catch(e) {}
});

async function pollStatus() {
  try {
    const res = await fetch('/status');
    const s = await res.json();

    // Update logs
    if (s.logs && s.logs.length) {
      // Configure & Run log panel
      document.getElementById('logOutput').textContent = s.logs.join('\n');
      document.getElementById('logOutput').scrollTop = 999999;
      State.lastLogs = s.logs;

      // Network View live log — colorized
      const netLog = document.getElementById('networkLogOutput');
      if (netLog) {
        netLog.innerHTML = s.logs.map(line => {
          let cls = '';
          if (line.startsWith('[Agent]') && line.includes('active'))   cls = 'log-agent-active';
          else if (line.startsWith('[Agent]') && line.includes('done')) cls = 'log-agent-done';
          else if (line.startsWith('[Gate]'))                           cls = 'log-gate';
          else if (line.startsWith('[WATCHDOG]'))                       cls = 'log-warn';
          else if (line.startsWith('ERROR') || line.includes('ERROR')) cls = 'log-error';
          else if (line.startsWith('Starting Analysis'))                cls = 'log-start';
          else if (line.startsWith('Pipeline complete'))                cls = 'log-done';
          const escaped = line.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
          return cls ? `<span class="${cls}">${escaped}</span>` : escaped;
        }).join('\n');
        netLog.scrollTop = netLog.scrollHeight;
      }
    }

    // Update agent states from server
    if (s.agent_states) {
      State.agentStates = s.agent_states;
      updateAgentCards(s.agent_states, s.logs || []);
    }

    // Timeout / stall banner
    const banner = document.getElementById('timeoutBanner');
    if (banner) {
      if (s.timed_out && s.running) {
        banner.style.display = 'flex';
        const maxRetries = 2;
        document.getElementById('timeoutMsg').textContent =
          `Agent stalled — restarting from beginning…`;
        document.getElementById('retryBadge').textContent =
          `Retry ${s.retry_count}/${maxRetries}`;
      } else if (!s.timed_out || !s.running) {
        banner.style.display = 'none';
      }
    }

    setLiveBadge(s.running);

    if (!s.running) {
      clearInterval(State.pollInterval);
      State.pollInterval = null;
      if (State.elapsedInterval) { clearInterval(State.elapsedInterval); State.elapsedInterval = null; }
      resetRunBtn();

      if (s.error) {
        setLogStatus('error', 'Analysis failed — see log above.');
        updateDots('error');
        if (banner) banner.style.display = 'none';
      } else if (s.report_ready) {
        setLogStatus('done', 'Analysis complete — loading results…');
        updateDots('done');
        if (banner) banner.style.display = 'none';
        // Mark all agent states done
        Object.keys(State.agentStates).forEach(k => { State.agentStates[k] = 'done'; });
        // Fetch and render report
        setTimeout(async () => {
          try {
            const rep = await fetch('/report').then(r => r.json());
            State.reportData = rep;
            showPage('results');
          } catch(e) {}
        }, 1000);
      } else {
        setLogStatus('done', 'Finished.');
        updateDots('idle');
      }
    }
  } catch(e) {}
}

// ── Agent Card Updates ───────────────────────────────────────────────────────

const CARD_LOG_HINTS = {
  scraper:  { idle: 'Awaiting activation…',        active: 'Scraping social platforms…',  done: 'Data collection complete' },
  analyst:  { idle: 'Awaiting data from Scraper…', active: 'Analysing engagement data…',  done: 'Analysis complete' },
  reporter: { idle: 'Awaiting analysis…',           active: 'Composing intelligence report…', done: 'Report compiled' },
  gate:     { idle: 'Awaiting pipeline output…',   active: 'Validating · computing spend…', done: 'Report approved ✓' },
};

function updateAgentCards(agentStates, logs) {
  Object.entries(agentStates).forEach(([id, status]) => {
    const card  = document.getElementById('acard-' + id);
    const badge = document.getElementById('abadge-' + id);
    const logEl = document.getElementById('alog-' + id);
    if (!card || !badge || !logEl) return;

    // Card class
    card.className = 'agent-card ' + status;

    // Badge
    badge.className = 'acard-badge ' + status;
    badge.textContent = status === 'active' ? '● ACTIVE' : status === 'done' ? '✓ DONE' : '○ IDLE';

    // Log line — find the last log entry mentioning this agent label
    const agentLabel = { scraper: 'scraper', analyst: 'analyst', reporter: 'reporter', gate: 'gate' }[id];
    const hint = CARD_LOG_HINTS[id]?.[status] || '';
    const match = [...logs].reverse().find(l => l.toLowerCase().includes(agentLabel));
    if (match && status === 'active') {
      // Trim to fit — strip brackets/prefixes
      const clean = match.replace(/^\[.*?\]\s*/, '').substring(0, 52);
      logEl.textContent = clean + (match.length > 52 ? '…' : '');
    } else {
      logEl.textContent = hint;
    }
  });
}

function resetAgentCards() {
  ['scraper','analyst','reporter','gate'].forEach(id => {
    const card  = document.getElementById('acard-' + id);
    const badge = document.getElementById('abadge-' + id);
    const logEl = document.getElementById('alog-' + id);
    if (!card || !badge || !logEl) return;
    card.className = 'agent-card';
    badge.className = 'acard-badge idle';
    badge.textContent = '○ IDLE';
    logEl.textContent = CARD_LOG_HINTS[id]?.idle || 'Awaiting…';
  });
}

function resetRunBtn() {
  const btn = document.getElementById('runBtn');
  btn.disabled = false;
  const icon = document.getElementById('runBtnIcon');
  icon.textContent = '▶';
  icon.classList.remove('spinner-icon');
  document.getElementById('runBtnText').textContent = 'Run Analysis';
  const stopBtn = document.getElementById('stopBtn');
  if (stopBtn) { stopBtn.disabled = true; stopBtn.textContent = '■ Stop'; }
}

function resetForm() {
  // Clear text inputs
  document.getElementById('advertiser').value = '';
  document.getElementById('keywords').value = '';
  document.getElementById('cpmRate').value = '15.00';

  // Clear competitor chips
  State.competitors = [];
  renderTags();
  document.getElementById('tagInput').value = '';

  // Reset country to default (Global)
  document.getElementById('country').value = '';

  // Reset platforms to TikTok + Instagram checked
  document.querySelectorAll('.platform-option').forEach(label => {
    const cb = label.querySelector('input[type=checkbox]');
    const val = cb.value;
    const checked = (val === 'TikTok' || val === 'Instagram');
    cb.checked = checked;
    label.classList.toggle('checked', checked);
  });

  // Reset date preset to "Last 30 days"
  document.querySelectorAll('.date-preset').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.days === '30');
  });
  document.getElementById('dateCustom').classList.remove('show');
  document.getElementById('dateFrom').value = '';
  document.getElementById('dateTo').value = '';
  State.dateRange = 'Last 30 days';
  State.customDateFrom = '';
  State.customDateTo = '';

  // Reset depth to deep
  document.querySelectorAll('.depth-option').forEach(label => {
    const val = label.querySelector('input').value;
    label.classList.toggle('active', val === 'deep');
  });
  State.depth = 'deep';

  // Hide log panel
  showLogPanel(false);
}

document.getElementById('resetBtn').addEventListener('click', resetForm);

function showLogPanel(show) {
  document.getElementById('logPanel').classList.toggle('show', show);
}

function setLogStatus(state, text) {
  const spinner = document.getElementById('logSpinner');
  spinner.className = 'log-spinner ' + state;
  document.getElementById('logStatus').textContent = text;
}

function updateDots(state) {
  const dotN = document.getElementById('dot-network');
  const dotC = document.getElementById('dot-configure');
  dotC.className = 'status-dot ' + (state === 'running' ? 'running' : state === 'error' ? 'error' : '');
  dotN.className = 'status-dot ' + (state === 'running' ? 'running' : state === 'done' ? 'done' : '');
}

function setLiveBadge(running) {
  const badge = document.getElementById('liveBadge');
  const text  = document.getElementById('liveText');
  if (running) {
    badge.classList.add('connected');
    text.textContent = 'Live';
  } else {
    badge.classList.remove('connected');
    text.textContent = 'Idle';
  }
}

// ═══════════════════════════════════════════════════════════════════════════════
// NETWORK CANVAS — Silicon Boardroom dark theme (faithful JS port)
// ═══════════════════════════════════════════════════════════════════════════════

// Silicon Boardroom palette
const SB = {
  bg:      '#050a14',
  bg2:     '#060d1c',
  bg3:     '#0a1628',
  bg4:     '#060e1e',
  fg:      '#c9d1d9',
  fg2:     '#8b949e',
  fg3:     '#3d6fa8',
  grid:    '#1e90ff',
  accent:  '#1f6feb',
  success: '#22c55e',
  idleBorder: '#2e5080',
};

// Blend two hex colors: 0=c1, 1=c2
function hexBlend(c1, c2, t) {
  const h = s => [parseInt(s.slice(1,3),16), parseInt(s.slice(3,5),16), parseInt(s.slice(5,7),16)];
  const [r1,g1,b1] = h(c1), [r2,g2,b2] = h(c2);
  const r = Math.round(r1 + (r2-r1)*t);
  const g = Math.round(g1 + (g2-g1)*t);
  const b = Math.round(b1 + (b2-b1)*t);
  return `#${r.toString(16).padStart(2,'0')}${g.toString(16).padStart(2,'0')}${b.toString(16).padStart(2,'0')}`;
}

const AGENTS = {
  scraper:  { label: 'SCRAPER',       role: 'Social Data Scraper',    color: '#1f6feb' },
  analyst:  { label: 'ANALYST',       role: 'Engagement Analyst',     color: '#38bdf8' },
  reporter: { label: 'REPORTER',      role: 'Intelligence Reporter',  color: '#a78bfa' },
  gate:     { label: 'APPROVAL GATE', role: 'Math · Sanitize · Score',color: '#22c55e' },
};

const NODE_POS = {
  scraper:  { x: 0.50, y: 0.20 },
  analyst:  { x: 0.25, y: 0.55 },
  reporter: { x: 0.75, y: 0.55 },
  gate:     { x: 0.50, y: 0.82 },
};

const EDGES = [
  { from: 'scraper',  to: 'analyst'  },
  { from: 'scraper',  to: 'reporter' },
  { from: 'analyst',  to: 'gate'     },
  { from: 'reporter', to: 'gate'     },
];

let _canvas = null;
let _ctx    = null;
let _pulse  = 0;
let _gridOff = 0;
let _rafId  = null;
let _packets = [];

function initNetworkCanvas() {
  _canvas = document.getElementById('networkCanvas');
  _ctx    = _canvas.getContext('2d');
  resizeCanvas();
  window.addEventListener('resize', resizeCanvas);
  _canvas.addEventListener('mousemove', onCanvasHover);
  _canvas.addEventListener('mouseleave', hidePopover);
}

function resizeCanvas() {
  if (!_canvas) return;
  const wrap = _canvas.parentElement;
  _canvas.width  = wrap.clientWidth;
  _canvas.height = wrap.clientHeight;
}

function nodePixel(id) {
  const p = NODE_POS[id];
  return { x: p.x * _canvas.width, y: p.y * _canvas.height };
}

function drawNetworkLoop() {
  if (_rafId) cancelAnimationFrame(_rafId);
  let firstFrame = true;
  function frame() {
    if (!_canvas || !_ctx) return;
    if (firstFrame) { resizeCanvas(); firstFrame = false; }
    drawNetwork();
    _pulse   += 1;
    _gridOff  = (_gridOff + 0.4) % 24;
    _rafId    = requestAnimationFrame(frame);
  }
  _rafId = requestAnimationFrame(frame);
}

function drawNetwork() {
  const W = _canvas.width, H = _canvas.height, ctx = _ctx;

  // Dark background fill
  ctx.fillStyle = SB.bg;
  ctx.fillRect(0, 0, W, H);

  // Scrolling grid lines (Silicon Boardroom style)
  const off = _gridOff % 24;
  ctx.strokeStyle = hexBlend(SB.bg, SB.grid, 0.07);
  ctx.lineWidth = 1;
  ctx.beginPath();
  for (let x = -24 + off; x < W + 24; x += 24) {
    ctx.moveTo(x, 0); ctx.lineTo(x, H);
  }
  for (let y = -24 + off; y < H + 24; y += 24) {
    ctx.moveTo(0, y); ctx.lineTo(W, y);
  }
  ctx.stroke();

  // Edges
  EDGES.forEach(e => drawEdge(ctx, e));

  // Spawn packets
  if (_pulse % 15 === 0 && Math.random() < 0.7) {
    const active = EDGES.filter(e => (State.agentStates[e.from] || 'idle') === 'active');
    if (active.length) {
      const e = active[Math.floor(Math.random() * active.length)];
      _packets.push({
        from: e.from, to: e.to, t: 0,
        speed: 0.012 + Math.random() * 0.008,
        color: AGENTS[e.from].color,
      });
    }
  }

  // Draw packets
  _packets = _packets.filter(p => { p.t += p.speed; return p.t < 1.0; });
  _packets.forEach(p => {
    const a = nodePixel(p.from), b = nodePixel(p.to);
    const px = a.x + (b.x - a.x) * p.t;
    const py = a.y + (b.y - a.y) * p.t;
    [[8, 0.12], [5, 0.28], [3, 0.65]].forEach(([r, alpha]) => {
      ctx.save();
      ctx.globalAlpha = alpha;
      ctx.fillStyle = hexBlend(SB.bg, p.color, 1.0);
      ctx.beginPath(); ctx.arc(px, py, r, 0, Math.PI * 2); ctx.fill();
      ctx.restore();
    });
  });

  // Nodes
  Object.keys(AGENTS).forEach(id => drawNode(ctx, id, W, H));

  // Footer label
  ctx.save();
  ctx.font = 'bold 9px Menlo, "SF Mono", monospace';
  ctx.fillStyle = SB.fg3;
  ctx.textAlign = 'left';
  ctx.textBaseline = 'bottom';
  ctx.fillText('HERMES · NEURAL AGENT NETWORK', 12, H - 8);
  ctx.restore();
}

function drawEdge(ctx, edge) {
  const a = nodePixel(edge.from), b = nodePixel(edge.to);
  const stFrom = State.agentStates[edge.from] || 'idle';
  const stTo   = State.agentStates[edge.to]   || 'idle';
  const isActive = stFrom === 'active' || stTo === 'active';
  const isDone   = stFrom === 'done'   && stTo === 'done';
  const color    = AGENTS[edge.from].color;

  if (isActive) {
    // 3-layer glow (outer → inner → core)
    [[8, hexBlend(SB.bg, color, 0.12)],
     [4, hexBlend(SB.bg, color, 0.22)],
     [2, hexBlend(SB.bg, color, 0.60)]].forEach(([w, c]) => {
      ctx.save();
      ctx.strokeStyle = c; ctx.lineWidth = w;
      ctx.setLineDash([]); ctx.lineCap = 'round';
      ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
      ctx.restore();
    });
  } else if (isDone) {
    ctx.save();
    ctx.strokeStyle = hexBlend(SB.bg, SB.success, 0.30);
    ctx.lineWidth = 1; ctx.setLineDash([]);
    ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
    ctx.restore();
  } else {
    ctx.save();
    ctx.strokeStyle = '#182840';
    ctx.lineWidth = 1; ctx.setLineDash([4, 8]);
    ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
    ctx.restore();
  }

  // Terminal pip at destination
  const pipColor = isActive ? hexBlend(SB.bg, color, 0.60)
                 : isDone   ? hexBlend(SB.bg, SB.success, 0.30)
                 : '#182840';
  const pipR = isActive ? 3 : 2;
  ctx.save();
  ctx.fillStyle = pipColor; ctx.setLineDash([]);
  ctx.beginPath(); ctx.arc(b.x, b.y, pipR, 0, Math.PI * 2); ctx.fill();
  ctx.restore();
}

function drawNode(ctx, id, W, H) {
  const { x, y } = nodePixel(id);
  const agent    = AGENTS[id];
  const status   = State.agentStates[id] || 'idle';
  const isActive = status === 'active';
  const isDone   = status === 'done';
  const color    = agent.color;
  const pulse    = _pulse;
  const sinFast  = Math.sin(pulse * 0.25);
  const sinSlow  = Math.sin(pulse * 0.12);
  const intensity = 0.55 + 0.45 * sinFast;

  // Node radius — larger than before
  const nr = isActive ? 22 : isDone ? 20 : 18;

  // ── Active: multi-layer bloom ─────────────────────────────────────────────
  if (isActive) {
    // Outer atmospheric bloom (3 layers)
    [[56, 0.04], [40, 0.08], [26, 0.15]].forEach(([r, alpha]) => {
      ctx.save();
      ctx.globalAlpha = alpha * intensity;
      const grad = ctx.createRadialGradient(x, y, 0, x, y, r);
      grad.addColorStop(0, color);
      grad.addColorStop(1, SB.bg);
      ctx.fillStyle = grad;
      ctx.beginPath(); ctx.arc(x, y, r, 0, Math.PI * 2); ctx.fill();
      ctx.restore();
    });
    // Pulsing outer ring
    const outerR = nr + 8 + 5 * sinSlow;
    ctx.save();
    ctx.strokeStyle = hexBlend(SB.bg, color, 0.28 * intensity);
    ctx.lineWidth = 1; ctx.setLineDash([]);
    ctx.beginPath(); ctx.arc(x, y, outerR, 0, Math.PI * 2); ctx.stroke();
    ctx.restore();
    // Middle ring
    const midR = nr + 4 + 3 * Math.sin(pulse * 0.20);
    ctx.save();
    ctx.strokeStyle = hexBlend(SB.bg, color, 0.50 * intensity);
    ctx.lineWidth = 1.5; ctx.setLineDash([]);
    ctx.beginPath(); ctx.arc(x, y, midR, 0, Math.PI * 2); ctx.stroke();
    ctx.restore();
  }

  // ── Done: success ring ────────────────────────────────────────────────────
  if (isDone) {
    ctx.save();
    ctx.strokeStyle = hexBlend(SB.bg, SB.success, 0.35);
    ctx.lineWidth = 1.5; ctx.setLineDash([]);
    ctx.beginPath(); ctx.arc(x, y, nr + 5, 0, Math.PI * 2); ctx.stroke();
    ctx.restore();
    // Faint done bloom
    ctx.save();
    ctx.globalAlpha = 0.08;
    const grad = ctx.createRadialGradient(x, y, 0, x, y, nr + 14);
    grad.addColorStop(0, SB.success);
    grad.addColorStop(1, SB.bg);
    ctx.fillStyle = grad;
    ctx.beginPath(); ctx.arc(x, y, nr + 14, 0, Math.PI * 2); ctx.fill();
    ctx.restore();
  }

  // ── Node body — radial gradient fill (Silicon Boardroom style) ───────────
  const innerColor = isActive ? hexBlend(SB.bg, color, 0.22 * intensity)
                   : isDone   ? hexBlend(SB.bg, SB.success, 0.12)
                   : SB.bg3;
  ctx.save();
  const bodyGrad = ctx.createRadialGradient(x - nr * 0.25, y - nr * 0.25, 0, x, y, nr);
  bodyGrad.addColorStop(0, isActive ? hexBlend(SB.bg3, color, 0.30 * intensity) : isDone ? hexBlend(SB.bg3, SB.success, 0.18) : hexBlend(SB.bg3, SB.fg3, 0.08));
  bodyGrad.addColorStop(1, isActive ? '#050f20' : isDone ? '#031208' : '#070e1e');
  ctx.fillStyle = bodyGrad;
  ctx.strokeStyle = isActive ? hexBlend(SB.bg, color, 0.85) : isDone ? SB.success : SB.idleBorder;
  ctx.lineWidth = isActive ? 2 : 1.5;
  ctx.setLineDash([]);
  ctx.beginPath(); ctx.arc(x, y, nr, 0, Math.PI * 2);
  ctx.fill(); ctx.stroke();
  ctx.restore();

  // ── Corner pip — blinks when active ──────────────────────────────────────
  const pipVisible = !isActive || Math.sin(pulse * 0.5) > 0;
  if (pipVisible) {
    const dotColor = isActive ? color : isDone ? SB.success : SB.idleBorder;
    const pipR = isActive ? 4 : 3;
    ctx.save();
    if (isActive) {
      // Pip glow
      ctx.globalAlpha = 0.45 * intensity;
      ctx.fillStyle = color;
      ctx.beginPath(); ctx.arc(x + nr - 6, y - nr + 3, pipR + 3, 0, Math.PI * 2); ctx.fill();
      ctx.globalAlpha = 1.0;
    }
    ctx.fillStyle = dotColor;
    ctx.setLineDash([]);
    ctx.beginPath(); ctx.arc(x + nr - 6, y - nr + 3, pipR, 0, Math.PI * 2); ctx.fill();
    ctx.restore();
  }

  // ── Label ─────────────────────────────────────────────────────────────────
  ctx.save();
  ctx.font = 'bold 10px Menlo, "SF Mono", monospace';
  ctx.fillStyle = isActive ? color : isDone ? SB.success : SB.fg3;
  ctx.textAlign = 'center'; ctx.textBaseline = 'top'; ctx.setLineDash([]);
  ctx.fillText(agent.label, x, y + nr + 8);
  ctx.restore();

  // ── Status line / Thinking animation ─────────────────────────────────────
  ctx.save();
  ctx.font = '600 9px Menlo, "SF Mono", monospace';
  ctx.textAlign = 'center'; ctx.textBaseline = 'top'; ctx.setLineDash([]);
  if (isActive) {
    const dots = '.'.repeat((Math.floor(pulse * 0.06) % 3) + 1);
    ctx.fillStyle = hexBlend(SB.bg, color, 0.85);
    ctx.fillText('Thinking' + dots, x, y + nr + 21);
  } else if (isDone) {
    ctx.fillStyle = hexBlend(SB.bg, SB.success, 0.70);
    ctx.fillText('✓ DONE', x, y + nr + 21);
  } else {
    ctx.fillStyle = hexBlend(SB.bg, SB.fg3, 0.55);
    ctx.fillText('○ IDLE', x, y + nr + 21);
  }
  ctx.restore();
}

// ── Popover ──────────────────────────────────────────────────────────────────
const nodePopover = document.getElementById('nodePopover');
let _popoverAgent = null;
let _popoverTO = null;

function getNodeAt(mx, my) {
  if (!_canvas) return null;
  const rect = _canvas.getBoundingClientRect();
  const cx = mx - rect.left;
  const cy = my - rect.top;
  for (const id of Object.keys(NODE_POS)) {
    const { x, y } = nodePixel(id);
    const r = State.agentStates[id] === 'active' ? 26 : 22;
    if (Math.hypot(cx - x, cy - y) <= r) return id;
  }
  return null;
}

function onCanvasHover(e) {
  const id = getNodeAt(e.clientX, e.clientY);
  if (id === _popoverAgent) return;
  hidePopover();
  if (!id) return;
  _popoverAgent = id;
  clearTimeout(_popoverTO);
  _popoverTO = setTimeout(() => showPopover(id, e.clientX, e.clientY), 120);
}

function showPopover(id, mx, my) {
  const agent  = AGENTS[id];
  const status = State.agentStates[id] || 'idle';

  document.getElementById('popoverName').textContent = agent.label;
  document.getElementById('popoverRole').textContent = agent.role;

  const badge = document.getElementById('popoverBadge');
  badge.textContent  = status.toUpperCase();
  badge.className    = 'popover-status-badge ' + status;

  const lastLog = [...State.lastLogs].reverse().find(l =>
    l.toLowerCase().includes(agent.label.split(' ')[0].toLowerCase())
  ) || '';
  document.getElementById('popoverLog').textContent = lastLog
    ? lastLog.substring(0, 100) + (lastLog.length > 100 ? '…' : '')
    : 'No log output yet.';

  const pop = nodePopover;
  pop.style.display = 'block';
  // Position — keep on screen
  const PW = 280, PH = 160;
  let left = mx + 16;
  let top  = my - 80;
  if (left + PW > window.innerWidth  - 8) left = mx - PW - 16;
  if (top  < 8)                           top  = 8;
  if (top  + PH > window.innerHeight - 8) top  = window.innerHeight - PH - 8;
  pop.style.left = left + 'px';
  pop.style.top  = top  + 'px';
  pop.classList.add('show');
}

function hidePopover() {
  _popoverAgent = null;
  clearTimeout(_popoverTO);
  nodePopover.classList.remove('show');
  nodePopover.style.display = 'none';
}

// ═══════════════════════════════════════════════════════════════════════════════
// RESULTS RENDERING
// ═══════════════════════════════════════════════════════════════════════════════

const CHART_OPTS = {
  responsive: true,
  maintainAspectRatio: true,
  plugins: { legend: { display: false } },
  scales: {
    x: { grid: { color: C.chartGrid }, ticks: { color: C.chartTick, font: { size: 11 } } },
    y: { grid: { color: C.chartGrid }, ticks: { color: C.chartTick, font: { size: 11 } } },
  },
};

function renderResults(data) {
  if (!data || !data.competitors || !data.competitors.length) {
    document.getElementById('noDataState').style.display = 'block';
    document.getElementById('resultsContent').style.display = 'none';
    return;
  }
  document.getElementById('noDataState').style.display = 'none';
  document.getElementById('resultsContent').style.display = 'block';

  const comp = data.competitors;
  const params = data.scan_params || {};

  // Context bar
  buildContextBar(params, comp);

  // KPIs
  const totalSpend  = comp.reduce((s, c) => s + (c.estimated_spend_usd || 0), 0);
  const avgEngage   = comp.reduce((s, c) => s + (c.engagement_rate || 0), 0) / comp.length;
  const platformCts = {};
  comp.forEach(c => { const p = c.platform || 'Unknown'; platformCts[p] = (platformCts[p] || 0) + 1; });
  const topPlatform = Object.entries(platformCts).sort((a,b) => b[1]-a[1])[0]?.[0] || '—';

  document.getElementById('kpiCount').textContent    = comp.length;
  document.getElementById('kpiSpend').textContent    = '$' + totalSpend.toLocaleString(undefined, {maximumFractionDigits:0});
  document.getElementById('kpiEngage').textContent   = avgEngage.toFixed(2) + '%';
  document.getElementById('kpiPlatform').textContent = topPlatform;

  // Charts
  const labels    = comp.map(c => c.name || c.handle || '?');
  const spends    = comp.map(c => c.estimated_spend_usd || 0);
  const engages   = comp.map(c => c.engagement_rate || 0);
  const bgColors  = labels.map((_, i) => BRAND_COLORS[i % BRAND_COLORS.length]);

  destroyCharts();

  Charts.spend = new Chart(document.getElementById('spendChart').getContext('2d'), {
    type: 'bar',
    data: {
      labels,
      datasets: [{ data: spends, backgroundColor: bgColors, borderRadius: 5, borderSkipped: false }],
    },
    options: { ...CHART_OPTS },
  });

  Charts.engage = new Chart(document.getElementById('engageChart').getContext('2d'), {
    type: 'line',
    data: {
      labels,
      datasets: [{
        data: engages,
        borderColor: C.accent,
        backgroundColor: C.accentLt,
        tension: 0.4,
        fill: true,
        pointBackgroundColor: C.accent,
        pointRadius: 5,
        pointBorderColor: '#050a14',
        pointBorderWidth: 2,
      }],
    },
    options: { ...CHART_OPTS },
  });

  // Platform donut
  const platLabels = Object.keys(platformCts);
  const platData   = platLabels.map(k => platformCts[k]);
  Charts.platform = new Chart(document.getElementById('platformChart').getContext('2d'), {
    type: 'doughnut',
    data: {
      labels: platLabels,
      datasets: [{ data: platData, backgroundColor: bgColors, hoverOffset: 6, borderColor: '#060d1c', borderWidth: 2 }],
    },
    options: {
      responsive: true,
      cutout: '62%',
      plugins: { legend: { display: true, position: 'right', labels: { font: { size: 11 }, color: '#8b949e' } } },
    },
  });

  // Sentiment bar
  const sentCts = { Positive: 0, Neutral: 0, Negative: 0 };
  comp.forEach(c => { sentCts[c.sentiment] = (sentCts[c.sentiment] || 0) + 1; });
  Charts.sentiment = new Chart(document.getElementById('sentimentChart').getContext('2d'), {
    type: 'bar',
    data: {
      labels: Object.keys(sentCts),
      datasets: [{
        data: Object.values(sentCts),
        backgroundColor: ['rgba(34,197,94,0.18)', 'rgba(245,158,11,0.18)', 'rgba(248,81,73,0.18)'],
        borderColor:     [C.green, C.amber, C.red],
        borderWidth: 2,
        borderRadius: 5,
        borderSkipped: false,
      }],
    },
    options: {
      ...CHART_OPTS,
      plugins: { legend: { display: false } },
    },
  });

  // Content intelligence cards
  renderContentIntel(comp, bgColors);

  // Table
  const tbody = document.getElementById('resultsTableBody');
  tbody.innerHTML = '';
  comp.forEach((c, i) => {
    const m = c.metrics || {};
    const sentClass = (c.sentiment || '').toLowerCase();
    const color = bgColors[i % bgColors.length];
    tbody.innerHTML += `
      <tr>
        <td><span style="display:inline-flex;align-items:center;gap:7px;">
          <span style="width:8px;height:8px;border-radius:50%;background:${color};display:inline-block;flex-shrink:0;"></span>
          <strong>${esc(c.name || '—')}</strong>
        </span></td>
        <td style="color:#64748b;">${esc(c.handle || '—')}</td>
        <td><span class="platform-badge">${esc(c.platform || 'Multi')}</span></td>
        <td>${fmt(m.likes)}</td>
        <td>${fmt(m.comments)}</td>
        <td>${fmt(m.shares)}</td>
        <td>${fmt(m.views)}</td>
        <td><strong>${(c.engagement_rate || 0).toFixed(2)}%</strong></td>
        <td style="color:#2563eb;font-weight:700;">$${(c.estimated_spend_usd || 0).toLocaleString(undefined,{maximumFractionDigits:0})}</td>
        <td><span class="sentiment-badge ${sentClass}">${esc(c.sentiment || '—')}</span></td>
      </tr>`;
  });

  // Top posts by channel
  renderTopPosts(data);

  // Calculation tree
  renderCalcTree(data);

  // Mark results dot done
  document.getElementById('dot-results').className = 'status-dot done';
}

function renderContentIntel(comp, bgColors) {
  const container = document.getElementById('contentIntelBody');
  if (!container) return;

  const hasContent = comp.some(c =>
    (c.top_posts && c.top_posts.length) ||
    (c.hashtags && c.hashtags.length) ||
    (c.content_themes && c.content_themes.length)
  );

  if (!hasContent) {
    container.innerHTML = '<div style="padding:20px 24px;color:var(--text3);font-size:0.82rem;">No post content data available — run a deeper analysis to populate this section.</div>';
    return;
  }

  const grid = document.createElement('div');
  grid.className = 'content-intel-grid';

  comp.forEach((c, i) => {
    const color   = bgColors[i % bgColors.length];
    const posts   = c.top_posts      || [];
    const tags    = c.hashtags       || [];
    const themes  = c.content_themes || [];

    const card = document.createElement('div');
    card.className = 'content-intel-card fade-in';

    let html = `
      <div class="ci-brand-row">
        <span class="ci-color-dot" style="background:${color}"></span>
        <span class="ci-brand-name">${esc(c.name || '—')}</span>
        ${c.handle ? `<span class="ci-handle">${esc(c.handle)}</span>` : ''}
        <span class="platform-badge" style="margin-left:auto;">${esc(c.platform || 'Multi')}</span>
      </div>`;

    if (posts.length) {
      html += `<div class="ci-section-label">Top Posts / Campaigns</div><ul class="ci-posts">`;
      posts.forEach(p => {
        html += `<li class="ci-post-item">${esc(p)}</li>`;
      });
      html += `</ul>`;
    }

    if (tags.length) {
      html += `<div class="ci-section-label">Hashtags</div><div class="ci-tags">`;
      tags.forEach(t => {
        const tag = t.startsWith('#') ? t : '#' + t;
        html += `<span class="hashtag-badge">${esc(tag)}</span>`;
      });
      html += `</div>`;
    }

    if (themes.length) {
      html += `<div class="ci-section-label">Content Themes</div><div class="ci-tags">`;
      themes.forEach(th => {
        html += `<span class="theme-badge">${esc(th)}</span>`;
      });
      html += `</div>`;
    }

    card.innerHTML = html;
    grid.appendChild(card);
  });

  container.innerHTML = '';
  container.appendChild(grid);
}

// ── Top Posts by Platform ────────────────────────────────────────────────────

const PLATFORM_META = {
  'tiktok':    { cls: 'ch-tiktok',    icon: 'pi-tiktok',    label: 'TikTok' },
  'instagram': { cls: 'ch-instagram', icon: 'pi-instagram', label: 'Instagram' },
  'youtube':   { cls: 'ch-youtube',   icon: 'pi-youtube',   label: 'YouTube' },
  'facebook':  { cls: 'ch-facebook',  icon: 'pi-facebook',  label: 'Facebook' },
  'x':         { cls: 'ch-twitter',   icon: 'pi-twitter',   label: 'X / Twitter' },
  'twitter':   { cls: 'ch-twitter',   icon: 'pi-twitter',   label: 'X / Twitter' },
  'linkedin':  { cls: 'ch-linkedin',  icon: 'pi-linkedin',  label: 'LinkedIn' },
};

function platformMeta(platformStr) {
  const key = (platformStr || '').toLowerCase().replace(/[^a-z]/g, '');
  return PLATFORM_META[key] || { cls: 'ch-default', icon: 'pi-default', label: platformStr || 'Social' };
}

function renderTopPosts(data) {
  const section = document.getElementById('topPostsSection');
  const grid    = document.getElementById('topPostsGrid');
  if (!section || !grid) return;

  const comp = data.competitors || [];
  const hasAny = comp.some(c => c.top_posts && c.top_posts.length);
  if (!hasAny) { section.style.display = 'none'; return; }

  // Group by platform → brand → posts
  const byPlatform = {};
  comp.forEach((c, i) => {
    const plat = c.platform || 'Social Media';
    if (!byPlatform[plat]) byPlatform[plat] = [];
    byPlatform[plat].push({
      name:   c.name || '—',
      handle: c.handle || '',
      posts:  c.top_posts || [],
      metrics: c.metrics || {},
      color:  BRAND_COLORS[i % BRAND_COLORS.length],
    });
  });

  const meta_outer = platformMeta; // alias
  grid.className = 'top-posts-grid';
  grid.innerHTML = '';

  Object.entries(byPlatform).forEach(([plat, brands]) => {
    const pm = meta_outer(plat);

    brands.forEach(b => {
      if (!b.posts.length) return;
      const card = document.createElement('div');
      card.className = `channel-card ${pm.cls}`;

      const postCount = Math.min(b.posts.length, 10);

      let postsHtml = '<ul class="channel-posts-list">';
      b.posts.slice(0, 10).forEach((post, idx) => {
        const isTop = idx < 3;
        const m = b.metrics;
        const hasMetrics = m && (m.likes || m.views || m.comments);
        postsHtml += `
          <li class="channel-post-item">
            <div class="post-rank ${isTop ? 'top' : ''}">#${idx + 1}</div>
            <div class="post-content">
              <div class="post-caption">${esc(post)}</div>
              ${hasMetrics && idx === 0 ? `
              <div class="post-meta">
                ${m.likes    ? `<span class="post-stat">♥ <span>${fmt(m.likes)}</span></span>` : ''}
                ${m.comments ? `<span class="post-stat">💬 <span>${fmt(m.comments)}</span></span>` : ''}
                ${m.shares   ? `<span class="post-stat">↗ <span>${fmt(m.shares)}</span></span>` : ''}
                ${m.views    ? `<span class="post-stat">👁 <span>${fmt(m.views)}</span></span>` : ''}
              </div>` : ''}
            </div>
          </li>`;
      });
      postsHtml += '</ul>';

      card.innerHTML = `
        <div class="channel-card-header">
          <span class="channel-platform-icon ${pm.icon}"></span>
          <div class="channel-header-info">
            <div class="channel-brand-name">
              <span style="display:inline-block;width:7px;height:7px;border-radius:50%;background:${b.color};margin-right:5px;vertical-align:middle;"></span>
              ${esc(b.name)}${b.handle ? ' <span style="font-weight:400;opacity:0.5;font-size:0.78rem;">· ' + esc(b.handle) + '</span>' : ''}
            </div>
            <div class="channel-platform-label">${esc(pm.label)}</div>
          </div>
          <span class="channel-post-count">${postCount} post${postCount !== 1 ? 's' : ''}</span>
        </div>
        ${postsHtml}`;

      grid.appendChild(card);
    });
  });

  section.style.display = grid.children.length ? 'block' : 'none';
}

function renderCalcTree(data) {
  const card = document.getElementById('calcTreeCard');
  const body = document.getElementById('calcTreeBody');
  if (!card || !body) return;

  const a = data.assumptions;
  if (!a) { card.style.display = 'none'; return; }
  card.style.display = 'block';

  // Set CPM override input to current value
  const cpmInput = document.getElementById('cpmOverride');
  if (cpmInput && !cpmInput._seeded) {
    cpmInput.value = a.cpm_rate_usd || 15.0;
    cpmInput._seeded = true;
  }

  const brandColors = BRAND_COLORS;

  let html = `
    <div class="calc-assumptions">
      <div class="calc-pill">CPM Rate: <strong>$${a.cpm_rate_usd || '—'}</strong></div>
      <div class="calc-pill">Data Source: <strong>${esc(a.data_source || 'Web search')}</strong></div>
      <div class="calc-pill">Total Engagement: <strong>${fmt(a.total_engagement)}</strong></div>
      <div class="calc-pill">Total Est. Spend: <strong>$${fmt(a.total_spend_usd)}</strong></div>
    </div>
    <div class="calc-formula-row">
      <div class="calc-formula-label">Spend Formula</div>
      ${esc(a.spend_formula || '')}
    </div>
    <div class="calc-formula-row">
      <div class="calc-formula-label">Engagement Rate Formula</div>
      ${esc(a.engagement_formula || '')}
    </div>
    <div class="calc-formula-row">
      <div class="calc-formula-label">Caveats</div>
      ${esc(a.engagement_proxy || '')} · ${esc(a.views_note || '')} · ${esc(a.cpm_note || '')}
    </div>`;

  if (a.brand_breakdowns && a.brand_breakdowns.length) {
    html += '<div class="calc-brand-grid">';
    a.brand_breakdowns.forEach((b, i) => {
      const color = brandColors[i % brandColors.length];
      html += `
        <div class="calc-brand-card">
          <div class="calc-brand-name">
            <span style="width:8px;height:8px;border-radius:50%;background:${color};display:inline-block;flex-shrink:0;"></span>
            ${esc(b.brand)}
          </div>
          <div class="calc-row"><span>Likes</span><span class="calc-val">${fmt(b.likes)}</span></div>
          <div class="calc-row"><span>Comments</span><span class="calc-val">${fmt(b.comments)}</span></div>
          <div class="calc-row"><span>Shares</span><span class="calc-val">${fmt(b.shares)}</span></div>
          <div class="calc-row"><span>Views</span><span class="calc-val">${fmt(b.views)}</span></div>
          <div class="calc-row"><span>Engagement</span><span class="calc-val accent">${fmt(b.engagement)}</span></div>
          <div class="calc-row"><span>Eng. Rate</span><span class="calc-val green">${b.eng_rate_pct}%</span></div>
          <div class="calc-row"><span>Est. Spend</span><span class="calc-val accent">$${fmt(b.spend_usd)}</span></div>
          <div class="calc-formula-inline">${esc(b.formula || '')}</div>
        </div>`;
    });
    html += '</div>';
  }

  body.innerHTML = html;
}

// Recalc with new CPM rate
document.addEventListener('DOMContentLoaded', () => {});
(function wireRecalc() {
  const btn = document.getElementById('recalcBtn');
  if (!btn) return;
  btn.addEventListener('click', () => {
    const newCpm = parseFloat(document.getElementById('cpmOverride').value);
    if (!newCpm || !State.reportData) return;
    const data = State.reportData;
    const a    = data.assumptions;

    // Recompute every competitor spend
    (data.competitors || []).forEach((c, i) => {
      const m      = c.metrics || {};
      const eng    = (m.likes||0) + (m.comments||0) + (m.shares||0);
      c.estimated_spend_usd = Math.round((eng / 1000) * newCpm * 100) / 100;
      if (a && a.brand_breakdowns && a.brand_breakdowns[i]) {
        const b = a.brand_breakdowns[i];
        b.spend_usd = c.estimated_spend_usd;
        b.formula = `(${b.likes} likes + ${b.comments} comments + ${b.shares} shares) / 1000 × $${newCpm} CPM`;
      }
    });
    if (a) {
      a.cpm_rate_usd   = newCpm;
      a.cpm_note       = `CPM of $${newCpm} applied uniformly across all platforms. Adjust in Config.`;
      a.total_spend_usd = Math.round((data.competitors || []).reduce((s, c) => s + (c.estimated_spend_usd||0), 0) * 100) / 100;
    }

    renderResults(data);
  });
})();

function buildContextBar(params, comp) {
  const bar = document.getElementById('contextBar');
  const parts = [];
  if (params.advertiser) parts.push(`<strong>${esc(params.advertiser)}</strong>`);
  if (params.competitors && params.competitors.length)
    parts.push('vs ' + params.competitors.map(c => `<span class="ctx-badge">${esc(c)}</span>`).join(' '));
  if (params.country) parts.push(`<span class="ctx-sep">·</span> ${esc(params.country)}`);
  if (params.platforms && params.platforms.length)
    parts.push(`<span class="ctx-sep">·</span> ${params.platforms.map(p => `<span class="ctx-badge">${esc(p)}</span>`).join(' ')}`);
  if (params.date_range) parts.push(`<span class="ctx-sep">·</span> ${esc(params.date_range)}`);
  if (!parts.length && comp.length) parts.push(`${comp.length} competitor(s) analysed`);
  bar.innerHTML = parts.join(' ') || 'Scan complete';
}

function destroyCharts() {
  Object.keys(Charts).forEach(k => { if (Charts[k]) { Charts[k].destroy(); Charts[k] = null; } });
}

function fmt(n) { return n != null ? Number(n).toLocaleString() : '—'; }
function esc(s) {
  return String(s)
    .replace(/&/g,'&amp;')
    .replace(/</g,'&lt;')
    .replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;');
}

// ═══════════════════════════════════════════════════════════════════════════════
// EXPORT
// ═══════════════════════════════════════════════════════════════════════════════

document.getElementById('exportJson').addEventListener('click', () => {
  if (!State.reportData) return;
  const blob = new Blob([JSON.stringify(State.reportData, null, 2)], { type: 'application/json' });
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement('a');
  a.href = url; a.download = 'sociallisten-report.json';
  document.body.appendChild(a); a.click(); document.body.removeChild(a); URL.revokeObjectURL(url);
});

document.getElementById('exportCsv').addEventListener('click', () => {
  if (!State.reportData || !State.reportData.competitors) return;
  const cols = ['name','handle','platform','likes','comments','shares','views','engagement_rate','estimated_spend_usd','sentiment'];
  const rows = State.reportData.competitors.map(c => {
    const m = c.metrics || {};
    return [c.name, c.handle, c.platform, m.likes, m.comments, m.shares, m.views,
            c.engagement_rate, c.estimated_spend_usd, c.sentiment]
      .map(v => `"${String(v||'').replace(/"/g,'""')}"`)
      .join(',');
  });
  const csv  = [cols.join(','), ...rows].join('\n');
  const blob = new Blob([csv], { type: 'text/csv' });
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement('a');
  a.href = url; a.download = 'sociallisten-report.csv';
  document.body.appendChild(a); a.click(); document.body.removeChild(a); URL.revokeObjectURL(url);
});

// ═══════════════════════════════════════════════════════════════════════════════
// INIT — check server connection + pre-load any existing report
// ═══════════════════════════════════════════════════════════════════════════════

(async function init() {
  // Ping server
  try {
    const s = await fetch('/status').then(r => r.json());
    setLiveBadge(s.running);
    if (s.running) {
      // Analysis in progress — jump to network view, start polling
      updateDots('running');
      State.pollInterval = setInterval(pollStatus, 2000);
      showPage('network');
      showLogPanel(true);
      setLogStatus('running', 'Analysis in progress…');
    }
  } catch(e) { /* server offline */ }

  // Load existing report if any
  try {
    const rep = await fetch('/report').then(r => r.json());
    if (rep && rep.competitors && rep.competitors.length) {
      State.reportData = rep;
      document.getElementById('dot-results').className = 'status-dot done';
    }
  } catch(e) {}
})();
