/* AgenticSearch frontend — vanilla JS, no dependencies */

'use strict';

// ── State ──────────────────────────────────────────────────────────────────
let currentJobId = null;
let pollTimer    = null;

// ── DOM refs ───────────────────────────────────────────────────────────────
const form         = document.getElementById('search-form');
const queryInput   = document.getElementById('query-input');
const searchBtn    = document.getElementById('search-btn');
const statusSec    = document.getElementById('status-section');
const phaseLabel   = document.getElementById('phase-label');
const errorBanner  = document.getElementById('error-banner');
const errorMsg     = document.getElementById('error-msg');
const resultsSec   = document.getElementById('results-section');
const tableHead    = document.getElementById('table-head');
const tableBody    = document.getElementById('table-body');
const metaEntityType = document.getElementById('meta-entity-type');
const metaSummary  = document.getElementById('meta-summary');
const anglesList   = document.getElementById('angles-list');
const runMeta      = document.getElementById('run-meta');
const exportJson   = document.getElementById('btn-export-json');
const exportCsv    = document.getElementById('btn-export-csv');

// Modal
const modalOverlay   = document.getElementById('modal-overlay');
const modalClose     = document.getElementById('modal-close');
const modalColLabel  = document.getElementById('modal-col-label');
const modalValue     = document.getElementById('modal-value');
const modalConf      = document.getElementById('modal-conf');
const modalSnippet   = document.getElementById('modal-snippet');
const modalSourceUrl = document.getElementById('modal-source-url');
const modalSourceTitle = document.getElementById('modal-source-title');

// ── Helpers ────────────────────────────────────────────────────────────────
function show(el)  { el.classList.remove('hidden'); }
function hide(el)  { el.classList.add('hidden'); }

function showError(msg) {
  errorMsg.textContent = msg;
  show(errorBanner);
}

function clearError() { hide(errorBanner); }

function setPhase(raw) {
  const labels = {
    queued:      'Queued…',
    pending:     'Starting pipeline…',
    planning:    'Planning schema…',
    searching:   'Searching the web…',
    scraping:    'Scraping pages…',
    extracting:  'Extracting entities…',
    merging:     'Merging & deduplicating…',
    gap_filling: 'Gap-fill enrichment…',
    done:        'Done!',
  };
  phaseLabel.textContent = labels[raw] || raw || 'Working…';
}

function confClass(conf) {
  if (conf >= 0.8) return 'conf-high';
  if (conf >= 0.5) return 'conf-medium';
  return 'conf-low';
}

function humanColumn(col) {
  return col.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
}

// ── Form submit ────────────────────────────────────────────────────────────
form.addEventListener('submit', async (e) => {
  e.preventDefault();
  const query = queryInput.value.trim();
  if (!query) return;

  if (pollTimer) clearInterval(pollTimer);
  currentJobId = null;

  clearError();
  hide(resultsSec);
  show(statusSec);
  searchBtn.disabled = true;
  setPhase('pending');

  try {
    const res = await fetch('/api/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query }),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }

    const job = await res.json();
    currentJobId = job.job_id;
    startPolling();

  } catch (err) {
    hide(statusSec);
    searchBtn.disabled = false;
    showError(err.message);
  }
});

// ── Example chips ──────────────────────────────────────────────────────────
document.querySelectorAll('.example-chip').forEach(btn => {
  btn.addEventListener('click', () => {
    queryInput.value = btn.dataset.q;
    form.dispatchEvent(new Event('submit'));
  });
});

// ── Polling ────────────────────────────────────────────────────────────────
let _pollErrors = 0;
const _MAX_POLL_ERRORS = 8; // stop only after 8 consecutive failures (~16s)

function startPolling() {
  _pollErrors = 0;
  pollTimer = setInterval(pollJob, 2000);
  pollJob(); // immediate first check
}

async function pollJob() {
  if (!currentJobId) return;

  try {
    const res = await fetch(`/api/search/${currentJobId}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const job = await res.json();

    _pollErrors = 0; // reset on success
    setPhase(job.phase || job.status);

    if (job.status === 'done') {
      clearInterval(pollTimer);
      hide(statusSec);
      searchBtn.disabled = false;
      renderResults(job.result);
    } else if (job.status === 'failed') {
      clearInterval(pollTimer);
      hide(statusSec);
      searchBtn.disabled = false;
      showError(job.error || 'Pipeline failed. Check server logs.');
    }

  } catch (err) {
    _pollErrors++;
    // Transient network error (ERR_NETWORK_CHANGED, etc.) — keep polling silently
    if (_pollErrors < _MAX_POLL_ERRORS) {
      console.warn(`Poll error (${_pollErrors}/${_MAX_POLL_ERRORS}): ${err.message} — retrying…`);
      return;
    }
    // Too many consecutive failures — give up
    clearInterval(pollTimer);
    hide(statusSec);
    searchBtn.disabled = false;
    showError(`Lost connection to server after ${_pollErrors} attempts. Refresh and try again.`);
  }
}

// ── Render results ─────────────────────────────────────────────────────────
function renderResults(data) {
  // Entity type tag
  metaEntityType.textContent = data.entity_type;

  // Summary
  const { metadata: m } = data;
  metaSummary.textContent =
    `${data.rows.length} entities · ${m.pages_scraped} pages scraped · ${m.urls_considered} URLs considered`;

  // Angles
  anglesList.innerHTML = '';
  (m.search_angles || []).forEach(angle => {
    const li = document.createElement('li');
    li.textContent = angle;
    anglesList.appendChild(li);
  });

  // Run metadata
  const gapNote = m.gap_fill_used ? ' · gap-fill applied' : '';
  runMeta.textContent =
    `${m.entities_extracted} entities extracted · ${m.entities_after_merge} after merge ` +
    `· ${m.duration_seconds}s${gapNote}`;

  // Export buttons
  exportJson.onclick = () => window.open(`/api/export/json?query_id=${data.query_id}`);
  exportCsv.onclick  = () => window.open(`/api/export/csv?query_id=${data.query_id}`);

  // Build table header
  const cols = data.columns;
  tableHead.innerHTML = '';
  const tr = document.createElement('tr');
  const thIdx = document.createElement('th');
  thIdx.textContent = '#';
  thIdx.className = 'col-idx';
  tr.appendChild(thIdx);
  cols.forEach(col => {
    const th = document.createElement('th');
    th.textContent = humanColumn(col);
    tr.appendChild(th);
  });
  tableHead.appendChild(tr);

  // Build table body
  tableBody.innerHTML = '';
  data.rows.forEach((row, rowIdx) => {
    const tr = document.createElement('tr');

    // Index cell
    const tdIdx = document.createElement('td');
    tdIdx.className = 'col-idx';
    tdIdx.textContent = rowIdx + 1;
    tr.appendChild(tdIdx);

    // Data cells
    cols.forEach(col => {
      const td = document.createElement('td');
      const cell = row.cells[col];

      if (cell && cell.value) {
        td.className = 'cell-filled';
        td.title = 'Click to view evidence';

        const span = document.createElement('span');
        span.className = 'cell-value';
        span.textContent = cell.value;

        const dot = document.createElement('span');
        dot.className = `conf-dot ${confClass(cell.confidence)}`;
        dot.title = `Confidence: ${Math.round(cell.confidence * 100)}%`;

        td.appendChild(span);
        td.appendChild(dot);

        td.addEventListener('click', () => openModal(col, cell));
      } else {
        td.className = 'cell-empty';
        td.textContent = '—';
      }

      tr.appendChild(td);
    });

    tableBody.appendChild(tr);
  });

  show(resultsSec);
  resultsSec.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

// ── Modal ──────────────────────────────────────────────────────────────────
function openModal(col, cell) {
  modalColLabel.textContent  = humanColumn(col);
  modalValue.textContent     = cell.value;
  modalConf.textContent      = `Confidence: ${Math.round(cell.confidence * 100)}%`;
  modalSnippet.textContent   = cell.evidence_snippet || '(no snippet)';
  modalSourceUrl.textContent = cell.source_url || '';
  modalSourceUrl.href        = cell.source_url || '#';
  modalSourceTitle.textContent = cell.source_title ? `· ${cell.source_title}` : '';
  show(modalOverlay);
}

function closeModal() {
  hide(modalOverlay);
}

modalClose.addEventListener('click', closeModal);
modalOverlay.addEventListener('click', (e) => {
  if (e.target === modalOverlay) closeModal();
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') closeModal();
});
