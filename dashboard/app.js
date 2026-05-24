/* ─────────────────────────────────────────────────────────────────────────
   Hermes — app.js  v2
   Page router · Form state · Canvas neural graph · /status polling · Charts
   ───────────────────────────────────────────────────────────────────────── */

'use strict';

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
  activePlatforms: new Set(),
  activePostTypeFilter: 'all', // all | paid | organic  (results page)
  // Drill-down filter state
  dd: {
    view:        'all',   // all | mine | competitors | vs
    brand:       '',      // specific brand name (non-vs views), '' = all
    platform:    'all',   // all | TikTok | Instagram | YouTube | Facebook
    postType:    'all',   // all | paid | organic
    vsMyBrands:  [],      // selected "my brand" names for vs view
    vsCompBrands:[],      // selected competitor names for vs view
  },
  agentStates: { profile: 'idle', feed: 'idle', scraper: 'idle', analyst: 'idle', reporter: 'idle', gate: 'idle' },
  lastLogs: [],
  uploadedFiles: [],
  partialShown: false,   // true once partial results have been rendered this run
};

const Charts = { spend: null, sov: null, engage: null, platform: null, sentiment: null };

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

// ── CPM Engine ───────────────────────────────────────────────────────────────
// Base CPMs: market-level platform benchmarks (USD per 1,000 impressions)
// Source: eMarketer, Statista, agency trading desk benchmarks 2024-25
const COUNTRY_CPM = {
  '':               { tiktok:3.50, instagram:8.00,  youtube:9.50,  facebook:7.50  },
  'United States':  { tiktok:5.50, instagram:12.00, youtube:15.00, facebook:11.00 },
  'United Kingdom': { tiktok:4.80, instagram:10.50, youtube:13.00, facebook:9.50  },
  'Canada':         { tiktok:4.50, instagram:10.00, youtube:12.50, facebook:9.00  },
  'Australia':      { tiktok:4.20, instagram:9.50,  youtube:11.50, facebook:8.50  },
  'Germany':        { tiktok:4.00, instagram:9.00,  youtube:11.00, facebook:8.00  },
  'France':         { tiktok:3.80, instagram:8.50,  youtube:10.50, facebook:7.50  },
  'Japan':          { tiktok:4.50, instagram:9.50,  youtube:12.00, facebook:9.00  },
  'South Korea':    { tiktok:3.50, instagram:8.00,  youtube:10.00, facebook:7.00  },
  'UAE':            { tiktok:4.00, instagram:9.00,  youtube:11.50, facebook:8.50  },
  'Saudi Arabia':   { tiktok:3.80, instagram:8.50,  youtube:11.00, facebook:8.00  },
  'Singapore':      { tiktok:3.80, instagram:8.50,  youtube:11.00, facebook:8.00  },
  'Malaysia':       { tiktok:1.80, instagram:3.50,  youtube:4.50,  facebook:3.00  },
  'Thailand':       { tiktok:1.50, instagram:3.00,  youtube:4.00,  facebook:2.80  },
  'Vietnam':        { tiktok:1.20, instagram:2.50,  youtube:3.50,  facebook:2.20  },
  'Indonesia':      { tiktok:1.00, instagram:2.20,  youtube:3.00,  facebook:2.00  },
  'Philippines':    { tiktok:1.00, instagram:2.00,  youtube:3.00,  facebook:1.80  },
  'India':          { tiktok:0.80, instagram:1.80,  youtube:2.50,  facebook:1.50  },
  'Brazil':         { tiktok:1.50, instagram:3.00,  youtube:4.00,  facebook:2.80  },
  'Mexico':         { tiktok:1.20, instagram:2.80,  youtube:3.80,  facebook:2.50  },
};

// Industry multipliers relative to general baseline (1.0)
// Derived from category-level CPM premium data (DV360, Meta Ads Manager benchmarks)
const INDUSTRY_CPM_MULT = {
  '':            1.00, // General / Mixed
  'fmcg':        0.95, // High volume, moderate CPM
  'food_bev':    0.90,
  'beauty':      1.10,
  'fashion':     1.05,
  'retail':      1.00,
  'tech':        1.30,
  'telco':       1.25,
  'finance':     2.20, // Highest category CPM
  'insurance':   2.00,
  'automotive':  1.80,
  'travel':      1.40,
  'health':      1.50,
  'entertainment':0.85,
  'gaming':      1.10,
  'education':   0.90,
  'real_estate': 1.60,
};

const INDUSTRY_LABELS = {
  '':'General / Mixed','fmcg':'FMCG / CPG','food_bev':'Food & Beverage',
  'beauty':'Beauty & Personal Care','fashion':'Fashion & Apparel','retail':'Retail & E-commerce',
  'tech':'Technology & Electronics','telco':'Telecoms','finance':'Financial Services',
  'insurance':'Insurance','automotive':'Automotive','travel':'Travel & Hospitality',
  'health':'Health & Pharma','entertainment':'Entertainment & Media',
  'gaming':'Gaming','education':'Education','real_estate':'Real Estate',
};

// 3-month rolling seasonal index (month 0=Jan … 11=Dec)
// Reflects ad spend seasonality: Q1 dip → Q2/Q3 moderate → Q4 peak
// Methodology: 3-month centred moving average applied to observed monthly spend curves
// (Source: Meta, TikTok quarterly revenue disclosures; Nielsen ad spend indices)
const SEASONAL_INDEX = [
  0.82, // Jan — post-holiday spend drop
  0.85, // Feb — Valentine's lift
  0.90, // Mar — Q1 close, spring campaigns
  0.93, // Apr
  0.95, // May
  0.97, // Jun — mid-year
  0.95, // Jul — summer lull in some markets
  0.97, // Aug
  1.02, // Sep — Q3/Q4 ramp
  1.10, // Oct — pre-holiday surge
  1.25, // Nov — peak (Singles Day, Black Friday)
  1.40, // Dec — peak (Christmas, year-end)
];

function getSeasonalIndex() {
  return SEASONAL_INDEX[new Date().getMonth()];
}

function getSeasonalLabel() {
  const months = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  const m = new Date().getMonth();
  const prev2 = [(m+10)%12,(m+11)%12].map(i=>months[i]).join(', ');
  return `3-month rolling avg (${prev2}, ${months[m]}): ×${getSeasonalIndex().toFixed(2)}`;
}

function effectiveCpm(platform, country, industry) {
  const base  = (COUNTRY_CPM[country] || COUNTRY_CPM[''])[platform.toLowerCase()] || 7.00;
  const iMult = INDUSTRY_CPM_MULT[industry] ?? 1.00;
  const sMult = getSeasonalIndex();
  return Math.round(base * iMult * sMult * 100) / 100;
}

function cpmDerivation(platform, country, industry) {
  const base  = (COUNTRY_CPM[country] || COUNTRY_CPM[''])[platform.toLowerCase()] || 7.00;
  const iMult = INDUSTRY_CPM_MULT[industry] ?? 1.00;
  const sMult = getSeasonalIndex();
  const eff   = Math.round(base * iMult * sMult * 100) / 100;
  return {
    base, iMult, sMult, effective: eff,
    label: `$${base} (${platform} ${country||'global'}) × ${iMult.toFixed(2)} (${INDUSTRY_LABELS[industry]||'general'} industry) × ${sMult.toFixed(2)} (seasonal) = $${eff}`,
  };
}

function getCpmDefaults(country) {
  return COUNTRY_CPM[country] || COUNTRY_CPM[''];
}

function updateCpmHint(country, industry) {
  const platforms = ['tiktok','instagram','youtube','facebook'];
  const labels    = ['TikTok','IG','YT','FB'];
  const hint = document.getElementById('cpmHint');
  if (!hint) return;
  const parts = platforms.map((p,i) => `${labels[i]} $${effectiveCpm(p, country, industry||'')}`);
  hint.textContent = `Auto CPM for ${country||'global'} · ${INDUSTRY_LABELS[industry||'']||'general'} (${new Date().toLocaleString('default',{month:'short'})} seasonal): ${parts.join(', ')}`;
}

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

// ── Country/Industry → update CPM hint live ──────────────────────────────────
function _refreshCpmHint() {
  const country  = document.getElementById('country').value;
  const industry = document.getElementById('industry').value;
  updateCpmHint(country, industry);
}
document.getElementById('country').addEventListener('change', _refreshCpmHint);
document.getElementById('industry').addEventListener('change', _refreshCpmHint);

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

  const cpmVal    = parseFloat(document.getElementById('cpmRate').value) || 0;
  const country   = document.getElementById('country').value;
  const industry  = document.getElementById('industry').value;

  return {
    advertisers:  [...State.advertisers],
    advertiser:   State.advertisers[0] || '',          // backward compat
    competitors:  [...State.competitors],
    country,
    industry,
    platforms,
    post_type:    State.postType,
    date_range:   dateRange,
    date_from:    dateFrom,
    date_to:      dateTo,
    cpm_rate:     cpmVal,
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

    // ── Partial results: render as soon as scraper/analyst checkpoint is ready ──
    if (s.running && s.partial_report && s.partial_report.competitors && s.partial_report.competitors.length) {
      if (!State.partialShown) {
        State.partialShown = true;
        // Tag scan_params so results page knows it came from params
        if (!s.partial_report.scan_params && State.reportData && State.reportData.scan_params) {
          s.partial_report.scan_params = State.reportData.scan_params;
        }
        State.reportData = s.partial_report;
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
            State.reportData = rep;
            saveRun(rep);
            lockForm(false);
            showPage('results');
          } catch(e) { lockForm(false); }
        }, 1000);
      } else {
        setLogStatus('done', 'Finished.'); updateDots('idle');
      }
    }
  } catch(e) {}
}

// ── Agent Card Updates ───────────────────────────────────────────────────────

const CARD_LOG_HINTS = {
  profile:  { idle: 'Awaiting activation…', active: 'Scraping brand profile pages…',   done: 'Profile baselines collected' },
  feed:     { idle: 'Awaiting activation…', active: 'Scrolling in-feed ads…',          done: 'Feed ads captured' },
  scraper:  { idle: 'Awaiting activation…', active: 'Collecting paid & organic data…', done: 'Data collection complete' },
  analyst:  { idle: 'Awaiting data…',       active: 'Analysing engagement & spend…',   done: 'Analysis complete' },
  reporter: { idle: 'Awaiting analysis…',   active: 'Composing intelligence report…',  done: 'Report compiled' },
  gate:     { idle: 'Awaiting output…',     active: 'Validating · computing CPM & ER…', done: 'Report approved ✓' },
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
  document.getElementById('cpmRate').value  = '0';
  State.advertisers.length = 0; State.competitors.length = 0;
  if (_advCtrl)  _advCtrl.renderTags();
  if (_compCtrl) _compCtrl.renderTags();
  document.getElementById('advertiserInput').value = '';
  document.getElementById('tagInput').value        = '';
  document.getElementById('country').value   = '';
  document.getElementById('industry').value  = '';
  _refreshCpmHint();

  document.querySelectorAll('.platform-option').forEach(label => {
    const cb  = label.querySelector('input[type=checkbox]');
    const val = cb.value;
    const checked = (val === 'TikTok' || val === 'Instagram');
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
  analyst:  { label:'ANALYST',       role:'Engagement Analyst',       color:'#38bdf8' },
  reporter: { label:'REPORTER',      role:'Intelligence Reporter',    color:'#a78bfa' },
  gate:     { label:'APPROVAL GATE', role:'CPM · ER · Validation',   color:'#22c55e' },
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

// Platform metadata — only the 4 supported platforms
const PLATFORM_META = {
  'tiktok':    { cls:'ch-tiktok',    icon:'pi-tiktok',    label:'TikTok' },
  'instagram': { cls:'ch-instagram', icon:'pi-instagram', label:'Instagram' },
  'youtube':   { cls:'ch-youtube',   icon:'pi-youtube',   label:'YouTube' },
  'facebook':  { cls:'ch-facebook',  icon:'pi-facebook',  label:'Facebook' },
};
function platformMeta(s) {
  const key=(s||'').toLowerCase().replace(/[^a-z]/g,'');
  return PLATFORM_META[key]||{cls:'ch-default',icon:'pi-default',label:s||'Social'};
}

// ── Average view-through rates by platform ───────────────────────────────────
const PLATFORM_AVG_VIEW_RATE = {
  tiktok:0.26, instagram:0.30, youtube:0.32, facebook:0.22, default:0.25,
};

// ── ER benchmarks: 3-month rolling by platform × industry ───────────────────
// Source: Socialinsider, Sprout Social, Rival IQ industry reports 2024-25
const INDUSTRY_ER_BENCHMARKS = {
  tiktok:    {'':5.0,fmcg:5.5,food_bev:6.0,beauty:7.0,fashion:6.5,retail:5.5,tech:4.5,telco:4.0,finance:3.5,insurance:3.0,automotive:4.5,travel:6.5,health:5.5,entertainment:8.0,gaming:7.5,education:5.0,real_estate:3.5},
  instagram: {'':1.5,fmcg:1.8,food_bev:2.2,beauty:2.5,fashion:2.0,retail:1.6,tech:1.2,telco:1.0,finance:0.9,insurance:0.8,automotive:1.3,travel:2.3,health:1.8,entertainment:2.8,gaming:2.5,education:1.5,real_estate:1.1},
  facebook:  {'':0.8,fmcg:0.9,food_bev:1.0,beauty:1.1,fashion:0.9,retail:0.8,tech:0.6,telco:0.5,finance:0.5,insurance:0.4,automotive:0.7,travel:1.0,health:0.8,entertainment:1.2,gaming:1.1,education:0.7,real_estate:0.5},
  youtube:   {'':2.0,fmcg:2.2,food_bev:2.5,beauty:3.0,fashion:2.5,retail:2.0,tech:1.8,telco:1.5,finance:1.5,insurance:1.2,automotive:2.0,travel:2.8,health:2.2,entertainment:3.5,gaming:3.0,education:2.0,real_estate:1.3},
};

function benchmarkFor(platform, industry) {
  const pk  = (platform||'').toLowerCase().replace(/[^a-z]/g,'');
  const ind = industry || '';
  const platMap = INDUSTRY_ER_BENCHMARKS[pk];
  if (!platMap) return 2.0;
  return platMap[ind] ?? platMap[''] ?? 2.0;
}

function renderResults(data) {
  if (!data || !data.competitors || !data.competitors.length) {
    const nd = document.getElementById('noDataState');
    const params = (data && data.scan_params) || {};
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

  const comp   = data.competitors;
  const params = data.scan_params || {};

  // Wire post-type filter toggle
  const ptWrap = document.getElementById('postTypeFilterWrap');
  const hasPaidOrg = comp.some(c => c.post_type === 'paid' || c.post_type === 'organic');
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
  State.dd = { view: 'all', brand: '', platform: 'all', postType: 'all', vsMyBrands: [], vsCompBrands: [] };
  buildContextBar(params, comp);
  buildDrilldownBar(params, comp);
  renderResultsFiltered();

  // TikTok embed script
  if (!document.getElementById('tiktok-embed-js')) {
    const s = document.createElement('script');
    s.id='tiktok-embed-js'; s.src='https://www.tiktok.com/embed.js'; s.async=true;
    document.body.appendChild(s);
  } else if (window.tiktokEmbed) { window.tiktokEmbed.render(); }

  document.getElementById('dot-results').className = 'status-dot done';
}

function buildContextBar(params, comp) {
  const bar = document.getElementById('contextBar');

  const paramPlats = params.platforms || [];
  const dataPlats  = [...new Set(comp.map(c => c.platform).filter(Boolean))];
  const allPlats   = [...new Set([...paramPlats, ...dataPlats])].filter(p =>
    ['TikTok','Instagram','YouTube','Facebook'].includes(p)
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

  // ── Reset button ─────────────────────────────────────────────────────────
  const resetBtn = document.getElementById('ddReset');
  if (resetBtn) {
    resetBtn.onclick = () => {
      State.dd = { view: 'all', brand: '', platform: 'all', postType: 'all', vsMyBrands: [], vsCompBrands: [] };
      if (brandSel) brandSel.value = '';
      // Re-select all options in vs multi-selects
      if (myBrandsSel)  Array.from(myBrandsSel.options).forEach(o => { o.selected = true; });
      if (compBrandsSel) Array.from(compBrandsSel.options).forEach(o => { o.selected = true; });
      bar.querySelectorAll('.dd-pill[data-view]').forEach(b => b.classList.toggle('active', b.dataset.view === 'all'));
      bar.querySelectorAll('.dd-pill[data-plat]').forEach(b => b.classList.toggle('active', b.dataset.plat === 'all'));
      bar.querySelectorAll('.dd-pill[data-pt]').forEach(b   => b.classList.toggle('active', b.dataset.pt   === 'all'));
      _syncVsVisibility();
      renderResultsFiltered();
    };
  }
}

function filteredComp() {
  if (!State.reportData) return [];
  const params   = State.reportData.scan_params || {};
  const myBrands = new Set(
    ((params.advertisers || (params.advertiser ? [params.advertiser] : [])))
      .map(b => (b||'').toLowerCase().trim())
      .filter(Boolean)
  );
  const dd = State.dd;

  let all = State.reportData.competitors || [];

  // ── View dimension ────────────────────────────────────────────────
  if (dd.view === 'mine') {
    all = all.filter(c => myBrands.has((c.name||'').toLowerCase().trim()));
  } else if (dd.view === 'competitors') {
    all = all.filter(c => !myBrands.has((c.name||'').toLowerCase().trim()));
  } else if (dd.view === 'vs') {
    // Multi-select vs: keep only selected my-brands + selected competitor brands
    const selMine = new Set((dd.vsMyBrands || []).map(b => (b||'').toLowerCase().trim()).filter(Boolean));
    const selComp = new Set((dd.vsCompBrands || []).map(b => (b||'').toLowerCase().trim()).filter(Boolean));
    const anyMine = selMine.size > 0;
    const anyComp = selComp.size > 0;
    if (anyMine || anyComp) {
      all = all.filter(c => {
        const n = (c.name||'').toLowerCase().trim();
        return (anyMine && selMine.has(n)) || (anyComp && selComp.has(n));
      });
    }
    // If both sets empty (nothing selected yet) show all — colours differentiate
  }

  // ── Specific brand pick (non-vs views only) ───────────────────────
  if (dd.view !== 'vs' && dd.brand) {
    const b = dd.brand.toLowerCase().trim();
    all = all.filter(c => (c.name||'').toLowerCase().trim() === b);
  }

  // ── Platform dimension ────────────────────────────────────────────
  if (dd.platform !== 'all') {
    all = all.filter(c => c.platform === dd.platform);
  } else if (State.activePlatforms.size > 0) {
    all = all.filter(c => State.activePlatforms.has(c.platform));
  }

  // ── Post type dimension ───────────────────────────────────────────
  const pt = dd.postType !== 'all' ? dd.postType : State.activePostTypeFilter;
  if (pt !== 'all') {
    all = all.filter(c => !c.post_type || c.post_type === pt || c.post_type === 'both');
  }

  return all;
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

function renderResultsFiltered() {
  const data = State.reportData;
  if (!data) return;
  const comp     = filteredComp();
  const bgColors = comp.map((_, i) => BRAND_COLORS[i % BRAND_COLORS.length]);

  // ── KPIs ────────────────────────────────────────────────────────────────
  const totalSpend    = comp.reduce((s,c) => s + (c.estimated_spend_usd||0), 0);
  const avgEngage     = comp.length
    ? comp.reduce((s,c) => s+(c.engagement_rate||0), 0) / comp.length : 0;

  // Unique brands (by name)
  const uniqueBrands = new Set(comp.map(c => (c.name||'').toLowerCase().trim()).filter(Boolean));
  const uniqueBrandCount = uniqueBrands.size || comp.length;

  // Top spender for Share of Spend KPI
  const topSpenderEntry = comp.length
    ? comp.reduce((best,c) => (c.estimated_spend_usd||0) > (best.estimated_spend_usd||0) ? c : best, comp[0])
    : null;
  const topSpenderSos = totalSpend > 0 && topSpenderEntry
    ? ((topSpenderEntry.estimated_spend_usd||0) / totalSpend * 100).toFixed(1)
    : '—';

  document.getElementById('kpiCount').textContent     = comp.length;
  document.getElementById('kpiPostCount').textContent = `across ${uniqueBrandCount} brand${uniqueBrandCount!==1?'s':''}`;
  document.getElementById('kpiSpend').textContent     = '$' + totalSpend.toLocaleString(undefined,{maximumFractionDigits:0});
  document.getElementById('kpiEngage').textContent    = avgEngage.toFixed(2) + '%';

  const kpiTop = document.getElementById('kpiTopSpender');
  const kpiTopSub = document.getElementById('kpiTopSpenderSub');
  if (kpiTop) kpiTop.textContent = topSpenderEntry ? esc(topSpenderEntry.name||'—') : '—';
  if (kpiTopSub) kpiTopSub.textContent = totalSpend > 0 && topSpenderEntry
    ? `${topSpenderSos}% of total est. spend · ${topSpenderEntry.platform||''}`
    : 'largest est. media value';

  // Benchmark comparison (industry-adjusted)
  const scanIndustry = (data.scan_params || {}).industry || '';
  const avgBench = comp.length
    ? comp.reduce((s,c)=>s+benchmarkFor(c.platform, scanIndustry),0)/comp.length : 2.0;
  const diff = avgEngage - avgBench;
  const kpiBench = document.getElementById('kpiBenchmark');
  if (kpiBench) {
    kpiBench.textContent = diff >= 0
      ? `+${diff.toFixed(2)}% above industry avg`
      : `${diff.toFixed(2)}% below industry avg`;
    kpiBench.style.color = diff >= 0 ? 'var(--green)' : 'var(--red)';
  }

  // Table subtitle
  const params = data.scan_params || {};
  const tSub = document.getElementById('tableSubtitle');
  if (tSub) {
    const pt = params.post_type || 'both';
    const label = pt === 'paid' ? 'Paid only' : pt === 'organic' ? 'Organic only' : 'Paid + Organic';
    tSub.textContent = `${comp.length} entries across ${uniqueBrandCount} brand${uniqueBrandCount!==1?'s':''} · ${label} · sorted by est. media value`;
  }

  // ── Charts ───────────────────────────────────────────────────────────────
  const labels  = comp.map(c => c.name || c.handle || '?');
  const spends  = comp.map(c => c.estimated_spend_usd || 0);
  const engages = comp.map(c => c.engagement_rate || 0);
  const benchmarks = comp.map(c => benchmarkFor(c.platform, scanIndustry));

  destroyCharts();

  // Share of Spend — absolute est. media value per brand entry
  Charts.spend = new Chart(document.getElementById('spendChart').getContext('2d'), {
    type: 'bar',
    data: { labels, datasets: [{ data: spends, backgroundColor: bgColors, borderRadius:5, borderSkipped:false, label:'Est. Value (USD)' }] },
    options: { ...CHART_OPTS, plugins: { legend:{display:false}, tooltip:{callbacks:{label:ctx=>`$${Number(ctx.parsed.y).toLocaleString()}`}} } },
  });

  // Share of Voice — total impressions per brand entry
  const sovLabels = comp.map(c => c.name || c.handle || '?');
  const sovViews  = comp.map(c => (c.metrics||{}).views||0);
  const totalViews = sovViews.reduce((s,v)=>s+v,0);
  Charts.sov = new Chart(document.getElementById('sovChart').getContext('2d'), {
    type: 'bar',
    data: { labels: sovLabels, datasets: [{ data: sovViews, backgroundColor: bgColors, borderRadius:5, borderSkipped:false, label:'Impressions' }] },
    options: { ...CHART_OPTS, plugins: { legend:{display:false}, tooltip:{callbacks:{label:ctx=>{
      const pct = totalViews > 0 ? (ctx.parsed.y/totalViews*100).toFixed(1)+'%' : '';
      return `${Number(ctx.parsed.y).toLocaleString()} views${pct?' · SoV: '+pct:''}`;
    }}} } },
  });

  // Engagement Rate vs benchmark
  Charts.engage = new Chart(document.getElementById('engageChart').getContext('2d'), {
    type: 'bar',
    data: {
      labels,
      datasets: [
        { data: engages,    backgroundColor: bgColors,              borderRadius:5, borderSkipped:false, label:'Engagement Rate %' },
        { data: benchmarks, backgroundColor: 'rgba(248,81,73,0.15)',borderRadius:3, borderSkipped:false, label:'Platform Benchmark', borderColor:'rgba(248,81,73,0.6)', borderWidth:1 },
      ]
    },
    options: { ...CHART_OPTS, plugins: { legend:{display:true,position:'top',labels:{color:'#8b949e',font:{size:10}}}, tooltip:{callbacks:{label:ctx=>`${ctx.dataset.label}: ${ctx.parsed.y.toFixed(2)}%`}} } },
  });

  // Platform impression share — doughnut by platform
  const platViewMap = {};
  comp.forEach(c => {
    const p = c.platform || 'Unknown';
    platViewMap[p] = (platViewMap[p]||0) + ((c.metrics||{}).views||0);
  });
  const platLabels = Object.keys(platViewMap);
  Charts.platform = new Chart(document.getElementById('platformChart').getContext('2d'), {
    type: 'doughnut',
    data: { labels: platLabels, datasets: [{ data: platLabels.map(k=>platViewMap[k]), backgroundColor: BRAND_COLORS, hoverOffset:6, borderColor:'#060d1c', borderWidth:2 }] },
    options: { responsive:true, cutout:'62%', plugins:{ legend:{display:true,position:'right',labels:{font:{size:11},color:'#8b949e'}}, tooltip:{callbacks:{label:ctx=>`${ctx.label}: ${Number(ctx.parsed).toLocaleString()} views`}} } },
  });

  // ── Content Intel ─────────────────────────────────────────────────────────
  renderContentIntel(comp, bgColors);

  // ── Table ─────────────────────────────────────────────────────────────────
  const totalTableSpend = comp.reduce((s,c)=>(s+(c.estimated_spend_usd||0)),0);
  const sorted = [...comp].sort((a,b) => (b.estimated_spend_usd||0) - (a.estimated_spend_usd||0));
  const isVsView = State.dd.view === 'vs';
  const tbody = document.getElementById('resultsTableBody');
  tbody.innerHTML = sorted.map((c, i) => {
    const m = c.metrics || {};
    const interactions = (m.likes||0) + (m.comments||0) + (m.shares||0) + (m.saves||0);
    const sentClass = (c.sentiment||'').toLowerCase();
    // In vs-view: my brands = green, competitors = accent blue
    const color = isVsView
      ? (_isMyBrand(c) ? C.green : C.accent)
      : bgColors[i % bgColors.length];
    const pt = c.post_type || 'both';
    const ptBadge = `<span class="post-type-badge ${pt}">${esc(pt)}</span>`;
    const bench = benchmarkFor(c.platform, scanIndustry);
    const erDiff = (c.engagement_rate||0) - bench;
    const erColor = erDiff >= 0 ? 'var(--green)' : 'var(--red)';
    const erDiffStr = (erDiff >= 0 ? '+' : '') + erDiff.toFixed(2) + '%';
    const sos = totalTableSpend > 0
      ? ((c.estimated_spend_usd||0) / totalTableSpend * 100).toFixed(1) + '%'
      : '—';
    return `<tr>
      <td><span style="display:inline-flex;align-items:center;gap:7px;">
        <span style="width:8px;height:8px;border-radius:50%;background:${color};display:inline-block;flex-shrink:0;"></span>
        <strong>${esc(c.name||'—')}</strong>
      </span></td>
      <td style="color:#64748b;">${esc(c.handle||'—')}</td>
      <td><span class="platform-badge">${esc(c.platform||'Multi')}</span></td>
      <td>${ptBadge}</td>
      <td>${fmtShort(m.views)}</td>
      <td>${fmtShort(interactions)}</td>
      <td>${fmtShort(m.likes)}</td>
      <td>${fmtShort(m.comments)}</td>
      <td>${fmtShort(m.shares)}</td>
      <td>${fmtShort(m.saves)}</td>
      <td>${fmtShort(m.followers)}</td>
      <td><strong>${(c.engagement_rate||0).toFixed(2)}%</strong></td>
      <td style="color:${erColor};font-size:0.78rem;">${erDiffStr}</td>
      <td style="color:#2563eb;font-weight:700;">$${(c.estimated_spend_usd||0).toLocaleString(undefined,{maximumFractionDigits:0})}</td>
      <td style="color:var(--text2);font-weight:600;">${sos}</td>
      <td><span class="sentiment-badge ${sentClass}">${esc(c.sentiment||'—')}</span></td>
    </tr>`;
  }).join('');

  renderTopPosts({ competitors: comp });
  renderCalcTree(data);
}

function renderContentIntel(comp, bgColors) {
  const container = document.getElementById('contentIntelBody');
  if (!container) return;

  const hasContent = comp.some(c =>
    (c.top_posts && c.top_posts.length) ||
    (c.hashtags && c.hashtags.length) ||
    (c.content_themes && c.content_themes.length) ||
    (c.paid_campaigns && c.paid_campaigns.length)
  );
  if (!hasContent) {
    container.innerHTML = '<div style="padding:20px 24px;color:var(--text3);font-size:0.82rem;">No post content data available — run a deeper analysis to populate this section.</div>';
    return;
  }

  const grid = document.createElement('div');
  grid.className = 'content-intel-grid';

  comp.forEach((c, i) => {
    const color  = bgColors[i % bgColors.length];
    const posts  = c.top_posts      || [];
    const tags   = c.hashtags       || [];
    const themes = c.content_themes || [];
    const campaigns = c.paid_campaigns || [];
    const pt = c.post_type || 'both';

    const card = document.createElement('div');
    card.className = 'content-intel-card fade-in';

    let html = `
      <div class="ci-brand-row">
        <span class="ci-color-dot" style="background:${color}"></span>
        <span class="ci-brand-name">${esc(c.name||'—')}</span>
        ${c.handle ? `<span class="ci-handle">${esc(c.handle)}</span>` : ''}
        <span class="platform-badge" style="margin-left:auto;">${esc(c.platform||'Multi')}</span>
        <span class="post-type-badge ${pt}" style="margin-left:6px;">${esc(pt)}</span>
      </div>`;

    if (campaigns.length) {
      html += `<div class="ci-section-label">Paid Campaigns</div><ul class="ci-posts">`;
      campaigns.forEach(camp => { html += `<li class="ci-post-item"><span class="post-type-badge paid" style="font-size:0.62rem;">paid</span>${esc(camp)}</li>`; });
      html += `</ul>`;
    }

    // Only show posts with a real URL — skip AI-generated descriptions
    const realPosts = posts.filter(p => typeof p === 'object' && p.url && p.url !== 'null' && p.url !== '');
    if (realPosts.length) {
      html += `<div class="ci-section-label">Top Posts</div><ul class="ci-posts">`;
      realPosts.forEach(p => {
        const url      = p.url;
        const postType = p.post_type || pt;
        const likes    = p.likes  ? `${fmtShort(p.likes)} likes` : '';
        const views    = p.views  ? `${fmtShort(p.views)} views` : '';
        const statParts = [likes, views].filter(Boolean);
        const statStr  = statParts.length
          ? `<span style="color:var(--text3);font-size:0.72rem;margin-left:6px;">${statParts.join(' · ')}</span>` : '';
        const typeBadge = `<span class="post-type-badge ${postType}" style="font-size:0.62rem;">${esc(postType)}</span>`;
        let domain = ''; try { domain = new URL(url).hostname.replace('www.',''); } catch {}
        html += `<li class="ci-post-item">${typeBadge}<a href="${esc(url)}" target="_blank" rel="noopener noreferrer" class="ci-post-link">${esc(domain||url)}</a>${statStr}</li>`;
      });
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
function _tiktokVideoId(url) {
  const m=url.match(/\/video\/(\d+)/); return m?m[1]:null;
}
function buildPostEmbed(url,caption,platform,idx){
  const plat=(platform||'').toLowerCase();
  const rankBadge=`<span class="post-tile-rank">#${idx+1}</span>`;
  if(plat.includes('youtube')||url.includes('youtu')){
    const vid=_ytVideoId(url);
    if(vid) return `<div class="post-tile-embed">${rankBadge}<iframe src="https://www.youtube.com/embed/${esc(vid)}" frameborder="0" allow="accelerometer;autoplay;clipboard-write;encrypted-media;gyroscope;picture-in-picture" allowfullscreen loading="lazy"></iframe></div><div class="post-tile-caption">${esc(caption)}</div>`;
  }
  if(plat.includes('tiktok')||url.includes('tiktok.com')){
    const vid=_tiktokVideoId(url);
    if(vid) return `<div class="post-tile-embed post-tile-tiktok">${rankBadge}<blockquote class="tiktok-embed" cite="${esc(url)}" data-video-id="${esc(vid)}" style="max-width:320px;min-width:240px;"><section></section></blockquote></div><div class="post-tile-caption">${esc(caption)}</div>`;
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

// ── Calculation Tree ──────────────────────────────────────────────────────────

function renderCalcTree(data) {
  const card = document.getElementById('calcTreeCard');
  const body = document.getElementById('calcTreeBody');
  if (!card || !body) return;

  const a = data.assumptions;
  if (!a) { card.style.display='none'; return; }
  card.style.display = 'block';

  const cpmInput = document.getElementById('cpmOverride');
  if (cpmInput && !cpmInput._seeded) {
    cpmInput.value = typeof a.cpm_rate_usd === 'number' ? a.cpm_rate_usd : 0;
    cpmInput._seeded = true;
  }

  const pt       = a.post_type || 'both';
  const country  = (data.scan_params||{}).country  || '';
  const industry = (data.scan_params||{}).industry || '';
  const iLabel   = INDUSTRY_LABELS[industry] || 'General';
  const iMult    = INDUSTRY_CPM_MULT[industry] ?? 1.00;
  const sMult    = getSeasonalIndex();
  const sLabel   = getSeasonalLabel();

  // Build per-platform effective CPM derivation table for display
  const platList = ['TikTok','Instagram','YouTube','Facebook'];
  const cpmRows  = platList.map(p => {
    const base = (COUNTRY_CPM[country] || COUNTRY_CPM[''])[p.toLowerCase()] || 7.00;
    const eff  = Math.round(base * iMult * sMult * 100) / 100;
    return `<tr><td style="color:var(--text2);padding:3px 8px;">${p}</td><td style="padding:3px 8px;">$${base.toFixed(2)}</td><td style="padding:3px 8px;">×${iMult.toFixed(2)}</td><td style="padding:3px 8px;">×${sMult.toFixed(2)}</td><td style="color:var(--accent);font-weight:700;padding:3px 8px;">= $${eff.toFixed(2)}</td></tr>`;
  }).join('');

  let html = `
    <div class="calc-assumptions">
      <div class="calc-pill">Content Type: <strong>${esc(pt)}</strong></div>
      <div class="calc-pill">Market: <strong>${esc(country||'Global')}</strong></div>
      <div class="calc-pill">Industry: <strong>${esc(iLabel)}</strong></div>
      <div class="calc-pill">Total Interactions: <strong>${fmt(a.total_interactions)}</strong></div>
      <div class="calc-pill">Total Impressions: <strong>${fmt(a.total_impressions)}</strong></div>
      <div class="calc-pill">Est. Paid Value: <strong>$${fmt(a.total_spend_paid_usd)}</strong></div>
      <div class="calc-pill">Est. Organic Value: <strong>$${fmt(a.total_spend_org_usd)}</strong></div>
      <div class="calc-pill total">Est. Total Media Value: <strong>$${fmt(a.total_spend_usd)}</strong></div>
    </div>

    <div class="calc-formula-row" style="flex-direction:column;align-items:flex-start;gap:8px;">
      <div class="calc-formula-label" style="margin-bottom:4px;">CPM Derivation — Market × Industry × Seasonal</div>
      <div style="font-size:0.75rem;color:var(--text3);margin-bottom:6px;">
        Formula: <strong>Base market CPM</strong> × <strong>Industry multiplier</strong> × <strong>Seasonal index</strong><br>
        Industry: ${esc(iLabel)} (×${iMult.toFixed(2)}) · ${esc(sLabel)}<br>
        Sources: eMarketer, Statista, Meta/TikTok/YouTube quarterly revenue disclosures, agency trading desk benchmarks 2024-25
      </div>
      <table style="border-collapse:collapse;font-size:0.75rem;font-family:var(--mono);">
        <thead><tr style="color:var(--text3);">
          <th style="padding:3px 8px;text-align:left;">Platform</th>
          <th style="padding:3px 8px;">Base (${esc(country||'Global')})</th>
          <th style="padding:3px 8px;">Industry ×</th>
          <th style="padding:3px 8px;">Seasonal ×</th>
          <th style="padding:3px 8px;">Effective CPM</th>
        </tr></thead>
        <tbody>${cpmRows}</tbody>
      </table>
    </div>
    ${(typeof a.cpm_rate_usd === 'number' && a.cpm_rate_usd > 0) ? `
    <div class="calc-formula-row" style="border-left:3px solid var(--amber);padding-left:10px;">
      <div class="calc-formula-label" style="color:var(--amber);">CPM Override Active</div>
      ${esc('$' + a.cpm_rate_usd + '/1K impressions applied uniformly. Market/industry/seasonal adjustments bypassed.')}
    </div>` : ''}
    <div class="calc-formula-row">
      <div class="calc-formula-label">Paid Spend Formula</div>${esc(a.spend_formula_paid||'(Views / Avg Platform View Rate) / 1,000 × Platform CPM')}
    </div>
    <div class="calc-formula-row">
      <div class="calc-formula-label">Avg View-Through Rates</div>${(() => {
        const vr = a.avg_view_rates || {TikTok:'26%',Instagram:'30%',YouTube:'32%',Facebook:'22%'};
        return Object.entries(vr).filter(([k])=>k!=='default').map(([k,v])=>`${esc(k)}: ${esc(String(v))}`).join(' · ');
      })()}
    </div>
    <div class="calc-formula-row">
      <div class="calc-formula-label">Organic Value Formula</div>${esc(a.spend_formula_organic||'interactions × $0.75')}
    </div>
    <div class="calc-formula-row">
      <div class="calc-formula-label">Mixed Formula</div>${esc(a.spend_formula_both||'(Views×60%/Avg View Rate/1K×CPM) + (Interactions×40%×$0.75)')}
    </div>
    <div class="calc-formula-row">
      <div class="calc-formula-label">Engagement Rate</div>${esc(a.engagement_rate_formula||'TikTok/YT: interactions/views×100 · IG/FB: interactions/followers×100').replace(/\n/g,'<br>')}
    </div>
    <div class="calc-formula-row">
      <div class="calc-formula-label">ER Benchmarks</div>${esc(a.benchmark_note||'3-month rolling industry standard. Source: Socialinsider, Sprout Social, Rival IQ 2024-25.')}
    </div>`;

  if (a.brand_breakdowns && a.brand_breakdowns.length) {
    html += '<div class="calc-brand-grid">';
    a.brand_breakdowns.forEach((b, i) => {
      const color = BRAND_COLORS[i % BRAND_COLORS.length];
      const erSign = (b.er_vs_benchmark||0) >= 0 ? '+' : '';
      const erColor = (b.er_vs_benchmark||0) >= 0 ? 'var(--green)' : 'var(--red)';
      html += `
        <div class="calc-brand-card">
          <div class="calc-brand-name">
            <span style="width:8px;height:8px;border-radius:50%;background:${color};display:inline-block;flex-shrink:0;"></span>
            ${esc(b.brand)}
            <span class="post-type-badge ${b.post_type||'both'}" style="font-size:0.62rem;margin-left:4px;">${esc(b.post_type||'both')}</span>
          </div>
          <div class="calc-row"><span>Platform</span><span class="calc-val">${esc(b.platform||'—')}</span></div>
          <div class="calc-row"><span>Likes</span><span class="calc-val">${fmt(b.likes)}</span></div>
          <div class="calc-row"><span>Comments</span><span class="calc-val">${fmt(b.comments)}</span></div>
          <div class="calc-row"><span>Shares</span><span class="calc-val">${fmt(b.shares)}</span></div>
          <div class="calc-row"><span>Saves</span><span class="calc-val">${fmt(b.saves)}</span></div>
          <div class="calc-row"><span>Views / Impressions</span><span class="calc-val">${fmt(b.views)}</span></div>
          <div class="calc-row"><span>Followers</span><span class="calc-val">${fmt(b.followers)}</span></div>
          <div class="calc-row"><span>Interactions</span><span class="calc-val accent">${fmt(b.interactions)}</span></div>
          <div class="calc-row"><span>ER Denominator</span><span class="calc-val" style="font-size:0.72rem;">${fmt(b.er_denominator)} ${esc(b.er_denominator_label||'')}</span></div>
          <div class="calc-row"><span>Engagement Rate</span><span class="calc-val green">${(b.engagement_rate||0).toFixed(2)}%</span></div>
          <div class="calc-row"><span>vs Benchmark</span><span class="calc-val" style="color:${erColor};">${erSign}${(b.er_vs_benchmark||0).toFixed(2)}%</span></div>
          <div class="calc-row"><span>CPM Used</span><span class="calc-val">$${b.cpm_used||'—'}</span></div>
          <div class="calc-row"><span>Est. Value</span><span class="calc-val accent">$${fmt(b.spend_usd)}</span></div>
          <div class="calc-formula-inline">${esc(b.spend_note||b.spend_formula||'')}</div>
          <div class="calc-formula-inline" style="color:var(--text3);">${esc(b.er_formula||'')}</div>
        </div>`;
    });
    html += '</div>';
  }

  body.innerHTML = html;
}

// ── Recalc ────────────────────────────────────────────────────────────────────
;(function wireRecalc() {
  const btn = document.getElementById('recalcBtn');
  if (!btn) return;
  btn.addEventListener('click', () => {
    const newCpm    = parseFloat(document.getElementById('cpmOverride').value) || 0;
    if (!State.reportData) return;
    const data      = State.reportData;
    const a         = data.assumptions;
    const country   = (data.scan_params||{}).country || '';
    const industry  = (data.scan_params||{}).industry || '';

    ;(data.competitors||[]).forEach((c, i) => {
      const m    = c.metrics || {};
      const plat = (c.platform||'').toLowerCase().split('/')[0].trim();
      const cpm  = newCpm > 0 ? newCpm : effectiveCpm(plat, country, industry);
      const deriv = newCpm > 0 ? null : cpmDerivation(plat, country, industry);
      const pt   = c.post_type || 'both';
      const views = m.views || 0;
      const interactions = (m.likes||0)+(m.comments||0)+(m.shares||0)+(m.saves||0);

      let spend;
      if      (pt==='paid')    spend = Math.round((views/1000)*cpm*100)/100;
      else if (pt==='organic') spend = Math.round(interactions*0.75*100)/100;
      else {
        const paid_part = Math.round((views*0.60/1000)*cpm*100)/100;
        const org_part  = Math.round(interactions*0.40*0.75*100)/100;
        spend = Math.round((paid_part+org_part)*100)/100;
      }
      c.estimated_spend_usd = spend;
      c.cpm_used = cpm;

      if (a && a.brand_breakdowns && a.brand_breakdowns[i]) {
        const b = a.brand_breakdowns[i];
        b.spend_usd  = spend;
        b.cpm_used   = cpm;
        b.spend_note = newCpm > 0
          ? `User CPM override: $${newCpm}/1K impressions`
          : `Auto CPM: ${deriv ? deriv.label : `$${cpm}`}`;
      }
    });

    if (a) {
      a.cpm_rate_usd = newCpm > 0 ? newCpm : 'market+industry+seasonal';
      a.cpm_note     = newCpm > 0
        ? `User-set override: $${newCpm}/1K impressions (bypasses market/industry/seasonal adjustments)`
        : `Auto: base market CPM × industry multiplier (${INDUSTRY_LABELS[industry]||'general'}) × seasonal index (${getSeasonalLabel()})`;
      a.total_spend_usd = Math.round((data.competitors||[]).reduce((s,c)=>s+(c.estimated_spend_usd||0),0)*100)/100;
    }

    if (cpmInput) cpmInput._seeded = false;
    renderResults(data);
  });
})();

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
  if (!report || !report.competitors) return;
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
  const sp    = report.scan_params || {};
  const comps = report.competitors || [];
  const total = comps.reduce((s,c) => s + (c.estimated_spend_usd||0), 0);

  // Sheet 1 header: scan metadata
  const metaRows = [
    ['Run Date', new Date().toLocaleString()],
    ['Brands', [...(sp.advertisers||[]), ...(sp.competitors||[])].join(', ')],
    ['Market', sp.country || 'Global'],
    ['Industry', sp.industry || 'General'],
    ['Date Range', sp.date_range || ''],
    ['Platforms', (sp.platforms||[]).join(', ')],
    ['Content Type', sp.post_type || ''],
    ['Analysis Depth', sp.depth || ''],
    [],
  ].map(r => r.map(v => `"${String(v||'').replace(/"/g,'""')}"`).join(','));

  const cols = [
    'Brand','Handle','Platform','Type','Impressions','Interactions',
    'Likes','Comments','Shares','Saves','Followers',
    'Eng. Rate (%)','Benchmark ER (%)','ER vs Benchmark (%)','Est. Value (USD)',
    'Share of Spend (%)','CPM Used','Sentiment',
  ];

  const dataRows = comps.map(c => {
    const m   = c.metrics || {};
    const interactions = (m.likes||0)+(m.comments||0)+(m.shares||0)+(m.saves||0);
    const sos = total > 0 ? ((c.estimated_spend_usd||0)/total*100).toFixed(2) : '0';
    return [
      c.name, c.handle, c.platform, c.post_type,
      m.views, interactions, m.likes, m.comments, m.shares, m.saves, m.followers,
      c.engagement_rate, c.benchmark_er_pct, c.er_vs_benchmark,
      c.estimated_spend_usd, sos, c.cpm_used, c.sentiment,
    ].map(v => `"${String(v||'').replace(/"/g,'""')}"`).join(',');
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
    const sp     = entry.params || {};
    const comps  = (entry.report.competitors || []);
    const total  = comps.reduce((s,c) => s + (c.estimated_spend_usd||0), 0);
    const avgER  = comps.length ? (comps.reduce((s,c) => s + (c.engagement_rate||0), 0) / comps.length) : 0;
    const brands = [...(sp.advertisers||[]), ...(sp.competitors||[])];
    const tsLabel = new Date(entry.ts).toLocaleString('en-SG', { dateStyle:'medium', timeStyle:'short' });

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
        ${brands.slice(0,6).map(b => `<span class="src-pill">${esc(b)}</span>`).join('')}
        ${brands.length > 6 ? `<span class="src-pill muted">+${brands.length-6} more</span>` : ''}
      </div>
      <div class="src-stats">
        <div class="src-stat"><span class="src-stat-label">Posts</span><span class="src-stat-val">${comps.length}</span></div>
        <div class="src-stat"><span class="src-stat-label">Est. Value</span><span class="src-stat-val">$${fmtShort(total)}</span></div>
        <div class="src-stat"><span class="src-stat-label">Avg ER</span><span class="src-stat-val">${avgER.toFixed(2)}%</span></div>
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
  if (!State.reportData||!State.reportData.competitors) return;
  const allComp = filteredComp();
  const totalCsvSpend = allComp.reduce((s,c)=>s+(c.estimated_spend_usd||0),0);
  const cols = ['brand','handle','platform','post_type','impressions','interactions','likes','comments','shares','saves','followers','engagement_rate_pct','benchmark_er_pct','er_vs_benchmark_pct','est_value_usd','share_of_spend_pct','cpm_used','sentiment'];
  const rows = allComp.map(c => {
    const m = c.metrics||{};
    const interactions = (m.likes||0)+(m.comments||0)+(m.shares||0)+(m.saves||0);
    const sos = totalCsvSpend > 0 ? ((c.estimated_spend_usd||0)/totalCsvSpend*100).toFixed(2) : 0;
    return [c.name,c.handle,c.platform,c.post_type,m.views,interactions,m.likes,m.comments,m.shares,m.saves,m.followers,
            c.engagement_rate,c.benchmark_er_pct,c.er_vs_benchmark,c.estimated_spend_usd,sos,c.cpm_used,c.sentiment]
      .map(v=>`"${String(v||'').replace(/"/g,'""')}"`)
      .join(',');
  });
  const csv  = [cols.join(','),...rows].join('\n');
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
  return String(s)
    .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function destroyCharts() {
  Object.keys(Charts).forEach(k=>{if(Charts[k]){Charts[k].destroy();Charts[k]=null;}});
}

// ═══════════════════════════════════════════════════════════════════════════════
// INIT — server ping + pre-load existing report
// ═══════════════════════════════════════════════════════════════════════════════

(async function init() {
  // Seed CPM hint with initial (Global / General) defaults
  _refreshCpmHint();

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
    if (rep && rep.competitors && rep.competitors.length) {
      State.reportData = rep;
      document.getElementById('dot-results').className = 'status-dot done';
    }
  } catch(e) {}
})();
