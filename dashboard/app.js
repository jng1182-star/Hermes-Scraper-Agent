/* ─────────────────────────────────────────────────────────────────────────
   Hermes — app.js  v2
   Page router · Form state · Canvas neural graph · /status polling · Charts
   ───────────────────────────────────────────────────────────────────────── */

'use strict';

// ── Platform constant — single source of truth ───────────────────────────────
const ACTIVE_PLATFORMS = ['facebook', 'instagram', 'youtube', 'tiktok'];

// ── State ────────────────────────────────────────────────────────────────────
const State = {
  advertisers: [],          // tag chips — your brand(s)
  competitors: [],          // tag chips — competitors
  dateRange: 'Last 30 days',
  customDateFrom: '',
  customDateTo: '',
  depth: 'deep',
  postType: 'both',         // paid | organic | both
  pollInterval: null,
  elapsedInterval: null,
  elapsedStart: null,
  reportData: null,
  normalizedReport: null,   // cached normalizeReportData result; invalidated on reportData change
  activePlatforms: new Set(),
  activePostTypeFilter: 'all', // all | paid | organic  (results page)
  timeGrain: 'lifetime',    // lifetime | monthly | weekly | daily
  // Drill-down filter state
  dd: {
    view:        'all',   // all | mine | competitors | vs
    brand:       '',      // specific brand name (non-vs views), '' = all
    platform:    'all',   // all | tiktok | instagram | youtube | facebook
    postType:    'all',   // all | paid | organic
    market:      'all',   // all | specific market name
    vsMyBrands:  [],      // selected "my brand" names for vs view
    vsCompBrands:[],      // selected competitor names for vs view
  },
  agentStates: { profile: 'idle', feed: 'idle', scraper: 'idle', analyst: 'idle', reporter: 'idle', gate: 'idle' },
  lastLogs: [],
  uploadedFiles: [],
  partialShown: false,      // true once partial results have been rendered this run
  pollFailCount: 0,         // consecutive poll failures for connection-lost detection
};

const Charts = { sovComposite: null, platformSov: null, signalBreakdown: null, confidence: null, sovTrend: null };

// ── Colour palette ───────────────────────────────────────────────────────────
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
      setTimeout(resizeCanvas, 80);
    }
  } else {
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
// TAG INPUTS — Advertisers + Competitors
// ═══════════════════════════════════════════════════════════════════════════════

function _makeTagInput(wrapId, inputId, stateArray) {
  const wrap  = document.getElementById(wrapId);
  const input = document.getElementById(inputId);
  if (!wrap || !input) return;

  function addTag(val) {
    val = val.trim();
    if (!val || stateArray.includes(val)) return;
    stateArray.push(val);
    renderTags();
  }
  function removeTag(val) {
    const idx = stateArray.indexOf(val);
    if (idx !== -1) stateArray.splice(idx, 1);
    renderTags();
  }
  function renderTags() {
    wrap.querySelectorAll('.tag-chip').forEach(c => c.remove());
    stateArray.forEach(val => {
      const chip = document.createElement('span');
      chip.className = 'tag-chip';
      chip.innerHTML = val + '<button type="button" aria-label="remove">×</button>';
      chip.querySelector('button').addEventListener('click', () => removeTag(val));
      wrap.insertBefore(chip, input);
    });
  }
  input.addEventListener('keydown', e => {
    if ((e.key === 'Enter' || e.key === ',') && input.value.trim()) {
      e.preventDefault();
      addTag(input.value);
      input.value = '';
    }
    if (e.key === 'Backspace' && !input.value && stateArray.length) {
      removeTag(stateArray[stateArray.length - 1]);
    }
  });
  wrap.addEventListener('click', () => input.focus());
  return { addTag, removeTag, renderTags };
}

const _advCtrl  = _makeTagInput('advertiserWrap', 'advertiserInput', State.advertisers);
const _compCtrl = _makeTagInput('tagWrap',         'tagInput',        State.competitors);

// ── Platform checkboxes ──────────────────────────────────────────────────────
document.querySelectorAll('.platform-option').forEach(label => {
  label.addEventListener('click', () => {
    const cb = label.querySelector('input[type=checkbox]');
    cb.checked = !cb.checked;
    label.classList.toggle('checked', cb.checked);
  });
  label.querySelector('input').addEventListener('click', e => e.stopPropagation());
});

// ── Post type radios ─────────────────────────────────────────────────────────
document.querySelectorAll('#postTypeOptions .depth-option').forEach(label => {
  label.addEventListener('click', () => {
    document.querySelectorAll('#postTypeOptions .depth-option').forEach(l => l.classList.remove('active'));
    label.classList.add('active');
    State.postType = label.querySelector('input').value;
  });
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
      State.dateRange = `Last ${days} days`;
    }
  });
});

document.getElementById('dateFrom').addEventListener('change', e => { State.customDateFrom = e.target.value; });
document.getElementById('dateTo').addEventListener('change', e => { State.customDateTo = e.target.value; });

// ── Depth radio ──────────────────────────────────────────────────────────────
document.querySelectorAll('.depth-option').forEach(label => {
  label.addEventListener('click', () => {
    const inp = label.querySelector('input[name=depth]');
    if (!inp) return;
    document.querySelectorAll('.depth-option').forEach(l => {
      if (l.querySelector('input[name=depth]')) l.classList.remove('active');
    });
    label.classList.add('active');
    State.depth = inp.value;
  });
});

// ── File upload zone ─────────────────────────────────────────────────────────
const fileInput = document.getElementById('fileInput');
const fileList  = document.getElementById('fileList');
const dropZone  = document.getElementById('fileUploadZone');

function _uploadFiles(files) {
  if (!files || !files.length) return;
  const fd = new FormData();
  Array.from(files).forEach(f => fd.append('files', f));
  fetch('/upload-file', { method: 'POST', body: fd })
    .then(r => r.json())
    .then(d => {
      if (d.files) {
        d.files.forEach(f => {
          if (!State.uploadedFiles.find(u => u.name === f.name))
            State.uploadedFiles.push(f);
        });
        renderFileList();
      }
    })
    .catch(() => {});
}

function renderFileList() {
  if (!fileList) return;
  fileList.innerHTML = '';
  State.uploadedFiles.forEach(f => {
    const row = document.createElement('div');
    row.className = 'file-item';
    const icon = { '.xlsx':'📊','.xls':'📊','.csv':'📋','.pdf':'📄','.docx':'📝','.json':'🗂','.png':'🖼','.jpg':'🖼','.jpeg':'🖼','.webp':'🖼' };
    const ext = '.' + (f.name.split('.').pop() || '').toLowerCase();
    const emoji = icon[ext] || '📎';
    const kb = f.size ? ` · ${(f.size / 1024).toFixed(1)} KB` : '';
    const parsed = f.parsed ? '<span class="file-parsed">✓ parsed</span>' : '';
    row.innerHTML = `<span class="file-icon">${emoji}</span><span class="file-name">${esc(f.name)}${kb}</span>${parsed}`;
    fileList.appendChild(row);
  });
}

fileInput.addEventListener('change', e => { _uploadFiles(e.target.files); e.target.value = ''; });
dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('drag-over'); });
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag-over'));
dropZone.addEventListener('drop', e => {
  e.preventDefault();
  dropZone.classList.remove('drag-over');
  _uploadFiles(e.dataTransfer.files);
});

// ── Load existing uploads on init ────────────────────────────────────────────
(async function loadUploads() {
  try {
    const d = await fetch('/uploaded-files').then(r => r.json());
    if (d.files) { State.uploadedFiles = d.files; renderFileList(); }
  } catch(e) {}
})();

// ── report.json upload (results page) ────────────────────────────────────────
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
  // Auto-commit unsubmitted text
  const advInput = document.getElementById('advertiserInput');
  if (advInput && advInput.value.trim()) {
    advInput.value.split(',').forEach(v => { const t=v.trim(); if(t) State.advertisers.push(t); });
    advInput.value = '';
  }
  const tagInputEl = document.getElementById('tagInput');
  if (tagInputEl && tagInputEl.value.trim()) {
    tagInputEl.value.split(',').forEach(v => { const t=v.trim(); if(t) State.competitors.push(t); });
    tagInputEl.value = '';
  }

  const platforms = [];
  document.querySelectorAll('.platform-option input:checked').forEach(cb => platforms.push(cb.value));

  let dateRange = State.dateRange;
  let dateFrom = null, dateTo = null;
  if (dateRange === 'custom') {
    dateFrom = document.getElementById('dateFrom').value;
    dateTo   = document.getElementById('dateTo').value;
    if (dateFrom && dateTo) dateRange = `${dateFrom} to ${dateTo}`;
  } else {
    dateFrom = document.getElementById('dateFrom').value || null;
    dateTo   = document.getElementById('dateTo').value   || null;
  }

  // Multi-select country (markets)
  const countryEl = document.getElementById('country');
  const markets = Array.from(countryEl.selectedOptions).map(o => o.value).filter(Boolean);
  const country = markets[0] || '';   // backward compat — primary market

  const industry = document.getElementById('industry').value;

  return {
    advertisers:  [...State.advertisers],
    advertiser:   State.advertisers[0] || '',          // backward compat
    competitors:  [...State.competitors],
    country,
    markets,                                            // all selected markets
    industry,
    platforms,
    post_type:    State.postType,
    date_range:   dateRange,
    date_from:    dateFrom,
    date_to:      dateTo,
    keywords:     document.getElementById('keywords').value.trim(),
    depth:        State.depth,
  };
}

document.getElementById('runBtn').addEventListener('click', async () => {
  const params = getFormParams();

  if (!params.advertisers.length && !params.competitors.length) {
    const inp = document.getElementById('advertiserInput');
    inp.focus(); inp.style.borderColor = 'var(--red)';
    setTimeout(() => inp.style.borderColor = '', 2000);
    setLogStatus('error', 'Enter at least one brand to analyse.');
    showLogPanel(true);
    return;
  }

  const btn = document.getElementById('runBtn');
  btn.disabled = true;
  document.getElementById('runBtnIcon').textContent = '⟳';
  document.getElementById('runBtnIcon').classList.add('spinner-icon');
  document.getElementById('runBtnText').textContent = 'Running…';

  showLogPanel(true);
  setLogStatus('running', 'Analysis running…');
  document.getElementById('logOutput').textContent = '';

  // Enable Stop immediately — before waiting for fetch response
  const stopBtn = document.getElementById('stopBtn');
  if (stopBtn) { stopBtn.disabled = false; stopBtn.textContent = '■ Stop'; }

  State.agentStates = { profile: 'idle', feed: 'idle', scraper: 'idle', analyst: 'idle', reporter: 'idle', gate: 'idle' };
  State.partialShown = false;
  setPartialPill(false);
  resetAgentCards();
  updateDots('running');
  lockForm(true);

  const elapsedBadge = document.getElementById('elapsedBadge');
  if (elapsedBadge) elapsedBadge.style.display = 'flex';
  document.getElementById('elapsedTime').textContent = '0:00';
  State.elapsedStart = Date.now();
  if (State.elapsedInterval) clearInterval(State.elapsedInterval);
  State.elapsedInterval = setInterval(() => {
    const secs = Math.floor((Date.now() - State.elapsedStart) / 1000);
    const m = Math.floor(secs / 60), s = secs % 60;
    const el = document.getElementById('elapsedTime');
    if (el) el.textContent = `${m}:${String(s).padStart(2,'0')}`;
  }, 1000);
  const banner = document.getElementById('timeoutBanner');
  if (banner) banner.style.display = 'none';

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
    setLogStatus('error', 'Could not reach server.');
    resetRunBtn(); updateDots('idle'); return;
  }

  State.pollInterval = setInterval(pollStatus, 2000);
  showPage('network');
});

document.getElementById('stopBtn').addEventListener('click', async () => {
  const stopBtn = document.getElementById('stopBtn');
  if (stopBtn.disabled) return;
  const confirmed = confirm(
    'Stop the analysis?\n\nAny partial results already collected will still be available. The pipeline cannot be resumed — you will need to run again.'
  );
  if (!confirmed) return;
  stopBtn.disabled = true; stopBtn.textContent = '■ Stopping…';
  try { await fetch('/stop-analysis', { method: 'POST' }); } catch(e) {}
});

async function pollStatus() {
  try {
    const res = await fetch('/status');
    const s   = await res.json();

    if (s.logs && s.logs.length) {
      document.getElementById('logOutput').textContent = s.logs.join('\n');
      document.getElementById('logOutput').scrollTop = 999999;
      State.lastLogs = s.logs;

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

    if (s.agent_states) {
      State.agentStates = s.agent_states;
      updateAgentCards(s.agent_states, s.logs || []);
    }

    const banner = document.getElementById('timeoutBanner');
    if (banner) {
      if (s.timed_out && s.running) {
        banner.style.display = 'flex';
        document.getElementById('timeoutMsg').textContent = 'Agent stalled — restarting from beginning…';
        document.getElementById('retryBadge').textContent = `Retry ${s.retry_count}/2`;
      } else if (!s.timed_out || !s.running) {
        banner.style.display = 'none';
      }
    }

    setLiveBadge(s.running);

    // Sync elapsed timer from server when local timer isn't running (page reload mid-run)
    if (s.running && !State.elapsedInterval && typeof s.elapsed_secs === 'number') {
      const elapsedBadge = document.getElementById('elapsedBadge');
      if (elapsedBadge) elapsedBadge.style.display = 'flex';
      State.elapsedStart = Date.now() - s.elapsed_secs * 1000;
      State.elapsedInterval = setInterval(() => {
        const secs = Math.floor((Date.now() - State.elapsedStart) / 1000);
        const m = Math.floor(secs / 60), sec = secs % 60;
        const el = document.getElementById('elapsedTime');
        if (el) el.textContent = `${m}:${String(sec).padStart(2,'0')}`;
      }, 1000);
    }

    // Ensure Stop button is enabled whenever a run is active (covers page-reload mid-run)
    const stopBtn = document.getElementById('stopBtn');
    if (stopBtn && s.running && stopBtn.disabled && stopBtn.textContent.trim() !== '■ Stopping…') {
      stopBtn.disabled = false; stopBtn.textContent = '■ Stop';
    }

    // ── Partial results: render as soon as scraper/analyst checkpoint is ready ──
    if (s.running && s.partial_report && (s.partial_report.brands || s.partial_report.competitors) && (s.partial_report.brands?.length || s.partial_report.competitors?.length)) {
      if (!State.partialShown) {
        State.partialShown = true;
        // Tag scan_params so results page knows it came from params
        if (!s.partial_report.scan_params && State.reportData && State.reportData.scan_params) {
          s.partial_report.scan_params = State.reportData.scan_params;
        }
        _setReportData(s.partial_report);
        showPage('results');
      }
      setPartialPill(true, s.partial_report._partial_phase || 'scraper');
    }

    if (!s.running) {
      clearInterval(State.pollInterval); State.pollInterval = null;
      if (State.elapsedInterval) { clearInterval(State.elapsedInterval); State.elapsedInterval = null; }
      resetRunBtn();
      State.partialShown = false;
      setPartialPill(false);

      if (s.error) {
        setLogStatus('error', 'Analysis failed — see log above.');
        updateDots('error');
        if (banner) banner.style.display = 'none';
        lockForm(false);
      } else if (s.report_ready) {
        setLogStatus('done', 'Analysis complete — loading results…');
        updateDots('done');
        if (banner) banner.style.display = 'none';
        Object.keys(State.agentStates).forEach(k => { State.agentStates[k] = 'done'; });
        setTimeout(async () => {
          try {
            const rep = await fetch('/report').then(r => r.json());
            _setReportData(rep);
            saveRun(rep);
            lockForm(false);
            showPage('results');
          } catch(e) { lockForm(false); }
        }, 1000);
      } else {
        setLogStatus('done', 'Finished.'); updateDots('idle');
      }
    }
    State.pollFailCount = 0; // reset on success
  } catch(e) {
    State.pollFailCount = (State.pollFailCount || 0) + 1;
    const logOut = document.getElementById('logOutput');
    if (logOut) logOut.textContent += `\n[POLL ERROR] ${e.message || e}`;
    if (State.pollFailCount >= 3) {
      setLogStatus('error', 'Connection lost — retrying…');
      const lb = document.getElementById('liveText');
      if (lb) lb.textContent = 'Reconnecting…';
    }
  }
}

// ── Agent Card Updates ───────────────────────────────────────────────────────

const CARD_LOG_HINTS = {
  profile:  { idle: 'Awaiting activation…', active: 'Scraping brand profile pages…',   done: 'Profile baselines collected' },
  feed:     { idle: 'Awaiting activation…', active: 'Scrolling in-feed ads…',          done: 'Feed ads captured' },
  scraper:  { idle: 'Awaiting activation…', active: 'Collecting paid & organic data…', done: 'Data collection complete' },
  analyst:  { idle: 'Awaiting data…',       active: 'Computing 6-signal SOV index…',   done: 'Analysis complete' },
  reporter: { idle: 'Awaiting analysis…',   active: 'Composing intelligence report…',  done: 'Report compiled' },
  gate:     { idle: 'Awaiting output…',     active: 'Validating SOV, consistency & confidence…', done: 'SOV report validated ✓' },
};

function updateAgentCards(agentStates, logs) {
  Object.entries(agentStates).forEach(([id, status]) => {
    const card  = document.getElementById('acard-' + id);
    const badge = document.getElementById('abadge-' + id);
    const logEl = document.getElementById('alog-'   + id);
    if (!card || !badge || !logEl) return;
    card.className  = 'agent-card ' + status;
    badge.className = 'acard-badge ' + status;
    badge.textContent = status === 'active' ? '● ACTIVE' : status === 'done' ? '✓ DONE' : '○ IDLE';
    const hint  = CARD_LOG_HINTS[id]?.[status] || '';
    const match = [...logs].reverse().find(l => l.toLowerCase().includes(id));
    if (match && status === 'active') {
      const clean = match.replace(/^\[.*?\]\s*/, '').substring(0, 52);
      logEl.textContent = clean + (match.length > 52 ? '…' : '');
    } else {
      logEl.textContent = hint;
    }
  });
}

function resetAgentCards() {
  ['profile','feed','scraper','analyst','reporter','gate'].forEach(id => {
    const card  = document.getElementById('acard-' + id);
    const badge = document.getElementById('abadge-' + id);
    const logEl = document.getElementById('alog-'   + id);
    if (!card || !badge || !logEl) return;
    card.className  = 'agent-card';
    badge.className = 'acard-badge idle';
    badge.textContent = '○ IDLE';
    logEl.textContent = CARD_LOG_HINTS[id]?.idle || 'Awaiting…';
  });
}

function resetRunBtn() {
  const btn = document.getElementById('runBtn');
  btn.disabled = false;
  const icon = document.getElementById('runBtnIcon');
  icon.textContent = '▶'; icon.classList.remove('spinner-icon');
  document.getElementById('runBtnText').textContent = 'Run Analysis';
  const stopBtn = document.getElementById('stopBtn');
  if (stopBtn) { stopBtn.disabled = true; stopBtn.textContent = '■ Stop'; }
}

function lockForm(locked) {
  const cfg = document.getElementById('page-configure');
  if (!cfg) return;
  cfg.classList.toggle('form-locked', locked);
  cfg.querySelectorAll('input:not(#resetBtn), select, textarea').forEach(el => {
    el.disabled = locked;
  });
  cfg.querySelectorAll('.platform-option, .depth-option, .date-preset').forEach(el => {
    el.style.pointerEvents = locked ? 'none' : '';
    el.style.opacity       = locked ? '0.5'  : '';
  });
  cfg.querySelectorAll('.tag-input-wrap').forEach(el => {
    el.style.pointerEvents = locked ? 'none' : '';
    el.style.opacity       = locked ? '0.5'  : '';
  });
  const fileZone = document.getElementById('fileUploadZone');
  if (fileZone) {
    fileZone.style.pointerEvents = locked ? 'none' : '';
    fileZone.style.opacity       = locked ? '0.5'  : '';
  }
  const runBtn = document.getElementById('runBtn');
  if (runBtn) runBtn.disabled = locked;
  const resetBtn = document.getElementById('resetBtn');
  if (resetBtn) resetBtn.disabled = false; // always clickable
}

function resetForm() {
  document.getElementById('keywords').value = '';
  State.advertisers.length = 0; State.competitors.length = 0;
  if (_advCtrl)  _advCtrl.renderTags();
  if (_compCtrl) _compCtrl.renderTags();
  document.getElementById('advertiserInput').value = '';
  document.getElementById('tagInput').value        = '';
  document.getElementById('country').value   = '';
  document.getElementById('industry').value  = '';

  document.querySelectorAll('.platform-option').forEach(label => {
    const cb  = label.querySelector('input[type=checkbox]');
    const val = cb.value;
    const checked = (val === 'YouTube' || val === 'Facebook');
    cb.checked = checked; label.classList.toggle('checked', checked);
  });

  document.querySelectorAll('.date-preset').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.days === '30');
  });
  document.getElementById('dateCustom').classList.remove('show');
  document.getElementById('dateFrom').value = '';
  document.getElementById('dateTo').value   = '';
  State.dateRange = 'Last 30 days'; State.customDateFrom = ''; State.customDateTo = '';

  document.querySelectorAll('.depth-option').forEach(label => {
    const inp = label.querySelector('input[name=depth]');
    if (inp) label.classList.toggle('active', inp.value === 'deep');
  });
  State.depth = 'deep';

  document.querySelectorAll('#postTypeOptions .depth-option').forEach(label => {
    const inp = label.querySelector('input[name=postType]');
    if (inp) label.classList.toggle('active', inp.value === 'both');
  });
  State.postType = 'both';

  showLogPanel(false);
  lockForm(false);
}

document.getElementById('resetBtn').addEventListener('click', resetForm);

// ── Benchmark status ──────────────────────────────────────────────────────────
async function loadBenchmarkStatus() {
  const el = document.getElementById('benchmarkStatus');
  if (!el) return;
  try {
    const res  = await fetch('/benchmarks-status');
    const data = await res.json();
    if (data.status === 'never_refreshed') {
      el.textContent = 'never refreshed';
      el.style.color = 'var(--warn, #f59e0b)';
    } else if (data.age_days !== null && data.age_days > 14) {
      el.textContent = `stale (${data.age_days}d ago)`;
      el.style.color = 'var(--warn, #f59e0b)';
    } else if (data.updated_at) {
      const d = new Date(data.updated_at);
      el.textContent = `updated ${d.toLocaleDateString()}`;
      el.style.color = 'var(--text3)';
    } else {
      el.textContent = data.status;
    }
  } catch(e) {
    el.textContent = 'unavailable';
  }
}
loadBenchmarkStatus();

document.getElementById('refreshBenchmarksBtn').addEventListener('click', async () => {
  const btn = document.getElementById('refreshBenchmarksBtn');
  const el  = document.getElementById('benchmarkStatus');
  btn.disabled = true; btn.textContent = '↻ Refreshing…';
  if (el) { el.textContent = 'refreshing…'; el.style.color = 'var(--text3)'; }
  try {
    await fetch('/refresh-benchmarks', { method: 'POST' });
    // Poll until age changes or 90s timeout
    let waited = 0;
    const check = setInterval(async () => {
      waited += 3;
      await loadBenchmarkStatus();
      const fresh = document.getElementById('benchmarkStatus');
      if (!fresh || (fresh.textContent.includes('updated') && !fresh.textContent.includes('refreshing')) || waited >= 90) {
        clearInterval(check);
        btn.disabled = false; btn.textContent = '↻ Refresh';
      }
    }, 3000);
  } catch(e) {
    if (el) el.textContent = 'refresh failed';
    btn.disabled = false; btn.textContent = '↻ Refresh';
  }
});

function showLogPanel(show) {
  document.getElementById('logPanel').classList.toggle('show', show);
}
function setLogStatus(state, text) {
  const spinner = document.getElementById('logSpinner');
  spinner.className = 'log-spinner ' + state;
  document.getElementById('logStatus').textContent = text;
}
function updateDots(state) {
  document.getElementById('dot-configure').className =
    'status-dot ' + (state === 'running' ? 'running' : state === 'error' ? 'error' : '');
  document.getElementById('dot-network').className =
    'status-dot ' + (state === 'running' ? 'running' : state === 'done' ? 'done' : '');
}
function setLiveBadge(running) {
  const badge = document.getElementById('liveBadge');
  const text  = document.getElementById('liveText');
  if (running) { badge.classList.add('connected'); text.textContent = 'Live'; }
  else         { badge.classList.remove('connected'); text.textContent = 'Idle'; }
}

function setPartialPill(visible, phase) {
  const pill = document.getElementById('partialPill');
  if (!pill) return;
  if (visible) {
    const phaseLabel = phase === 'reporter' ? 'Reporter' : phase === 'analyst' ? 'Analyst' : 'Scraper';
    pill.querySelector('span:last-child').textContent = `Partial Results · ${phaseLabel} done`;
    pill.style.display = 'flex';
    // Clicking pill navigates to results page
    pill.onclick = () => showPage('results');
  } else {
    pill.style.display = 'none';
    pill.onclick = null;
  }
}

// ═══════════════════════════════════════════════════════════════════════════════
// NETWORK CANVAS — Silicon Boardroom dark theme
// ═══════════════════════════════════════════════════════════════════════════════

const SB = {
  bg:'#050a14', bg2:'#060d1c', bg3:'#0a1628', bg4:'#060e1e',
  fg:'#c9d1d9', fg2:'#8b949e', fg3:'#3d6fa8',
  grid:'#1e90ff', accent:'#1f6feb', success:'#22c55e', idleBorder:'#2e5080',
};

function hexBlend(c1, c2, t) {
  const h = s => [parseInt(s.slice(1,3),16), parseInt(s.slice(3,5),16), parseInt(s.slice(5,7),16)];
  const [r1,g1,b1]=h(c1), [r2,g2,b2]=h(c2);
  const r=Math.round(r1+(r2-r1)*t), g=Math.round(g1+(g2-g1)*t), b=Math.round(b1+(b2-b1)*t);
  return `#${r.toString(16).padStart(2,'0')}${g.toString(16).padStart(2,'0')}${b.toString(16).padStart(2,'0')}`;
}

const AGENTS = {
  profile:  { label:'PROFILE',       role:'Profile Baseline Scraper', color:'#f59e0b' },
  feed:     { label:'FEED',          role:'In-Feed Ad Capture',       color:'#ec4899' },
  scraper:  { label:'SCRAPER',       role:'Search Fallback',          color:'#1f6feb' },
  analyst:  { label:'ANALYST',       role:'Share-of-Voice Analyst',   color:'#38bdf8' },
  reporter: { label:'REPORTER',      role:'SOV Intelligence Reporter',color:'#a78bfa' },
  gate:     { label:'APPROVAL GATE', role:'SOV · Confidence · Consistency', color:'#22c55e' },
};

const NODE_POS = {
  profile:  { x:0.28, y:0.15 },
  feed:     { x:0.72, y:0.15 },
  scraper:  { x:0.50, y:0.35 },
  analyst:  { x:0.25, y:0.60 },
  reporter: { x:0.75, y:0.60 },
  gate:     { x:0.50, y:0.85 },
};

const EDGES = [
  { from:'profile',  to:'analyst'  },
  { from:'feed',     to:'analyst'  },
  { from:'scraper',  to:'analyst'  },
  { from:'profile',  to:'reporter' },
  { from:'feed',     to:'reporter' },
  { from:'analyst',  to:'gate'     },
  { from:'reporter', to:'gate'     },
];

let _canvas=null, _ctx=null, _pulse=0, _gridOff=0, _rafId=null, _packets=[];

function initNetworkCanvas() {
  _canvas = document.getElementById('networkCanvas');
  _ctx    = _canvas.getContext('2d');
  resizeCanvas();
  window.addEventListener('resize', resizeCanvas);
  _canvas.addEventListener('mousemove', onCanvasHover);
  _canvas.addEventListener('mouseleave', hidePopover);
  // Restart RAF loop when tab regains focus (RAF pauses in background tabs)
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) drawNetworkLoop();
  });
  window.addEventListener('pageshow', () => drawNetworkLoop());
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
  let first = true;
  function frame() {
    if (!_canvas||!_ctx) return;
    if (first) { resizeCanvas(); first=false; }
    drawNetwork(); _pulse+=1; _gridOff=(_gridOff+0.4)%24;
    _rafId = requestAnimationFrame(frame);
  }
  _rafId = requestAnimationFrame(frame);
}
function drawNetwork() {
  const W=_canvas.width, H=_canvas.height, ctx=_ctx;
  ctx.fillStyle=SB.bg; ctx.fillRect(0,0,W,H);
  const off=_gridOff%24;
  ctx.strokeStyle=hexBlend(SB.bg,SB.grid,0.07); ctx.lineWidth=1; ctx.beginPath();
  for(let x=-24+off;x<W+24;x+=24){ctx.moveTo(x,0);ctx.lineTo(x,H);}
  for(let y=-24+off;y<H+24;y+=24){ctx.moveTo(0,y);ctx.lineTo(W,y);}
  ctx.stroke();
  EDGES.forEach(e=>drawEdge(ctx,e));
  if(_pulse%15===0&&Math.random()<0.7){
    const active=EDGES.filter(e=>(State.agentStates[e.from]||'idle')==='active');
    if(active.length){
      const e=active[Math.floor(Math.random()*active.length)];
      _packets.push({from:e.from,to:e.to,t:0,speed:0.012+Math.random()*0.008,color:AGENTS[e.from].color});
    }
  }
  _packets=_packets.filter(p=>{p.t+=p.speed;return p.t<1.0;});
  _packets.forEach(p=>{
    const a=nodePixel(p.from), b=nodePixel(p.to);
    const px=a.x+(b.x-a.x)*p.t, py=a.y+(b.y-a.y)*p.t;
    [[8,0.12],[5,0.28],[3,0.65]].forEach(([r,alpha])=>{
      ctx.save(); ctx.globalAlpha=alpha; ctx.fillStyle=hexBlend(SB.bg,p.color,1.0);
      ctx.beginPath(); ctx.arc(px,py,r,0,Math.PI*2); ctx.fill(); ctx.restore();
    });
  });
  Object.keys(AGENTS).forEach(id=>drawNode(ctx,id,W,H));
  ctx.save(); ctx.font='bold 9px Menlo,"SF Mono",monospace'; ctx.fillStyle=SB.fg3;
  ctx.textAlign='left'; ctx.textBaseline='bottom'; ctx.setLineDash([]);
  ctx.fillText('HERMES · NEURAL AGENT NETWORK',12,H-8); ctx.restore();
}
function drawEdge(ctx,edge){
  const a=nodePixel(edge.from), b=nodePixel(edge.to);
  const stFrom=State.agentStates[edge.from]||'idle', stTo=State.agentStates[edge.to]||'idle';
  const isActive=stFrom==='active'||stTo==='active', isDone=stFrom==='done'&&stTo==='done';
  const color=AGENTS[edge.from].color;
  if(isActive){
    [[8,hexBlend(SB.bg,color,0.12)],[4,hexBlend(SB.bg,color,0.22)],[2,hexBlend(SB.bg,color,0.60)]].forEach(([w,c])=>{
      ctx.save(); ctx.strokeStyle=c; ctx.lineWidth=w; ctx.setLineDash([]); ctx.lineCap='round';
      ctx.beginPath(); ctx.moveTo(a.x,a.y); ctx.lineTo(b.x,b.y); ctx.stroke(); ctx.restore();
    });
  } else if(isDone){
    ctx.save(); ctx.strokeStyle=hexBlend(SB.bg,SB.success,0.30); ctx.lineWidth=1; ctx.setLineDash([]);
    ctx.beginPath(); ctx.moveTo(a.x,a.y); ctx.lineTo(b.x,b.y); ctx.stroke(); ctx.restore();
  } else {
    ctx.save(); ctx.strokeStyle='#182840'; ctx.lineWidth=1; ctx.setLineDash([4,8]);
    ctx.beginPath(); ctx.moveTo(a.x,a.y); ctx.lineTo(b.x,b.y); ctx.stroke(); ctx.restore();
  }
  const pipColor=isActive?hexBlend(SB.bg,color,0.60):isDone?hexBlend(SB.bg,SB.success,0.30):'#182840';
  ctx.save(); ctx.fillStyle=pipColor; ctx.setLineDash([]);
  ctx.beginPath(); ctx.arc(b.x,b.y,isActive?3:2,0,Math.PI*2); ctx.fill(); ctx.restore();
}
function drawNode(ctx,id,W,H){
  const {x,y}=nodePixel(id), agent=AGENTS[id];
  const status=State.agentStates[id]||'idle';
  const isActive=status==='active', isDone=status==='done', color=agent.color;
  const pulse=_pulse, sinFast=Math.sin(pulse*0.25), sinSlow=Math.sin(pulse*0.12);
  const intensity=0.55+0.45*sinFast, nr=isActive?22:isDone?20:18;
  if(isActive){
    [[56,0.04],[40,0.08],[26,0.15]].forEach(([r,alpha])=>{
      ctx.save(); ctx.globalAlpha=alpha*intensity;
      const g=ctx.createRadialGradient(x,y,0,x,y,r); g.addColorStop(0,color); g.addColorStop(1,SB.bg);
      ctx.fillStyle=g; ctx.beginPath(); ctx.arc(x,y,r,0,Math.PI*2); ctx.fill(); ctx.restore();
    });
    const outerR=nr+8+5*sinSlow;
    ctx.save(); ctx.strokeStyle=hexBlend(SB.bg,color,0.28*intensity); ctx.lineWidth=1; ctx.setLineDash([]);
    ctx.beginPath(); ctx.arc(x,y,outerR,0,Math.PI*2); ctx.stroke(); ctx.restore();
    const midR=nr+4+3*Math.sin(pulse*0.20);
    ctx.save(); ctx.strokeStyle=hexBlend(SB.bg,color,0.50*intensity); ctx.lineWidth=1.5; ctx.setLineDash([]);
    ctx.beginPath(); ctx.arc(x,y,midR,0,Math.PI*2); ctx.stroke(); ctx.restore();
  }
  if(isDone){
    ctx.save(); ctx.strokeStyle=hexBlend(SB.bg,SB.success,0.35); ctx.lineWidth=1.5; ctx.setLineDash([]);
    ctx.beginPath(); ctx.arc(x,y,nr+5,0,Math.PI*2); ctx.stroke(); ctx.restore();
    ctx.save(); ctx.globalAlpha=0.08;
    const g=ctx.createRadialGradient(x,y,0,x,y,nr+14); g.addColorStop(0,SB.success); g.addColorStop(1,SB.bg);
    ctx.fillStyle=g; ctx.beginPath(); ctx.arc(x,y,nr+14,0,Math.PI*2); ctx.fill(); ctx.restore();
  }
  ctx.save();
  const bg=ctx.createRadialGradient(x-nr*0.25,y-nr*0.25,0,x,y,nr);
  bg.addColorStop(0,isActive?hexBlend(SB.bg3,color,0.30*intensity):isDone?hexBlend(SB.bg3,SB.success,0.18):hexBlend(SB.bg3,SB.fg3,0.08));
  bg.addColorStop(1,isActive?'#050f20':isDone?'#031208':'#070e1e');
  ctx.fillStyle=bg; ctx.strokeStyle=isActive?hexBlend(SB.bg,color,0.85):isDone?SB.success:SB.idleBorder;
  ctx.lineWidth=isActive?2:1.5; ctx.setLineDash([]);
  ctx.beginPath(); ctx.arc(x,y,nr,0,Math.PI*2); ctx.fill(); ctx.stroke(); ctx.restore();
  const pipVisible=!isActive||Math.sin(pulse*0.5)>0;
  if(pipVisible){
    const dotColor=isActive?color:isDone?SB.success:SB.idleBorder, pipR=isActive?4:3;
    ctx.save();
    if(isActive){ ctx.globalAlpha=0.45*intensity; ctx.fillStyle=color; ctx.beginPath(); ctx.arc(x+nr-6,y-nr+3,pipR+3,0,Math.PI*2); ctx.fill(); ctx.globalAlpha=1.0; }
    ctx.fillStyle=dotColor; ctx.setLineDash([]);
    ctx.beginPath(); ctx.arc(x+nr-6,y-nr+3,pipR,0,Math.PI*2); ctx.fill(); ctx.restore();
  }
  ctx.save(); ctx.font='bold 10px Menlo,"SF Mono",monospace';
  ctx.fillStyle=isActive?color:isDone?SB.success:SB.fg3;
  ctx.textAlign='center'; ctx.textBaseline='top'; ctx.setLineDash([]);
  ctx.fillText(agent.label,x,y+nr+8); ctx.restore();
  ctx.save(); ctx.font='600 9px Menlo,"SF Mono",monospace';
  ctx.textAlign='center'; ctx.textBaseline='top'; ctx.setLineDash([]);
  if(isActive){ const dots='.'.repeat((Math.floor(pulse*0.06)%3)+1); ctx.fillStyle=hexBlend(SB.bg,color,0.85); ctx.fillText('Thinking'+dots,x,y+nr+21); }
  else if(isDone){ ctx.fillStyle=hexBlend(SB.bg,SB.success,0.70); ctx.fillText('✓ DONE',x,y+nr+21); }
  else{ ctx.fillStyle=hexBlend(SB.bg,SB.fg3,0.55); ctx.fillText('○ IDLE',x,y+nr+21); }
  ctx.restore();
}

// ── Popover ──────────────────────────────────────────────────────────────────
const nodePopover = document.getElementById('nodePopover');
let _popoverAgent=null, _popoverTO=null;
function getNodeAt(mx,my){
  if(!_canvas) return null;
  const rect=_canvas.getBoundingClientRect();
  const cx=mx-rect.left, cy=my-rect.top;
  for(const id of Object.keys(NODE_POS)){
    const{x,y}=nodePixel(id), r=State.agentStates[id]==='active'?26:22;
    if(Math.hypot(cx-x,cy-y)<=r) return id;
  }
  return null;
}
function onCanvasHover(e){
  const id=getNodeAt(e.clientX,e.clientY);
  if(id===_popoverAgent) return;
  hidePopover(); if(!id) return;
  _popoverAgent=id; clearTimeout(_popoverTO);
  _popoverTO=setTimeout(()=>showPopover(id,e.clientX,e.clientY),120);
}
function showPopover(id,mx,my){
  const agent=AGENTS[id], status=State.agentStates[id]||'idle';
  document.getElementById('popoverName').textContent=agent.label;
  document.getElementById('popoverRole').textContent=agent.role;
  const badge=document.getElementById('popoverBadge');
  badge.textContent=status.toUpperCase(); badge.className='popover-status-badge '+status;
  const lastLog=[...State.lastLogs].reverse().find(l=>l.toLowerCase().includes(agent.label.split(' ')[0].toLowerCase()))||'';
  document.getElementById('popoverLog').textContent=lastLog?lastLog.substring(0,100)+(lastLog.length>100?'…':''):'No log output yet.';
  const pop=nodePopover; pop.style.display='block';
  const PW=280, PH=160;
  let left=mx+16, top=my-80;
  if(left+PW>window.innerWidth-8) left=mx-PW-16;
  if(top<8) top=8;
  if(top+PH>window.innerHeight-8) top=window.innerHeight-PH-8;
  pop.style.left=left+'px'; pop.style.top=top+'px'; pop.classList.add('show');
}
function hidePopover(){
  _popoverAgent=null; clearTimeout(_popoverTO);
  nodePopover.classList.remove('show'); nodePopover.style.display='none';
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

// Platform metadata
const PLATFORM_META = {
  'youtube':   { cls:'ch-youtube',   icon:'pi-youtube',   label:'YouTube' },
  'facebook':  { cls:'ch-facebook',  icon:'pi-facebook',  label:'Facebook' },
  'instagram': { cls:'ch-instagram', icon:'pi-instagram', label:'~Instagram', modelled:true },
  'tiktok':    { cls:'ch-tiktok',    icon:'pi-tiktok',    label:'TikTok' },
};
function platformMeta(s) {
  const key=(s||'').toLowerCase().replace(/[^a-z]/g,'');
  return PLATFORM_META[key]||{cls:'ch-default',icon:'pi-default',label:s||'Social'};
}

// ── Average view-through rates by platform ───────────────────────────────────
const PLATFORM_AVG_VIEW_RATE = {
  youtube:0.32, facebook:0.22, default:0.25,
};

// ── ER benchmarks: 3-month rolling by platform × industry ───────────────────
// Source: Socialinsider, Sprout Social, Rival IQ industry reports 2024-25
const INDUSTRY_ER_BENCHMARKS = {
  facebook:  {'':0.8,fmcg:0.9,food_bev:1.0,beauty:1.1,fashion:0.9,retail:0.8,tech:0.6,telco:0.5,finance:0.5,insurance:0.4,automotive:0.7,travel:1.0,health:0.8,entertainment:1.2,gaming:1.1,education:0.7,real_estate:0.5},
  youtube:   {'':2.0,fmcg:2.2,food_bev:2.5,beauty:3.0,fashion:2.5,retail:2.0,tech:1.8,telco:1.5,finance:1.5,insurance:1.2,automotive:2.0,travel:2.8,health:2.2,entertainment:3.5,gaming:3.0,education:2.0,real_estate:1.3},
  instagram: {'':1.5,fmcg:1.8,food_bev:2.2,beauty:2.5,fashion:2.0,retail:1.6,tech:1.2,telco:1.0,finance:0.9,insurance:0.8,automotive:1.3,travel:2.3,health:1.8,entertainment:2.8,gaming:2.5,education:1.5,real_estate:1.1},
};

function benchmarkFor(platform, industry) {
  const pk  = (platform||'').toLowerCase().replace(/[^a-z]/g,'');
  const ind = industry || '';
  const platMap = INDUSTRY_ER_BENCHMARKS[pk];
  if (!platMap) return 2.0;
  return platMap[ind] ?? platMap[''] ?? 2.0;
}

function renderResults(rawData) {
  const data   = normalizeReportData(rawData || {});
  const brands = data.brands || [];
  // For context/drill-down bar: create a flat comp-like list from brands
  const comp   = brands.map(b => ({ name: b.name, platform: Object.keys(b.platforms||{})[0] || 'Social Media', sentiment: b.sentiment }));

  if (!brands.length) {
    const nd = document.getElementById('noDataState');
    const params = data.scan_params || {};
    const hadQuery = params.advertisers?.length || params.advertiser || params.competitors?.length;
    nd.innerHTML = hadQuery
      ? `<div class="nd-icon">🔍</div>
         <h3>No competitor data found</h3>
         <p style="color:var(--text3);font-size:0.82rem;max-width:340px;margin:0 auto 18px;">
           The agents couldn't extract structured data for <strong>${esc((params.advertisers||[params.advertiser||'']).join(', ') || 'this brand')}</strong>
           on the selected platforms. Try a longer date range, broader platforms, or more well-known brands.
         </p>
         <button onclick="showPage('configure')" style="background:var(--accent);color:#fff;border:none;border-radius:6px;padding:8px 20px;cursor:pointer;font-size:0.8rem;">
           ← Adjust &amp; Re-run
         </button>`
      : `<div class="nd-icon">📊</div><h3>No Results Yet</h3>
         <p style="color:var(--text3);font-size:0.82rem;">Run an analysis or load a report.json to see results here.</p>`;
    nd.style.display = 'block';
    document.getElementById('resultsContent').style.display = 'none';
    return;
  }

  document.getElementById('noDataState').style.display   = 'none';
  document.getElementById('resultsContent').style.display = 'block';

  const params = data.scan_params || {};

  // Wire post-type filter toggle (H4 fix: read from brands[], not flat comp)
  const ptWrap = document.getElementById('postTypeFilterWrap');
  const hasPaidOrg = brands.some(b => b.post_type === 'paid' || b.post_type === 'organic' ||
    Object.values(b.platforms || {}).some(pd => pd.post_type === 'paid' || pd.post_type === 'organic' ||
      (pd.posts || []).some(p => p.post_classification && p.post_classification !== 'Organic')));
  if (ptWrap) ptWrap.style.display = hasPaidOrg ? 'flex' : 'none';
  document.querySelectorAll('.post-type-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.post-type-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      State.activePostTypeFilter = btn.dataset.pt;
      renderResultsFiltered();
    });
  });

  // Reset platform filter and drill-down state
  State.activePlatforms = new Set();
  State.dd = { view: 'all', brand: '', platform: 'all', postType: 'all', market: 'all', vsMyBrands: [], vsCompBrands: [] };
  State.timeGrain = 'lifetime';
  buildContextBar(params, comp);
  buildDrilldownBar(params, comp);
  // Render insight cards from report
  renderInsightCards((rawData || {}).insights || []);
  // Render TikTok suppression notice
  renderTikTokNotice(rawData, 'all');
  renderResultsFiltered();

  document.getElementById('dot-results').className = 'status-dot done';
}

function buildContextBar(params, comp) {
  const bar = document.getElementById('contextBar');

  const paramPlats = params.platforms || [];
  // Flatten all platform keys from all brands (title-case to match paramPlats)
  const data_      = normalizeReportData(State.reportData || {});
  const dataPlats  = [...new Set(
    (data_.brands || []).flatMap(b => Object.keys(b.platforms || {}).map(p => p.charAt(0).toUpperCase() + p.slice(1)))
  )];
  const allPlats   = [...new Set([...paramPlats, ...dataPlats])].filter(p =>
    ['YouTube','Facebook','Instagram','Tiktok','TikTok'].includes(p)
  );

  if (State.activePlatforms.size === 0) allPlats.forEach(p => State.activePlatforms.add(p));

  const advertisers = params.advertisers || (params.advertiser ? [params.advertiser] : []);
  const parts = [];
  if (advertisers.length) parts.push(`<strong>${esc(advertisers.join(', '))}</strong>`);
  if (params.competitors && params.competitors.length)
    parts.push('vs ' + params.competitors.map(c => `<span class="ctx-badge">${esc(c)}</span>`).join(' '));
  if (params.country)    parts.push(`<span class="ctx-sep">·</span> ${esc(params.country)}`);
  if (params.date_range) parts.push(`<span class="ctx-sep">·</span> ${esc(params.date_range)}`);
  if (params.post_type && params.post_type !== 'both')
    parts.push(`<span class="ctx-sep">·</span> <span class="post-type-badge ${params.post_type}">${esc(params.post_type)}</span>`);

  let pillsHtml = '';
  if (allPlats.length) {
    const pillList = allPlats.map(p => {
      const active = State.activePlatforms.has(p);
      const pm = platformMeta(p);
      return `<button class="ctx-plat-pill ${pm.cls} ${active?'active':''}" data-plat="${esc(p)}">${esc(p)}</button>`;
    }).join('');
    pillsHtml = `<span class="ctx-sep">·</span><span class="ctx-pills-wrap">${pillList}</span>`;
  }

  if (!parts.length && comp.length) parts.push(`${comp.length} brand(s) analysed`);
  bar.innerHTML = (parts.join(' ') || 'Scan complete') + pillsHtml;

  bar.querySelectorAll('.ctx-plat-pill').forEach(btn => {
    btn.addEventListener('click', () => {
      const plat = btn.dataset.plat;
      if (State.activePlatforms.has(plat)) {
        if (State.activePlatforms.size === 1) return;
        State.activePlatforms.delete(plat); btn.classList.remove('active');
      } else {
        State.activePlatforms.add(plat); btn.classList.add('active');
      }
      renderResultsFiltered();
    });
  });
}

function buildDrilldownBar(params, comp) {
  const bar = document.getElementById('drilldownBar');
  if (!bar) return;
  bar.style.display = 'flex';

  // ── Populate single-brand picker (non-vs views) ──────────────────────────
  const brandSel   = document.getElementById('ddBrandPick');
  const brandGroup = document.getElementById('ddBrandGroup');
  const vsPanel    = document.getElementById('ddVsPanel');

  if (brandSel) {
    const brands = [...new Set(comp.map(c => c.name).filter(Boolean))].sort();
    brandSel.innerHTML = '<option value="">All</option>' +
      brands.map(b => `<option value="${esc(b)}">${esc(b)}</option>`).join('');
    brandSel.value = '';
    brandSel.onchange = () => { State.dd.brand = brandSel.value; renderResultsFiltered(); };
  }

  // ── Populate vs multi-selects ────────────────────────────────────────────
  const myBrandNames  = params.advertisers || (params.advertiser ? [params.advertiser] : []);
  const compBrandNames = params.competitors || [];
  const myBrandsSel   = document.getElementById('ddMyBrandsPick');
  const compBrandsSel = document.getElementById('ddCompBrandsPick');

  if (myBrandsSel) {
    // Fallback: if scan_params has no advertisers, show all comp names that match _isMyBrand
    const srcMine = myBrandNames.length
      ? myBrandNames
      : [...new Set(comp.filter(c => _isMyBrand(c)).map(c => c.name).filter(Boolean))];
    myBrandsSel.innerHTML = srcMine.map(b => `<option value="${esc(b)}">${esc(b)}</option>`).join('');
    // Pre-select all
    Array.from(myBrandsSel.options).forEach(o => { o.selected = true; });
    State.dd.vsMyBrands = srcMine.slice();
  }
  if (compBrandsSel) {
    const srcComp = compBrandNames.length
      ? compBrandNames
      : [...new Set(comp.filter(c => !_isMyBrand(c)).map(c => c.name).filter(Boolean))];
    compBrandsSel.innerHTML = srcComp.map(b => `<option value="${esc(b)}">${esc(b)}</option>`).join('');
    Array.from(compBrandsSel.options).forEach(o => { o.selected = true; });
    State.dd.vsCompBrands = srcComp.slice();
  }

  // ── Execute button ───────────────────────────────────────────────────────
  const executeBtn = document.getElementById('ddVsExecute');
  if (executeBtn) {
    executeBtn.onclick = () => {
      State.dd.vsMyBrands  = Array.from(myBrandsSel.selectedOptions).map(o => o.value);
      State.dd.vsCompBrands = Array.from(compBrandsSel.selectedOptions).map(o => o.value);
      renderResultsFiltered();
    };
  }

  // ── Helper: toggle between single-brand picker and vs-panel ─────────────
  function _syncVsVisibility() {
    const isVs = State.dd.view === 'vs';
    if (vsPanel)    vsPanel.style.display    = isVs ? 'flex' : 'none';
    if (brandGroup) brandGroup.style.display = isVs ? 'none' : '';
  }
  _syncVsVisibility();

  // ── View pills ───────────────────────────────────────────────────────────
  bar.querySelectorAll('#ddView .dd-pill').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.view === State.dd.view);
    btn.onclick = () => {
      bar.querySelectorAll('#ddView .dd-pill').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      State.dd.view = btn.dataset.view;
      _syncVsVisibility();
      if (State.dd.view !== 'vs') renderResultsFiltered();
      // vs view: user picks selections and clicks Execute; don't auto-render yet
    };
  });

  // ── Platform pills ───────────────────────────────────────────────────────
  bar.querySelectorAll('#ddPlatform .dd-pill').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.plat === State.dd.platform);
    btn.onclick = () => {
      bar.querySelectorAll('#ddPlatform .dd-pill').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      State.dd.platform = btn.dataset.plat;
      renderResultsFiltered();
    };
  });

  // ── Post type pills ──────────────────────────────────────────────────────
  bar.querySelectorAll('#ddPostType .dd-pill').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.pt === State.dd.postType);
    btn.onclick = () => {
      bar.querySelectorAll('#ddPostType .dd-pill').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      State.dd.postType = btn.dataset.pt;
      renderResultsFiltered();
    };
  });

  // ── Market filter (dynamic — built from report markets) ─────────────────
  const marketWrap = document.getElementById('ddMarketWrap');
  if (marketWrap) {
    const data_     = normalizeReportData(State.reportData || {});
    const markets   = [...new Set((data_.brands || []).flatMap(b => b.markets || []))];
    if (markets.length > 1) {
      marketWrap.style.display = '';
      const marketEl = document.getElementById('ddMarketPick');
      if (marketEl) {
        marketEl.innerHTML = '<option value="all">All Markets</option>' +
          markets.map(m => `<option value="${esc(m)}"${State.dd.market===m?' selected':''}>${esc(m)}</option>`).join('');
        marketEl.onchange = () => { State.dd.market = marketEl.value; renderTikTokNotice(State.reportData, State.dd.market); renderResultsFiltered(); };
      }
    } else {
      marketWrap.style.display = 'none';
    }
  }

  // ── Reset button ─────────────────────────────────────────────────────────
  const resetBtn = document.getElementById('ddReset');
  if (resetBtn) {
    resetBtn.onclick = () => {
      State.dd = { view: 'all', brand: '', platform: 'all', postType: 'all', market: 'all', vsMyBrands: [], vsCompBrands: [] };
      State.timeGrain = 'lifetime';
      if (brandSel) brandSel.value = '';
      if (myBrandsSel)  Array.from(myBrandsSel.options).forEach(o => { o.selected = true; });
      if (compBrandsSel) Array.from(compBrandsSel.options).forEach(o => { o.selected = true; });
      bar.querySelectorAll('.dd-pill[data-view]').forEach(b => b.classList.toggle('active', b.dataset.view === 'all'));
      bar.querySelectorAll('.dd-pill[data-plat]').forEach(b => b.classList.toggle('active', b.dataset.plat === 'all'));
      bar.querySelectorAll('.dd-pill[data-pt]').forEach(b   => b.classList.toggle('active', b.dataset.pt   === 'all'));
      const grainBtns = document.querySelectorAll('.grain-btn');
      grainBtns.forEach(b => b.classList.toggle('active', b.dataset.grain === 'lifetime'));
      const marketEl = document.getElementById('ddMarketPick');
      if (marketEl) marketEl.value = 'all';
      _syncVsVisibility();
      renderTikTokNotice(State.reportData, 'all');
      renderResultsFiltered();
    };
  }
}

// Returns filtered brands[] based on current drill-down state.
// Brand-level filtering (view/brand/vs) reduces which brands appear.
// Platform filtering reduces which platform rows are shown per brand.
function filteredBrands() {
  if (!State.reportData) return [];
  const data    = normalizeReportData(State.reportData);
  const brands  = data.brands || [];
  const params  = data.scan_params || {};
  const myBrands = new Set(
    ((params.advertisers || (params.advertiser ? [params.advertiser] : [])))
      .map(b => (b||'').toLowerCase().trim()).filter(Boolean)
  );
  const dd = State.dd;

  let filtered = brands;

  // ── View dimension (brand-level) ──────────────────────────────────
  if (dd.view === 'mine') {
    filtered = filtered.filter(b => myBrands.has((b.name||'').toLowerCase().trim()));
  } else if (dd.view === 'competitors') {
    filtered = filtered.filter(b => !myBrands.has((b.name||'').toLowerCase().trim()));
  } else if (dd.view === 'vs') {
    const selMine = new Set((dd.vsMyBrands  || []).map(b => (b||'').toLowerCase().trim()).filter(Boolean));
    const selComp = new Set((dd.vsCompBrands|| []).map(b => (b||'').toLowerCase().trim()).filter(Boolean));
    if (selMine.size || selComp.size) {
      filtered = filtered.filter(b => {
        const n = (b.name||'').toLowerCase().trim();
        return selMine.has(n) || selComp.has(n);
      });
    }
  }

  // ── Specific brand pick ───────────────────────────────────────────
  if (dd.view !== 'vs' && dd.brand) {
    const pick = dd.brand.toLowerCase().trim();
    filtered = filtered.filter(b => (b.name||'').toLowerCase().trim() === pick);
  }

  // ── Market dimension — filter brands by market ────────────────────
  if (dd.market && dd.market !== 'all') {
    const mkt = dd.market.toLowerCase().trim();
    filtered = filtered.filter(b => {
      const bMkts = (b.markets || []).map(m => m.toLowerCase().trim());
      return !bMkts.length || bMkts.includes(mkt);
    });
  }

  // ── Platform dimension — filter platforms dict per brand ──────────
  if (dd.platform !== 'all') {
    const platKey = dd.platform.toLowerCase();
    filtered = filtered
      .map(b => {
        const plats = {};
        if (b.platforms && b.platforms[platKey]) plats[platKey] = b.platforms[platKey];
        return { ...b, platforms: plats };
      })
      .filter(b => Object.keys(b.platforms).length > 0);
  }

  return filtered;
}

// Whether a brand entry belongs to "my brands" (for vs-view colouring)
function _isMyBrand(comp) {
  if (!State.reportData) return false;
  const params   = State.reportData.scan_params || {};
  const myBrands = new Set(
    ((params.advertisers || (params.advertiser ? [params.advertiser] : [])))
      .map(b => (b||'').toLowerCase().trim()).filter(Boolean)
  );
  return myBrands.has((comp.name||'').toLowerCase().trim());
}

// ── Report data setter — always use this; invalidates normalized cache ────────
function _setReportData(data) {
  State.reportData     = data;
  State.normalizedReport = null;
}

// Backward-compat adapter: converts old competitors[] schema to new brands[].
// Returns a new object — never mutates the input.
function normalizeReportData(data) {
  // Return cached result when called repeatedly for the same reportData
  if (data && data === State.reportData && State.normalizedReport) return State.normalizedReport;
  if (!data) return { brands: [] };
  if (data.brands && data.brands.length) {
    if (data === State.reportData) State.normalizedReport = data;
    return data;
  }
  if (!data.competitors || !data.competitors.length) return data;
  const byBrand = {};
  data.competitors.forEach(c => {
    const name = c.name || 'Unknown';
    if (!byBrand[name]) byBrand[name] = {
      name,
      platforms: {},
      composite_sov: c.sov_pct || 0,
      composite_confidence: 'Low',
      content_themes: c.content_themes || [],
      hashtags: c.hashtags || [],
      top_posts: c.top_posts || [],
      sentiment: c.sentiment || 'Neutral',
    };
    const plat = (c.platform || 'facebook').toLowerCase().split('/')[0].trim();
    byBrand[name].platforms[plat] = {
      sov_index: c.sov_pct || 0,
      sov_label: `${c.sov_pct || 0} (Directional / Indexed – Not Actual Spend)`,
      confidence: 'Low',
      consistency_flag: false,
      signals: {
        creative_volume_share: 0,
        creative_velocity_score: 0,
        longevity_score: 0,
        geo_presence_score: 0,
        reach_bucket_score: 0,
        engagement_corroboration: (c.metrics||{}).er_pct || c.engagement_rate || 0,
      },
    };
  });
  const result = { ...data, brands: Object.values(byBrand) };
  if (data === State.reportData) State.normalizedReport = result;
  return result;
}

function renderResultsFiltered() {
  const rawData = State.reportData;
  if (!rawData) return;
  const data   = normalizeReportData(rawData);
  const brands = filteredBrands();
  const bgColors = brands.map((_, i) => BRAND_COLORS[i % BRAND_COLORS.length]);

  // ── KPIs ────────────────────────────────────────────────────────────────
  const brandCount = brands.length;

  // Top SOV brand by composite_sov
  const topSovEntry = brands.length
    ? brands.reduce((best, b) => (b.composite_sov||0) > (best.composite_sov||0) ? b : best, brands[0])
    : null;

  // Per-platform leader (highest sov_index on that platform) — use ACTIVE_PLATFORMS
  const platLeader = {};
  ACTIVE_PLATFORMS.forEach(p => {
    let best = null, bestVal = -1;
    brands.forEach(b => {
      const v = (b.platforms||{})[p]?.sov_index || 0;
      if (v > bestVal) { bestVal = v; best = b.name; }
    });
    if (best && bestVal > 0) platLeader[p] = best;
  });
  const platLeaderStr = Object.entries(platLeader)
    .map(([p,n]) => `${p.charAt(0).toUpperCase()+p.slice(1)}: ${n}`)
    .join(' · ') || '—';

  // Avg confidence
  const confOrder = { High: 3, Medium: 2, Low: 1 };
  const avgConfScore = brands.length
    ? brands.reduce((s,b) => s + (confOrder[b.composite_confidence] || 1), 0) / brands.length
    : 1;
  const avgConfLabel = avgConfScore >= 2.5 ? 'High' : avgConfScore >= 1.5 ? 'Medium' : 'Low';

  // % Paid Posts across all brands
  let totalPosts = 0, totalPaid = 0;
  brands.forEach(b => {
    (b.top_posts || []).forEach(p => {
      totalPosts++;
      const cls = (typeof p === 'object' ? (p.post_classification || '') : '');
      if (cls.startsWith('Paid')) totalPaid++;
    });
  });
  const paidPct = totalPosts > 0 ? Math.round((totalPaid / totalPosts) * 100) : null;

  const kpiBrandCount  = document.getElementById('kpiBrandCount');
  const kpiTopSov      = document.getElementById('kpiTopSov');
  const kpiPlatLeader  = document.getElementById('kpiPlatformLeader');
  const kpiConfidence  = document.getElementById('kpiConfidence');
  const kpiBrandSub    = document.getElementById('kpiBrandSub');
  const kpiPaidPct     = document.getElementById('kpiPaidPct');
  if (kpiBrandCount) kpiBrandCount.textContent = brandCount;
  if (kpiBrandSub)   kpiBrandSub.textContent   = `across ${Object.keys(platLeader).length || '—'} platform(s)`;
  if (kpiTopSov)     kpiTopSov.textContent = topSovEntry ? `${esc(topSovEntry.name)} · ${(topSovEntry.composite_sov||0).toFixed(1)} (Dir.)` : '—';
  if (kpiPlatLeader) kpiPlatLeader.textContent = platLeaderStr;
  if (kpiConfidence) kpiConfidence.textContent = avgConfLabel;
  if (kpiPaidPct)    kpiPaidPct.textContent    = paidPct !== null ? `${paidPct}% of tracked posts` : 'No post data';

  // Table subtitle
  const params = data.scan_params || {};
  const tSub = document.getElementById('tableSubtitle');
  if (tSub) tSub.textContent = `Directional / Indexed – Not Actual Spend · Share of voice within selected competitor group only`;

  // ── Charts ───────────────────────────────────────────────────────────────
  const brandNames = brands.map(b => b.name || '?');

  // Determine which platforms are active (respect TikTok suppression)
  const reportMeta   = (State.reportData || {});
  const tiktokSuppressed = _isTikTokSuppressed(reportMeta, State.dd.market);
  const activePlats  = ACTIVE_PLATFORMS.filter(p => !tiktokSuppressed || p !== 'tiktok');
  const platColors   = { facebook: C.accent, instagram: C.purple, youtube: C.red, tiktok: '#e879f9' };
  const platLabels   = { facebook: 'Facebook SOV (Dir.)', instagram: 'Instagram (modelled) SOV (Dir.)', youtube: 'YouTube SOV (Dir.)', tiktok: 'TikTok SOV (Dir.)' };

  // Updated signal weights (new prompt: Vol 30 / Vel 10 / Long 15 / PlatPres 15 / Reach 15 / Eng 15)
  const sigKeys = [
    { key: 'creative_volume_share',    label: 'Creative Vol (30%)',    color: C.accent },
    { key: 'creative_velocity_score',  label: 'Velocity (10%)',        color: C.cyan },
    { key: 'longevity_score',          label: 'Longevity (15%)',       color: C.green },
    { key: 'geo_presence_score',       label: 'Platform Presence (15%)', color: C.amber },
    { key: 'reach_bucket_score',       label: 'Reach (15%)',           color: C.purple },
    { key: 'engagement_corroboration', label: 'Engagement (15%)',      color: C.red },
  ];

  function _avgSig(brand, sigKey) {
    const plats = Object.values(brand.platforms || {});
    if (!plats.length) return 0;
    const vals = plats.map(p => (p.signals||{})[sigKey] || 0);
    return vals.reduce((s,v)=>s+v,0) / vals.length;
  }

  // Confidence counts keyed by active platforms only
  const confCounts = {};
  activePlats.forEach(p => { confCounts[p] = {High:0, Medium:0, Low:0}; });
  brands.forEach(b => {
    activePlats.forEach(p => {
      const conf = (b.platforms||{})[p]?.confidence || 'Low';
      confCounts[p][conf]++;
    });
  });

  // Chart 1 — Composite SOV per brand (update-in-place; create only if needed)
  if (Charts.sovComposite && Charts.sovComposite.data.labels.length === brandNames.length) {
    Charts.sovComposite.data.labels = brandNames;
    Charts.sovComposite.data.datasets[0].data = brands.map(b => b.composite_sov || 0);
    Charts.sovComposite.data.datasets[0].backgroundColor = bgColors;
    Charts.sovComposite.update();
  } else {
    if (Charts.sovComposite) { Charts.sovComposite.destroy(); Charts.sovComposite = null; }
    Charts.sovComposite = new Chart(document.getElementById('sovCompositeChart').getContext('2d'), {
      type: 'bar',
      data: {
        labels: brandNames,
        datasets: [{
          label: 'Composite SOV (Directional)',
          data: brands.map(b => b.composite_sov || 0),
          backgroundColor: bgColors,
          borderRadius: 5,
          borderSkipped: false,
        }],
      },
      options: {
        ...CHART_OPTS,
        plugins: {
          legend: { display: false },
          tooltip: { callbacks: { label: ctx => `SOV Index: ${ctx.parsed.y.toFixed(1)} (Directional / Indexed – Not Actual Spend)` } },
        },
        scales: { y: { ...CHART_OPTS.scales?.y, title: { display: true, text: 'SOV Index (Directional)', color: '#8b949e', font: { size: 10 } } } },
      },
    });
  }

  // Chart 2 — Per-platform SOV grouped bar (active platforms only)
  const platDatasets = activePlats.map(p => ({
    label: platLabels[p] || (p + ' SOV (Dir.)'),
    data: brands.map(b => (b.platforms||{})[p]?.sov_index || 0),
    backgroundColor: platColors[p] || C.accent,
    borderRadius: 4, borderSkipped: false,
  }));
  if (Charts.platformSov) { Charts.platformSov.destroy(); Charts.platformSov = null; }
  Charts.platformSov = new Chart(document.getElementById('platformSovChart').getContext('2d'), {
    type: 'bar',
    data: { labels: brandNames, datasets: platDatasets },
    options: {
      ...CHART_OPTS,
      plugins: {
        legend: { display: true, position: 'top', labels: { color: '#8b949e', font: { size: 10 } } },
        tooltip: { callbacks: { label: ctx => `${ctx.dataset.label}: ${ctx.parsed.y.toFixed(1)} (Directional)` } },
      },
    },
  });

  // Chart 3 — 6-signal stacked bar per brand (update-in-place when label count matches)
  if (Charts.signalBreakdown && Charts.signalBreakdown.data.labels.length === brandNames.length) {
    Charts.signalBreakdown.data.labels = brandNames;
    Charts.signalBreakdown.data.datasets.forEach((ds, i) => {
      const sk = sigKeys[i];
      if (sk) { ds.data = brands.map(b => _avgSig(b, sk.key)); ds.label = sk.label; }
    });
    Charts.signalBreakdown.update();
  } else {
    if (Charts.signalBreakdown) { Charts.signalBreakdown.destroy(); Charts.signalBreakdown = null; }
    Charts.signalBreakdown = new Chart(document.getElementById('signalBreakdownChart').getContext('2d'), {
      type: 'bar',
      data: {
        labels: brandNames,
        datasets: sigKeys.map(s => ({
          label: s.label,
          data: brands.map(b => _avgSig(b, s.key)),
          backgroundColor: s.color,
          borderRadius: 3,
          borderSkipped: false,
        })),
      },
      options: {
        ...CHART_OPTS,
        plugins: {
          legend: { display: true, position: 'top', labels: { color: '#8b949e', font: { size: 9 } } },
          tooltip: { callbacks: { label: ctx => `${ctx.dataset.label}: ${ctx.parsed.y.toFixed(1)} (Directional – Not Actual Spend)` } },
        },
        scales: {
          x: { stacked: true, grid: { color: C.chartGrid }, ticks: { color: C.chartTick, font: { size: 11 } } },
          y: { stacked: true, grid: { color: C.chartGrid }, ticks: { color: C.chartTick, font: { size: 11 } } },
        },
      },
    });
  }

  // Chart 4 — Confidence distribution per active platform
  const confPlatLabels = activePlats.map(p => p.charAt(0).toUpperCase() + p.slice(1));
  if (Charts.confidence && Charts.confidence.data.labels.join() === confPlatLabels.join()) {
    Charts.confidence.data.datasets[0].data = activePlats.map(p => confCounts[p].High);
    Charts.confidence.data.datasets[1].data = activePlats.map(p => confCounts[p].Medium);
    Charts.confidence.data.datasets[2].data = activePlats.map(p => confCounts[p].Low);
    Charts.confidence.update();
  } else {
    if (Charts.confidence) { Charts.confidence.destroy(); Charts.confidence = null; }
    Charts.confidence = new Chart(document.getElementById('confidenceChart').getContext('2d'), {
      type: 'bar',
      data: {
        labels: confPlatLabels,
        datasets: [
          { label: 'High',   data: activePlats.map(p => confCounts[p].High),   backgroundColor: C.green,  borderRadius: 4, borderSkipped: false },
          { label: 'Medium', data: activePlats.map(p => confCounts[p].Medium), backgroundColor: C.amber,  borderRadius: 4, borderSkipped: false },
          { label: 'Low',    data: activePlats.map(p => confCounts[p].Low),    backgroundColor: C.red,    borderRadius: 4, borderSkipped: false },
        ],
      },
      options: {
        ...CHART_OPTS,
        plugins: {
          legend: { display: true, position: 'top', labels: { color: '#8b949e', font: { size: 10 } } },
          tooltip: { callbacks: { label: ctx => `${ctx.dataset.label}: ${ctx.parsed.y} brand${ctx.parsed.y!==1?'s':''}` } },
        },
      },
    });
  }

  // Chart 5 — SOV Trend line (shown only when grain ≠ lifetime)
  _renderSovTrendChart(brands, bgColors);

  // ── Content Intel ─────────────────────────────────────────────────────────
  // Pass full brands[] — renderContentIntel now handles multi-platform data (M1 fix)
  renderContentIntel(brands, bgColors);

  // ── Table (one row per brand × platform) ──────────────────────────────────
  const confBadgeColor = { High: 'var(--green)', Medium: 'var(--amber)', Low: 'var(--red)' };
  const tbody = document.getElementById('resultsTableBody');
  const rows = [];
  brands.forEach((b, bi) => {
    const color = bgColors[bi % bgColors.length];
    const sentClass = (b.sentiment||'').toLowerCase();
    // Use ACTIVE_PLATFORMS; skip tiktok if suppressed
    ACTIVE_PLATFORMS.filter(p => !tiktokSuppressed || p !== 'tiktok').forEach(p => {
      const pd = (b.platforms||{})[p];
      if (!pd) return;
      const sigs  = pd.signals || {};
      const conf  = pd.confidence || 'Low';
      const confColor = confBadgeColor[conf] || 'var(--text3)';
      const flagBadge = pd.consistency_flag
        ? '<span style="font-size:0.65rem;color:var(--amber);margin-left:4px;" title="Cross-signal consistency flag">⚠</span>'
        : '';
      const isModelled  = p === 'instagram';
      const platLabel   = p.charAt(0).toUpperCase() + p.slice(1) + (isModelled ? ' (est.)' : '');
      const platBadgeCls = isModelled ? 'platform-badge modelled' : 'platform-badge';
      // Post classification summary
      const posts = pd.posts || [];
      const paidConf  = posts.filter(pp => (pp.post_classification||'').startsWith('Paid (Confirmed)')).length;
      const paidEst   = posts.filter(pp => (pp.post_classification||'') === 'Paid (Est.)').length;
      const organic   = posts.filter(pp => (pp.post_classification||'') === 'Organic').length;
      const classStr  = posts.length
        ? `<span class="post-class-badge confirmed" title="Paid Confirmed">${paidConf}✓</span> <span class="post-class-badge estimated" title="Paid Est.">~${paidEst}</span> <span class="post-class-badge organic" title="Organic">${organic}○</span>`
        : '<span style="color:var(--text3);font-size:0.72rem;">—</span>';
      rows.push(`<tr>
        <td><span style="display:inline-flex;align-items:center;gap:7px;">
          <span style="width:8px;height:8px;border-radius:50%;background:${color};display:inline-block;flex-shrink:0;"></span>
          <strong>${esc(b.name||'—')}</strong>
        </span></td>
        <td><span class="${platBadgeCls}"${isModelled?' title="Instagram data modelled from associated Facebook Page"':''}>${esc(platLabel)}</span></td>
        <td style="font-weight:700;color:var(--accent);" title="${esc(pd.sov_label||'')}">
          ${(pd.sov_index||0).toFixed(1)}
        </td>
        <td><span style="color:${confColor};font-size:0.78rem;font-weight:600;">${esc(conf)}</span>${flagBadge}</td>
        <td>${classStr}</td>
        <td>${(sigs.creative_volume_share||0).toFixed(1)}</td>
        <td>${(sigs.creative_velocity_score||0).toFixed(1)}</td>
        <td>${(sigs.reach_bucket_score||0).toFixed(1)}</td>
        <td>${(sigs.geo_presence_score||0).toFixed(1)}</td>
        <td>${(sigs.longevity_score||0).toFixed(1)}</td>
        <td>${(sigs.engagement_corroboration||0).toFixed(1)}</td>
        <td><span class="sentiment-badge ${sentClass}">${esc(b.sentiment||'—')}</span></td>
      </tr>`);
    });
  });
  tbody.innerHTML = rows.join('');

  // Refresh TikTok notice for current market selection
  renderTikTokNotice(State.reportData, State.dd.market);

  // Top posts — pass all platforms (M1 fix: not just first platform)
  renderTopPosts({ competitors: brands.map(b => ({
    name: b.name,
    handle: b.handle,
    top_posts: b.top_posts || [],
    platform: Object.keys(b.platforms || {})[0] || 'Social Media',
  })) });
}

function _buildKeywordListHtml(kws) {
  if (!kws || !kws.length) return '<span style="color:var(--text3);font-size:0.75rem;">No keyword data</span>';
  return kws.slice(0,10).map((w,i) =>
    `<span class="keyword-badge" style="opacity:${1 - i*0.07}">${esc(w)}</span>`
  ).join('');
}

function renderContentIntel(brands, bgColors) {
  const container = document.getElementById('contentIntelBody');
  if (!container) return;

  const hasContent = brands.some(b =>
    (b.top_posts && b.top_posts.length) ||
    (b.hashtags && b.hashtags.length) ||
    (b.content_themes && b.content_themes.length) ||
    (b.paid_campaigns && b.paid_campaigns.length) ||
    b.keywords_by_type
  );
  if (!hasContent) {
    container.innerHTML = '<div style="padding:20px 24px;color:var(--text3);font-size:0.82rem;">No post content data available — run a deeper analysis to populate this section.</div>';
    return;
  }

  const grid = document.createElement('div');
  grid.className = 'content-intel-grid';

  brands.forEach((b, i) => {
    const color    = bgColors[i % bgColors.length];
    const posts    = b.top_posts      || [];
    const tags     = b.hashtags       || [];
    const themes   = b.content_themes || [];
    const campaigns = b.paid_campaigns || [];
    const pt       = b.post_type || 'both';
    const kwByType = b.keywords_by_type || {};

    // Derive first platform label
    const firstPlat   = Object.keys(b.platforms || {})[0] || 'Multi';
    const isModelled  = firstPlat === 'instagram';
    const platDisplay = firstPlat.charAt(0).toUpperCase() + firstPlat.slice(1) + (isModelled ? ' (est.)' : '');

    // Content-type counts (brand_say / sma / others_say)
    const ctCounts = { brand_say: 0, sma: 0, others_say: 0 };
    posts.forEach(p => {
      const ct = typeof p === 'object' ? (p.content_type || 'brand_say') : 'brand_say';
      ctCounts[ct] = (ctCounts[ct] || 0) + 1;
    });

    const card = document.createElement('div');
    card.className = 'content-intel-card fade-in';
    const cardId = `ci-card-${i}`;
    card.id = cardId;

    let html = `
      <div class="ci-brand-row">
        <span class="ci-color-dot" style="background:${color}"></span>
        <span class="ci-brand-name">${esc(b.name||'—')}</span>
        ${b.handle ? `<span class="ci-handle">${esc(b.handle)}</span>` : ''}
        <span class="platform-badge${isModelled?' modelled':''}" style="margin-left:auto;"
          ${isModelled?'title="Instagram data modelled from associated Facebook Page"':''}>${esc(platDisplay)}</span>
        <span class="post-type-badge ${pt}" style="margin-left:6px;">${esc(pt)}</span>
      </div>

      <!-- Content-type tabs: Brand Say / SMA / Others Say -->
      <div class="ct-tabs" role="tablist">
        <button class="ct-tab active" data-ct="brand_say" onclick="_switchCtTab(this,'${cardId}')">
          Brand Say <span class="ct-count">${ctCounts.brand_say}</span>
        </button>
        <button class="ct-tab" data-ct="sma" onclick="_switchCtTab(this,'${cardId}')">
          SMA <span class="ct-count">${ctCounts.sma}</span>
        </button>
        <button class="ct-tab" data-ct="others_say" onclick="_switchCtTab(this,'${cardId}')">
          Others Say <span class="ct-count">${ctCounts.others_say}</span>
        </button>
      </div>`;

    // Render each content-type panel
    ['brand_say','sma','others_say'].forEach(ct => {
      const ctPosts  = posts.filter(p => (typeof p === 'object' ? (p.content_type || 'brand_say') : 'brand_say') === ct);
      const ctLabel  = { brand_say: 'Brand Voice', sma: 'Collaborations (SMA)', others_say: 'Others Say' }[ct];
      const kws      = kwByType[ct] || [];
      const realUrls = ctPosts.filter(p => typeof p === 'object' && p.url && p.url !== 'null' && p.url !== '');

      html += `<div class="ct-panel${ct==='brand_say'?' active':''}" data-ct-panel="${ct}">`;

      if (ct === 'others_say' && !realUrls.length) {
        html += `<div style="color:var(--text3);font-size:0.77rem;padding:8px 0;">
          Source: Ad Library 3rd-party sponsored posts only.
          ${ctPosts.length === 0 ? 'No third-party ad library posts detected for this brand.' : ''}
        </div>`;
      }

      if (realUrls.length) {
        html += `<div class="ci-section-label">${esc(ctLabel)} Posts</div><ul class="ci-posts">`;
        realUrls.slice(0,5).forEach(p => {
          const url      = p.url;
          const cls      = p.post_classification || '';
          const clsCss   = cls === 'Paid (Confirmed)' ? 'confirmed' : cls === 'Paid (Est.)' ? 'estimated' : 'organic';
          const clsLabel = cls || 'Organic';
          const likes    = p.likes  ? `${fmtShort(p.likes)} likes` : '';
          const views    = p.views  ? `${fmtShort(p.views)} views` : '';
          const statStr  = [likes, views].filter(Boolean).join(' · ');
          let domain = ''; try { domain = new URL(url).hostname.replace('www.',''); } catch {}
          html += `<li class="ci-post-item">
            <span class="post-class-badge ${clsCss}" title="${esc(clsLabel)}" style="font-size:0.62rem;">${esc(clsLabel.replace('Paid (','').replace(')','') || 'Org')}</span>
            <a href="${esc(url)}" target="_blank" rel="noopener noreferrer" class="ci-post-link">${esc(domain||url)}</a>
            ${statStr ? `<span style="color:var(--text3);font-size:0.7rem;margin-left:4px;">${esc(statStr)}</span>` : ''}
          </li>`;
        });
        html += `</ul>`;
      }

      if (kws.length) {
        html += `<div class="ci-section-label">Top Keywords</div><div class="ci-keywords">${_buildKeywordListHtml(kws)}</div>`;
      }

      html += `</div>`; // end ct-panel
    });

    if (campaigns.length) {
      html += `<div class="ci-section-label">Paid Campaigns</div><ul class="ci-posts">`;
      campaigns.forEach(camp => { html += `<li class="ci-post-item"><span class="post-class-badge confirmed" style="font-size:0.62rem;">paid</span>${esc(camp)}</li>`; });
      html += `</ul>`;
    }

    if (tags.length) {
      html += `<div class="ci-section-label">Hashtags</div><div class="ci-tags">`;
      tags.forEach(t => { const tag=t.startsWith('#')?t:'#'+t; html+=`<span class="hashtag-badge">${esc(tag)}</span>`; });
      html += `</div>`;
    }
    if (themes.length) {
      html += `<div class="ci-section-label">Content Themes</div><div class="ci-tags">`;
      themes.forEach(th => { html+=`<span class="theme-badge">${esc(th)}</span>`; });
      html += `</div>`;
    }

    card.innerHTML = html;
    grid.appendChild(card);
  });

  container.innerHTML = '';
  container.appendChild(grid);
}

// ── Top Posts embeds ─────────────────────────────────────────────────────────

function _ytVideoId(url) {
  try {
    const u=new URL(url);
    if(u.hostname.includes('youtu.be')) return u.pathname.slice(1).split('?')[0];
    return u.searchParams.get('v')||null;
  } catch{return null;}
}
function buildPostEmbed(url,caption,platform,idx){
  const plat=(platform||'').toLowerCase();
  const rankBadge=`<span class="post-tile-rank">#${idx+1}</span>`;
  if(plat.includes('youtube')||url.includes('youtu')){
    const vid=_ytVideoId(url);
    if(vid) return `<div class="post-tile-embed">${rankBadge}<iframe src="https://www.youtube.com/embed/${esc(vid)}" frameborder="0" allow="accelerometer;autoplay;clipboard-write;encrypted-media;gyroscope;picture-in-picture" allowfullscreen loading="lazy"></iframe></div><div class="post-tile-caption">${esc(caption)}</div>`;
  }
  return buildPostLinkCard(url,caption,idx);
}
function buildPostNoUrl(caption,idx){
  return `<div class="post-tile-nourl"><span class="post-tile-rank">#${idx+1}</span><div class="post-tile-caption">${esc(caption)}</div></div>`;
}
function buildPostLinkCard(url,caption,idx){
  let domain=''; try{domain=new URL(url).hostname.replace('www.','');}catch{}
  return `<div class="post-tile-linkcard"><span class="post-tile-rank">#${idx+1}</span><div class="post-tile-caption">${esc(caption)}</div><a class="post-tile-cta" href="${esc(url)}" target="_blank" rel="noopener noreferrer">View Post ↗<span class="post-tile-domain">${esc(domain)}</span></a></div>`;
}
function renderTopPosts(data){
  const section=document.getElementById('topPostsSection'), grid=document.getElementById('topPostsGrid');
  if(!section||!grid) return;
  const comp=data.competitors||[];
  const hasAny=comp.some(c=>c.top_posts&&c.top_posts.length);
  if(!hasAny){section.style.display='none';return;}
  grid.className='top-posts-grid'; grid.innerHTML='';
  comp.forEach((c,ci)=>{
    const posts=c.top_posts||[]; if(!posts.length) return;
    const plat=c.platform||'Social Media', pm=platformMeta(plat);
    const brandColor=BRAND_COLORS[ci%BRAND_COLORS.length];
    const header=document.createElement('div'); header.className='tposts-brand-header';
    header.innerHTML=`<span class="channel-platform-icon ${pm.icon}" style="font-size:1rem;margin-right:6px;"></span><span class="dot" style="background:${brandColor}"></span><span class="tposts-brand-name">${esc(c.name||'—')}</span>${c.handle?`<span class="tposts-handle">${esc(c.handle)}</span>`:''}<span class="tposts-plat-label ${pm.cls}">${esc(pm.label)}</span>`;
    grid.appendChild(header);
    const row=document.createElement('div'); row.className='tposts-row';
    posts.slice(0,10).forEach((post,idx)=>{
      const caption=typeof post==='object'?(post.caption||''):String(post);
      const postUrl=typeof post==='object'?(post.url||null):null;
      const embedHtml=postUrl?buildPostEmbed(postUrl,caption,plat,idx):buildPostNoUrl(caption,idx);
      const tile=document.createElement('div'); tile.className='post-tile'; tile.innerHTML=embedHtml;
      row.appendChild(tile);
    });
    grid.appendChild(row);
  });
  section.style.display=grid.children.length?'block':'none';
}


// ═══════════════════════════════════════════════════════════════════════════════
// SAVED RUNS
// ═══════════════════════════════════════════════════════════════════════════════

// ── Server-backed saved runs ──────────────────────────────────────────────────
// Runs persist in data/saved_runs.json on the server — survive browser clears.

let _savedRunsCache = [];   // in-memory cache, refreshed on page load + after mutations

async function _fetchSavedRuns() {
  try {
    const d = await fetch('/saved-runs').then(r => r.json());
    _savedRunsCache = d.runs || [];
  } catch(e) { _savedRunsCache = []; }
}

async function saveRun(report) {
  if (!report || (!report.brands && !report.competitors)) return;
  const sp    = report.scan_params || {};
  const entry = {
    id:     Date.now(),
    ts:     new Date().toISOString(),
    label:  buildRunLabel(sp),
    params: sp,
    report: report,
  };
  try {
    await fetch('/saved-runs', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(entry),
    });
  } catch(e) {}
  await _fetchSavedRuns();
  renderSavedRunsList();
  const dot = document.getElementById('dot-saved');
  if (dot) { dot.className = 'status-dot done'; setTimeout(() => dot.className = 'status-dot', 3000); }
}

async function deleteRun(id) {
  try { await fetch(`/saved-runs/${id}`, { method: 'DELETE' }); } catch(e) {}
  await _fetchSavedRuns();
  renderSavedRunsList();
}

function buildRunLabel(sp) {
  const brands = [...(sp.advertisers||[]), ...(sp.competitors||[])].slice(0,3).join(', ') || 'Unknown brands';
  const market = sp.country || 'Global';
  const dr     = sp.date_range || '';
  return `${brands} · ${market} · ${dr}`;
}

function buildRunCsv(report) {
  const norm  = normalizeReportData(report);
  const sp    = norm.scan_params || {};
  const brands = norm.brands || [];

  const metaRows = [
    ['Run Date', new Date().toLocaleString()],
    ['Brands', [...(sp.advertisers||[]), ...(sp.competitors||[])].join(', ')],
    ['Market', sp.country || 'Global'],
    ['Industry', sp.industry || 'General'],
    ['Date Range', sp.date_range || ''],
    ['Platforms', (sp.platforms||[]).join(', ')],
    ['Analysis Depth', sp.depth || ''],
    [],
  ].map(r => r.map(v => `"${String(v||'').replace(/"/g,'""')}"`).join(','));

  const cols = [
    'brand','platform','sov_index','confidence','consistency_flag',
    'post_classification_confirmed','post_classification_estimated','post_classification_organic',
    'creative_volume','creative_velocity','reach_tier','platform_presence',
    'ad_longevity','engagement_corroboration',
    'composite_sov','composite_confidence','sentiment',
    'content_type_brand_say','content_type_sma','content_type_others_say',
    'tiktok_suppressed',
  ];

  const dataRows = [];
  brands.forEach(b => {
    ACTIVE_PLATFORMS.forEach(p => {
      const pd   = (b.platforms||{})[p];
      if (!pd) return;
      const sigs  = pd.signals || {};
      const posts = pd.posts || [];
      const paidConf = posts.filter(pp => (pp.post_classification||'') === 'Paid (Confirmed)').length;
      const paidEst  = posts.filter(pp => (pp.post_classification||'') === 'Paid (Est.)').length;
      const organic  = posts.filter(pp => (pp.post_classification||'') === 'Organic').length;
      const allPosts = b.top_posts || [];
      const bsCnt   = allPosts.filter(pp => (typeof pp==='object'?pp.content_type:'') === 'brand_say').length;
      const smaCnt  = allPosts.filter(pp => (typeof pp==='object'?pp.content_type:'') === 'sma').length;
      const otherCnt= allPosts.filter(pp => (typeof pp==='object'?pp.content_type:'') === 'others_say').length;
      dataRows.push([
        b.name, p, pd.sov_index, pd.confidence, pd.consistency_flag,
        paidConf, paidEst, organic,
        sigs.creative_volume_share, sigs.creative_velocity_score,
        sigs.reach_bucket_score, sigs.geo_presence_score,
        sigs.longevity_score, sigs.engagement_corroboration,
        b.composite_sov, b.composite_confidence, b.sentiment,
        bsCnt, smaCnt, otherCnt,
        _isTikTokSuppressed(norm, 'all'),
      ].map(v => `"${String(v==null?'':v).replace(/"/g,'""')}"`).join(','));
    });
  });

  return [...metaRows, cols.join(','), ...dataRows].join('\n');
}

function findRun(id) {
  return _savedRunsCache.find(r => r.id === id);
}

function downloadRunCsv(entry) {
  const csv  = buildRunCsv(entry.report);
  const blob = new Blob([csv], { type: 'text/csv' });
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement('a');
  const ts   = new Date(entry.ts).toISOString().slice(0,16).replace('T','_').replace(':','-');
  a.href = url; a.download = `hermes-run-${ts}.csv`;
  document.body.appendChild(a); a.click();
  document.body.removeChild(a); URL.revokeObjectURL(url);
}

function renderSavedRunsList() {
  const runs     = _savedRunsCache;
  const list     = document.getElementById('savedRunsList');
  const empty    = document.getElementById('savedRunsEmpty');
  if (!list) return;

  if (!runs.length) {
    if (empty) empty.style.display = 'flex';
    list.innerHTML = '';
    return;
  }
  if (empty) empty.style.display = 'none';

  list.innerHTML = runs.map(entry => {
    const sp        = entry.params || {};
    const norm      = normalizeReportData(entry.report || {});
    const repBrands = norm.brands || [];
    const topSov    = repBrands.length
      ? repBrands.reduce((best,b) => (b.composite_sov||0) > (best.composite_sov||0) ? b : best, repBrands[0])
      : null;
    const topSovStr = topSov ? `${topSov.name} ${(topSov.composite_sov||0).toFixed(1)} (Dir.)` : '—';
    const brandList = [...(sp.advertisers||[]), ...(sp.competitors||[])];
    const tsLabel   = new Date(entry.ts).toLocaleString('en-SG', { dateStyle:'medium', timeStyle:'short' });

    return `
    <div class="saved-run-card" data-id="${entry.id}">
      <div class="src-top">
        <div class="src-meta">
          <div class="src-label">${esc(entry.label)}</div>
          <div class="src-ts">${tsLabel}</div>
        </div>
        <div class="src-actions">
          <button class="btn-secondary src-load-btn" data-id="${entry.id}" title="Load into Results">↗ Load</button>
          <button class="btn-secondary src-csv-btn"  data-id="${entry.id}" title="Download CSV">↓ CSV</button>
          <button class="btn-ghost    src-del-btn"   data-id="${entry.id}" title="Delete">✕</button>
        </div>
      </div>
      <div class="src-pills">
        ${brandList.slice(0,6).map(b => `<span class="src-pill">${esc(b)}</span>`).join('')}
        ${brandList.length > 6 ? `<span class="src-pill muted">+${brandList.length-6} more</span>` : ''}
      </div>
      <div class="src-stats">
        <div class="src-stat"><span class="src-stat-label">Brands</span><span class="src-stat-val">${repBrands.length}</span></div>
        <div class="src-stat"><span class="src-stat-label">Top SOV</span><span class="src-stat-val">${esc(topSovStr)}</span></div>
        <div class="src-stat"><span class="src-stat-label">Market</span><span class="src-stat-val">${esc(sp.country||'Global')}</span></div>
        <div class="src-stat"><span class="src-stat-label">Platforms</span><span class="src-stat-val">${(sp.platforms||[]).join(', ')||'—'}</span></div>
        <div class="src-stat"><span class="src-stat-label">Range</span><span class="src-stat-val">${esc(sp.date_range||'—')}</span></div>
      </div>
    </div>`;
  }).join('');

  // Wire up buttons
  list.querySelectorAll('.src-load-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const entry = findRun(parseInt(btn.dataset.id));
      if (!entry) return;
      State.reportData = entry.report;
      showPage('results');
    });
  });
  list.querySelectorAll('.src-csv-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      const entry = findRun(parseInt(btn.dataset.id));
      if (entry) downloadRunCsv(entry);
    });
  });
  list.querySelectorAll('.src-del-btn').forEach(btn => {
    btn.addEventListener('click', () => deleteRun(parseInt(btn.dataset.id)));
  });
}

document.getElementById('clearSavedBtn').addEventListener('click', async () => {
  if (confirm('Delete all saved runs? This cannot be undone.')) {
    try { await fetch('/saved-runs', { method: 'DELETE' }); } catch(e) {}
    await _fetchSavedRuns();
    renderSavedRunsList();
  }
});

// Load saved runs on init from server
(async () => { await _fetchSavedRuns(); renderSavedRunsList(); })();

// ═══════════════════════════════════════════════════════════════════════════════
// EXPORT
// ═══════════════════════════════════════════════════════════════════════════════

document.getElementById('exportJson').addEventListener('click', () => {
  if (!State.reportData) return;
  const blob = new Blob([JSON.stringify(State.reportData,null,2)], {type:'application/json'});
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement('a');
  a.href=url; a.download='hermes-report.json';
  document.body.appendChild(a); a.click(); document.body.removeChild(a); URL.revokeObjectURL(url);
});

document.getElementById('exportCsv').addEventListener('click', () => {
  if (!State.reportData) return;
  const csv  = buildRunCsv(State.reportData);
  const blob = new Blob([csv],{type:'text/csv'});
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement('a');
  a.href=url; a.download='hermes-report.csv';
  document.body.appendChild(a); a.click(); document.body.removeChild(a); URL.revokeObjectURL(url);
});

// ═══════════════════════════════════════════════════════════════════════════════
// HELPERS
// ═══════════════════════════════════════════════════════════════════════════════

function fmt(n)     { return n!=null ? Number(n).toLocaleString() : '—'; }
function fmtShort(n) {
  if (n == null) return '—';
  if (n >= 1e9)  return (n/1e9).toFixed(1) + 'B';
  if (n >= 1e6)  return (n/1e6).toFixed(1) + 'M';
  if (n >= 1e3)  return (n/1e3).toFixed(1) + 'K';
  return String(n);
}
function esc(s) {
  // M2 fix: escape single quotes too
  return String(s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
function destroyCharts() {
  Object.keys(Charts).forEach(k=>{if(Charts[k]){Charts[k].destroy();Charts[k]=null;}});
}

// Content-type tab switcher (called from inline onclick)
function _switchCtTab(btn, cardId) {
  const card = document.getElementById(cardId);
  if (!card) return;
  const ct = btn.dataset.ct;
  card.querySelectorAll('.ct-tab').forEach(b => b.classList.remove('active'));
  card.querySelectorAll('.ct-panel').forEach(p => p.classList.remove('active'));
  btn.classList.add('active');
  const panel = card.querySelector(`.ct-panel[data-ct-panel="${ct}"]`);
  if (panel) panel.classList.add('active');
}

// TikTok suppression check — per market
function _isTikTokSuppressed(reportData, market) {
  if (!reportData) return false;
  const suppressed = reportData.tiktok_suppressed;
  // Per-market object or flat boolean
  if (typeof suppressed === 'boolean') return suppressed;
  if (typeof suppressed === 'object' && suppressed !== null) {
    if (market && market !== 'all') return !!(suppressed[market]);
    return Object.values(suppressed).some(Boolean);
  }
  return false;
}

// SOV Trend chart — shown only when grain !== 'lifetime'
function _renderSovTrendChart(brands, bgColors) {
  const container = document.getElementById('sovTrendChartWrap');
  if (!container) return;

  if (State.timeGrain === 'lifetime') {
    container.style.display = 'none';
    if (Charts.sovTrend) { Charts.sovTrend.destroy(); Charts.sovTrend = null; }
    return;
  }

  container.style.display = '';
  const grainKey = { monthly: 'by_month', weekly: 'by_week', daily: 'by_day' }[State.timeGrain] || 'by_month';

  // Collect labels from first brand that has grain data
  let labels = [];
  for (const b of brands) {
    const grain = b[grainKey] || [];
    if (grain.length) { labels = grain.map(g => g.period || g.date || g.week || g.month || '?'); break; }
  }
  if (!labels.length) {
    container.innerHTML = `<div style="padding:16px;color:var(--text3);font-size:0.8rem;">No ${State.timeGrain} data available — run analysis with sufficient date range.</div>`;
    return;
  }

  const datasets = brands.map((b, i) => ({
    label: b.name || '?',
    data: (b[grainKey] || []).map(g => g.composite_sov || g.sov || 0),
    borderColor: bgColors[i % bgColors.length],
    backgroundColor: bgColors[i % bgColors.length] + '22',
    borderWidth: 2, pointRadius: 3, tension: 0.35, fill: false,
  }));

  if (Charts.sovTrend) { Charts.sovTrend.destroy(); Charts.sovTrend = null; }

  // Ensure canvas exists in container
  let canvas = container.querySelector('canvas');
  if (!canvas) {
    container.innerHTML = '<canvas id="sovTrendChart"></canvas>';
    canvas = container.querySelector('canvas');
  }

  Charts.sovTrend = new Chart(canvas.getContext('2d'), {
    type: 'line',
    data: { labels, datasets },
    options: {
      ...CHART_OPTS,
      plugins: {
        legend: { display: true, position: 'top', labels: { color: '#8b949e', font: { size: 10 } } },
        tooltip: { callbacks: { label: ctx => `${ctx.dataset.label}: ${ctx.parsed.y.toFixed(1)} SOV (Dir.)` } },
      },
      scales: {
        y: { ...CHART_OPTS.scales?.y, title: { display: true, text: 'SOV Index (Dir.)', color: '#8b949e', font: { size: 10 } } },
      },
    },
  });
}

// Render executive insight cards
function renderInsightCards(insights) {
  const wrap = document.getElementById('insightCardsWrap');
  if (!wrap) return;
  if (!insights || !insights.length) { wrap.style.display = 'none'; return; }
  wrap.style.display = '';
  wrap.innerHTML = insights.slice(0, 5).map(ins => `
    <div class="insight-card fade-in">
      <span class="insight-icon">${ins.icon || '💡'}</span>
      <div class="insight-body">
        <div class="insight-brand">${esc(ins.brand || '')}</div>
        <div class="insight-text">${esc(ins.text || ins)}</div>
      </div>
    </div>
  `).join('');
}

// Render TikTok suppression notice
function renderTikTokNotice(reportData, market) {
  const notice = document.getElementById('tiktokNotice');
  if (!notice) return;
  const suppressed = reportData && reportData.tiktok_suppressed;
  if (!suppressed) { notice.style.display = 'none'; return; }
  let msg = '';
  if (typeof suppressed === 'boolean' && suppressed) {
    msg = `TikTok omitted — fewer than 2 posts detected. Composite SOV re-weighted across remaining platforms.`;
  } else if (typeof suppressed === 'object') {
    const suppressedMarkets = Object.entries(suppressed).filter(([,v])=>v).map(([k])=>k);
    if (!suppressedMarkets.length) { notice.style.display = 'none'; return; }
    if (market && market !== 'all' && !suppressed[market]) { notice.style.display = 'none'; return; }
    msg = `TikTok suppressed in: ${suppressedMarkets.join(', ')} — fewer than 2 posts detected. Composite SOV re-weighted.`;
  }
  if (msg) {
    notice.style.display = 'flex';
    notice.innerHTML = `<span class="notice-icon">ℹ</span> <span>${esc(msg)}</span>`;
  } else {
    notice.style.display = 'none';
  }
}

// ═══════════════════════════════════════════════════════════════════════════════
// INIT — server ping + pre-load existing report
// ═══════════════════════════════════════════════════════════════════════════════

(async function init() {
  // Wire time-grain toggle buttons
  document.querySelectorAll('.grain-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.grain-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      State.timeGrain = btn.dataset.grain || 'lifetime';
      renderResultsFiltered();
    });
  });

  // Load saved runs index
  await _fetchSavedRuns();
  renderSavedRunsList();

  try {
    const s = await fetch('/status').then(r => r.json());
    setLiveBadge(s.running);
    if (s.running) {
      updateDots('running');
      lockForm(true);
      const stopBtn = document.getElementById('stopBtn');
      if (stopBtn) { stopBtn.disabled = false; stopBtn.textContent = '■ Stop'; }
      State.pollInterval = setInterval(pollStatus, 2000);
      showPage('network');
      showLogPanel(true);
      setLogStatus('running', 'Analysis in progress — reconnected to running session…');
    }
  } catch(e) {}

  try {
    const rep = await fetch('/report').then(r => r.json());
    if (rep && (rep.brands?.length || rep.competitors?.length)) {
      _setReportData(rep);
      document.getElementById('dot-results').className = 'status-dot done';
    }
  } catch(e) {}
})();
