/* AgenticSearch frontend — vanilla JS, no dependencies */

"use strict";

// ── State ──────────────────────────────────────────────────────────────────
let currentJobId = null;
let pollTimer = null;
let pipelineStartTime = null;
let elapsedTimer = null;
let currentQuery = "";

// ── DOM refs ───────────────────────────────────────────────────────────────
const form = document.getElementById("search-form");
const queryInput = document.getElementById("query-input");
const searchBtn = document.getElementById("search-btn");
const tracker = document.getElementById("pipeline-tracker");
const phaseLabel = document.getElementById("phase-label");
const elapsedLabel = document.getElementById("elapsed-label");
const errorBanner = document.getElementById("error-banner");
const errorMsg = document.getElementById("error-msg");
const noResultsBanner = document.getElementById("no-results-banner");
const noResultsQuery = document.getElementById("no-results-query");
const pipelineQuery = document.getElementById("pipeline-query");
const resultsSec = document.getElementById("results-section");
const emptyState = document.getElementById("empty-state");
const tableHead = document.getElementById("table-head");
const tableBody = document.getElementById("table-body");
const exportJson = document.getElementById("btn-export-json");
const exportCsv = document.getElementById("btn-export-csv");

// Summary strip
const sumQuery = document.getElementById("sum-query");
const sumEntityType = document.getElementById("sum-entity-type");
const sumRows = document.getElementById("sum-rows");
const sumSources = document.getElementById("sum-sources");
const sumScraped = document.getElementById("sum-scraped");
const sumExtracted = document.getElementById("sum-extracted");
const sumGapfill = document.getElementById("sum-gapfill");
const sumDuration = document.getElementById("sum-duration");

// Panels
const retrievalBody = document.getElementById("retrieval-plan-body");
const qualityBody = document.getElementById("quality-controls-body");
const statsBody = document.getElementById("run-stats-body");

// Modal
const modalOverlay = document.getElementById("modal-overlay");
const modalClose = document.getElementById("modal-close");
const modalColLabel = document.getElementById("modal-col-label");
const modalValue = document.getElementById("modal-value");
const modalConf = document.getElementById("modal-conf");
const modalSnippet = document.getElementById("modal-snippet");
const modalSourceUrl = document.getElementById("modal-source-url");
const modalSourceTitle = document.getElementById("modal-source-title");
const modalSourceBadge = document.getElementById("modal-source-badge");
const modalFlags = document.getElementById("modal-flags");

// ── Helpers ────────────────────────────────────────────────────────────────
function show(el) {
  el.classList.remove("hidden");
}
function hide(el) {
  el.classList.add("hidden");
}

function showError(msg) {
  errorMsg.textContent = msg;
  show(errorBanner);
}
function clearError() {
  hide(errorBanner);
  hide(noResultsBanner);
}

function humanColumn(col) {
  return col.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function confClass(conf) {
  if (conf >= 0.8) return "conf-high";
  if (conf >= 0.5) return "conf-medium";
  return "conf-low";
}

function confLabel(conf) {
  if (conf >= 0.8) return "high";
  if (conf >= 0.5) return "medium";
  return "low";
}

// ── Source type classification (simplified frontend mirror of source_quality.py) ─
const _EDITORIAL = new Set([
  "wired.com",
  "techcrunch.com",
  "arstechnica.com",
  "acm.org",
  "arxiv.org",
  "bbc.com",
  "bloomberg.com",
  "cnet.com",
  "economist.com",
  "forbes.com",
  "ft.com",
  "ieee.org",
  "nature.com",
  "newyorker.com",
  "nytimes.com",
  "reuters.com",
  "sciencemag.org",
  "springer.com",
  "theatlantic.com",
  "theguardian.com",
  "theverge.com",
  "vox.com",
  "zdnet.com",
]);
const _DIRECTORY = new Set([
  "booking.com",
  "capterra.com",
  "crunchbase.com",
  "g2.com",
  "glassdoor.com",
  "opentable.com",
  "producthunt.com",
  "tripadvisor.com",
  "trustpilot.com",
  "yelp.com",
]);
const _MARKETPLACE = new Set([
  "airbnb.com",
  "amazon.com",
  "doordash.com",
  "ebay.com",
  "etsy.com",
  "expedia.com",
  "grubhub.com",
  "hotels.com",
  "kayak.com",
  "postmates.com",
  "ubereats.com",
]);

function extractDomain(url) {
  try {
    const h = new URL(url).hostname.toLowerCase();
    return h.startsWith("www.") ? h.slice(4) : h;
  } catch {
    return "";
  }
}

function classifySource(url) {
  const d = extractDomain(url);
  if (_EDITORIAL.has(d)) return "editorial";
  if (_DIRECTORY.has(d)) return "directory";
  if (_MARKETPLACE.has(d)) return "marketplace";
  return "unknown";
}

function classifySourceForRow(cells, entityWebsite) {
  const types = new Set();
  for (const cell of Object.values(cells)) {
    if (!cell || !cell.source_url) continue;
    const d = extractDomain(cell.source_url);
    if (entityWebsite && extractDomain(entityWebsite) === d) {
      types.add("official");
    } else {
      types.add(classifySource(cell.source_url));
    }
  }
  return types;
}

// ── Pipeline phase tracking ────────────────────────────────────────────────
const STAGES = [
  "planning",
  "searching",
  "scraping",
  "reranking",
  "extracting",
  "merging",
  "gap_filling",
  "verifying",
];

const PHASE_LABELS = {
  queued: "Queued…",
  pending: "Starting pipeline…",
  planning: "Planning schema…",
  searching: "Searching the web…",
  scraping: "Scraping pages…",
  reranking: "Reranking pages…",
  extracting: "Discovering candidates…",
  merging: "Merging & deduplicating…",
  gap_filling: "Gap-fill enrichment…",
  verifying: "Verifying quality…",
  done: "Done!",
};

function updatePipelineTracker(phase) {
  const stageEls = tracker.querySelectorAll(".stage");
  const currentIdx = STAGES.indexOf(phase);

  stageEls.forEach((el) => {
    const stage = el.dataset.stage;
    const idx = STAGES.indexOf(stage);
    el.classList.remove("stage--done", "stage--active", "stage--pending");
    if (idx < currentIdx) {
      el.classList.add("stage--done");
    } else if (idx === currentIdx) {
      el.classList.add("stage--active");
    } else {
      el.classList.add("stage--pending");
    }
  });

  phaseLabel.textContent = PHASE_LABELS[phase] || phase || "Working…";
}

function startElapsedTimer() {
  pipelineStartTime = Date.now();
  elapsedLabel.textContent = "0s";
  elapsedTimer = setInterval(() => {
    const s = Math.round((Date.now() - pipelineStartTime) / 1000);
    elapsedLabel.textContent = `${s}s`;
  }, 1000);
}

function stopElapsedTimer() {
  if (elapsedTimer) {
    clearInterval(elapsedTimer);
    elapsedTimer = null;
  }
}

// ── Form submit ────────────────────────────────────────────────────────────
form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const query = queryInput.value.trim();
  if (!query) return;

  currentQuery = query;

  if (pollTimer) clearInterval(pollTimer);
  stopElapsedTimer();
  currentJobId = null;

  clearError();
  hide(resultsSec);
  hide(emptyState);
  hide(noResultsBanner);
  show(tracker);
  pipelineQuery.textContent = `\u201c${query}\u201d`;
  searchBtn.disabled = true;
  updatePipelineTracker("pending");
  startElapsedTimer();

  try {
    const res = await fetch("/api/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
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
    hide(tracker);
    stopElapsedTimer();
    searchBtn.disabled = false;
    showError(err.message);
  }
});

// ── Example chips ──────────────────────────────────────────────────────────
document.querySelectorAll(".example-chip").forEach((btn) => {
  btn.addEventListener("click", () => {
    queryInput.value = btn.dataset.q;
    form.dispatchEvent(new Event("submit"));
  });
});

// ── Polling ────────────────────────────────────────────────────────────────
let _pollErrors = 0;
const _MAX_POLL_ERRORS = 8;

function startPolling() {
  _pollErrors = 0;
  pollTimer = setInterval(pollJob, 2000);
  pollJob();
}

async function pollJob() {
  if (!currentJobId) return;

  try {
    const res = await fetch(`/api/search/${currentJobId}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const job = await res.json();

    _pollErrors = 0;
    updatePipelineTracker(job.phase || job.status);

    if (job.status === "done") {
      clearInterval(pollTimer);
      stopElapsedTimer();
      hide(tracker);
      searchBtn.disabled = false;
      if (job.result && job.result.rows && job.result.rows.length > 0) {
        renderResults(job.result);
      } else {
        noResultsQuery.textContent = currentQuery
          ? `Query: \u201c${currentQuery}\u201d`
          : "";
        show(noResultsBanner);
      }
    } else if (job.status === "failed") {
      clearInterval(pollTimer);
      stopElapsedTimer();
      hide(tracker);
      searchBtn.disabled = false;
      showError(job.error || "Pipeline failed. Check server logs.");
    }
  } catch (err) {
    _pollErrors++;
    if (_pollErrors < _MAX_POLL_ERRORS) {
      console.warn(
        `Poll error (${_pollErrors}/${_MAX_POLL_ERRORS}): ${err.message}`,
      );
      return;
    }
    clearInterval(pollTimer);
    stopElapsedTimer();
    hide(tracker);
    searchBtn.disabled = false;
    showError(
      `Lost connection to server after ${_pollErrors} attempts. Refresh and try again.`,
    );
  }
}

// ── Render results ─────────────────────────────────────────────────────────
function renderResults(data) {
  const m = data.metadata;
  const normalizedChanged =
    m.normalized_query && m.normalized_query !== (m.original_query || currentQuery);

  // ── Summary strip ──
  sumQuery.textContent = normalizedChanged
    ? `${currentQuery} → ${m.normalized_query}`
    : currentQuery;
  sumEntityType.textContent = data.entity_type;
  sumRows.textContent = data.rows.length;
  sumSources.textContent = m.urls_considered;
  sumScraped.textContent = m.pages_scraped;
  sumExtracted.textContent = m.pages_after_rerank || m.pages_scraped;
  sumGapfill.textContent = m.gap_fill_used ? "gap-fill: yes" : "gap-fill: no";
  sumDuration.textContent = `\u23f1 ${m.duration_seconds}s`;

  // ── Retrieval plan panel ──
  renderRetrievalPlan(data, m);

  // ── Quality controls panel ──
  renderQualityControls(m);

  // ── Run stats panel ──
  renderRunStats(m);

  // Export buttons
  exportJson.onclick = () =>
    window.open(`/api/export/json?query_id=${data.query_id}`);
  exportCsv.onclick = () =>
    window.open(`/api/export/csv?query_id=${data.query_id}`);

  // ── Table ──
  renderTable(data);

  show(resultsSec);
  resultsSec.scrollIntoView({ behavior: "smooth", block: "start" });
}

// ── Retrieval plan panel ───────────────────────────────────────────────────
function renderRetrievalPlan(data, m) {
  let html = "";
  if (m.query_family) {
    html += `<div class="plan-row"><span class="plan-label">Query family:</span> <span class="plan-value">${esc(m.query_family)}</span></div>`;
  }
  if (m.normalized_query && m.normalized_query !== (m.original_query || currentQuery)) {
    html += `<div class="plan-row"><span class="plan-label">Normalized query:</span> <span class="plan-value">${esc(m.normalized_query)}</span></div>`;
  }
  html += `<div class="plan-row"><span class="plan-label">Entity type:</span> <span class="plan-value">${esc(data.entity_type)}</span></div>`;
  html += `<div class="plan-row"><span class="plan-label">Columns:</span> <span class="plan-value">${data.columns.map((c) => esc(humanColumn(c))).join(", ")}</span></div>`;

  if (m.facets && m.facets.length > 0) {
    html +=
      '<div class="plan-facets"><span class="plan-label">Search facets:</span>';
    html += '<ul class="facet-list">';
    for (const f of m.facets) {
      html += '<li class="facet-item">';
      html += `<span class="facet-type">${esc(f.type)}</span> `;
      html += `<span class="facet-query">${esc(f.query)}</span>`;
      if (f.expected_fill_columns && f.expected_fill_columns.length > 0) {
        html += `<span class="facet-cols"> → ${f.expected_fill_columns.map((c) => esc(humanColumn(c))).join(", ")}</span>`;
      }
      html += "</li>";
    }
    html += "</ul></div>";
  } else if (m.search_angles && m.search_angles.length > 0) {
    html +=
      '<div class="plan-facets"><span class="plan-label">Search angles:</span>';
    html += '<ul class="facet-list">';
    for (const a of m.search_angles) {
      html += `<li class="facet-item">${esc(a)}</li>`;
    }
    html += "</ul></div>";
  }

  if (m.rerank_scorer) {
    html += '<div class="plan-rerank">';
    html += `<span class="plan-label">Reranking:</span> `;
    html += `${m.pages_scraped} pages → ${m.pages_after_rerank} selected (${esc(m.rerank_scorer)})`;
    html += "</div>";
  }

  retrievalBody.innerHTML = html;
}

// ── Quality controls panel ────────────────────────────────────────────────
function renderQualityControls(m) {
  const items = [];

  if (m.normalized_query) {
    items.push({
      icon: "↺",
      text:
        m.normalized_query !== (m.original_query || currentQuery)
          ? `Query normalization applied before retrieval: ${m.normalized_query}`
          : "Query normalization checked before retrieval",
    });
  }
  if (m.query_family) {
    items.push({
      icon: "⌘",
      text: `Constrained schema selected from query family: ${m.query_family}`,
    });
  }
  if (m.rerank_scorer) {
    items.push({
      icon: "⚡",
      text: `Cross-encoder reranking: ${m.pages_scraped} → ${m.pages_after_rerank} pages`,
    });
  }
  items.push({
    icon: "🔎",
    text: "Candidate discovery runs before attribute filling to preserve recall",
  });
  if ((m.pipeline_counts?.pages_routed_deterministic || 0) > 0) {
    items.push({
      icon: "⚙",
      text: `Deterministic parsing handled ${m.pipeline_counts.pages_routed_deterministic} page(s) before LLM fallback`,
    });
  }
  if (m.entities_extracted && m.entities_after_merge) {
    const deduped = m.entities_extracted - m.entities_after_merge;
    if (deduped > 0) {
      items.push({
        icon: "🔗",
        text: `Entity deduplication: ${deduped} duplicates merged (${m.entities_extracted} → ${m.entities_after_merge})`,
      });
    }
  }
  items.push({
    icon: "🌐",
    text:
      (m.pipeline_counts?.official_sites_resolved || 0) > 0
        ? `Official-site resolution matched ${m.pipeline_counts.official_sites_resolved} candidate rows`
        : "Official-site resolution runs when canonical domains can be inferred",
  });
  items.push({ icon: "✓", text: "Cell-level entity-alignment verification" });
  items.push({
    icon: "✓",
    text: "Field validation (URL, phone, rating normalization)",
  });
  items.push({
    icon: "✓",
    text: "Evidence-regime-aware source scoring (official / editorial / directory / marketplace)",
  });
  items.push({
    icon: "✓",
    text: "Source-diversity penalty for single-domain rows",
  });
  if (m.gap_fill_used) {
    items.push({ icon: "↻", text: "Gap-fill enrichment run on sparse rows" });
  }
  if ((m.pipeline_counts?.pages_rendered_with_js || 0) > 0) {
    items.push({
      icon: "↯",
      text: `Selective JS fallback rendered ${m.pipeline_counts.pages_rendered_with_js} page(s)`,
    });
  }
  items.push({
    icon: "✓",
    text: "Rank first, then late filtering for obvious junk and weak marketplace rows",
  });

  let html = '<ul class="qc-list">';
  for (const it of items) {
    html += `<li><span class="qc-icon">${it.icon}</span> ${esc(it.text)}</li>`;
  }
  html += "</ul>";
  qualityBody.innerHTML = html;
}

// ── Run stats panel ───────────────────────────────────────────────────────
function renderRunStats(m) {
  const rows = [
    ["Total duration", `${m.duration_seconds}s`],
    ["Execution mode", "Async background job (non-blocking)"],
    ["Query family", m.query_family || "generic_entity_list"],
    ["URLs considered", m.urls_considered],
    ["Pages scraped", m.pages_scraped],
    ["Pages sent to extraction", m.pages_after_rerank || m.pages_scraped],
    ["Entities extracted", m.entities_extracted],
    ["Entities after merge", m.entities_after_merge],
    ["Candidate rows", m.pipeline_counts?.candidate_rows ?? m.entities_after_merge],
    ["Official sites resolved", m.pipeline_counts?.official_sites_resolved ?? 0],
    ["Deterministic pages", m.pipeline_counts?.pages_routed_deterministic ?? 0],
    ["Hybrid pages", m.pipeline_counts?.pages_routed_hybrid ?? 0],
    ["LLM-routed pages", m.pipeline_counts?.pages_routed_llm ?? 0],
    ["JS-rendered pages", m.pipeline_counts?.pages_rendered_with_js ?? 0],
    ["Gap-fill", m.gap_fill_used ? "Yes" : "No"],
  ];
  if (m.normalized_query && m.normalized_query !== (m.original_query || currentQuery)) {
    rows.splice(2, 0, ["Normalized query", m.normalized_query]);
  }
  if (m.rerank_scorer) {
    rows.push(["Rerank scorer", m.rerank_scorer]);
  }

  let html = '<table class="stats-table">';
  for (const [label, val] of rows) {
    html += `<tr><td class="stats-label">${esc(label)}</td><td class="stats-value">${esc(String(val))}</td></tr>`;
  }
  html += "</table>";
  statsBody.innerHTML = html;
}

// ── Column type classifier for smart sizing ──────────────────────────────
// ── Column priority map ────────────────────────────────────────────────────
const COL_PRIORITY = {
  // Highest — always visible, gets most space
  name: 0,
  entity_name: 0,
  company: 0,
  title: 0,
  // High — important structured fields
  website: 1,
  address: 1,
  headquarters: 1,
  location: 1,
  focus_area: 1,
  website_or_repo: 1,
  stage_or_status: 1,
  rating: 1,
  offering: 1,
  type: 1,
  // Medium — useful but narrower
  price_range: 2,
  neighborhood: 2,
  category: 2,
  investors: 2,
  phone_number: 2,
  phone: 2,
  founded: 2,
  year_founded: 2,
  employees: 2,
  email: 2,
  ceo: 2,
  founder: 2,
  // Low — long text fields that can compress
  description: 3,
  summary: 3,
  overview: 3,
  bio: 3,
  notable_claim: 3,
  notes: 3,
  details: 3,
  about: 3,
};

function colPriority(col) {
  const c = col.toLowerCase();
  if (COL_PRIORITY[c] !== undefined) return COL_PRIORITY[c];
  if (c.endsWith("_url") || c === "url" || c === "homepage" || c === "link")
    return 1;
  if (c.includes("description") || c.includes("summary") || c.includes("note"))
    return 3;
  return 2;
}

function colPriorityClass(col) {
  const p = colPriority(col);
  return (
    ["col-pri-highest", "col-pri-high", "col-pri-medium", "col-pri-low"][p] ||
    "col-pri-medium"
  );
}

function isUrlColumn(col) {
  const c = col.toLowerCase();
  return (
    c === "url" ||
    c === "website" ||
    c === "homepage" ||
    c === "link" ||
    c.endsWith("_url")
  );
}

function isLongTextColumn(col) {
  return colPriority(col) === 3;
}

function sortColumnsByPriority(cols) {
  return [...cols].sort((a, b) => colPriority(a) - colPriority(b));
}

function truncateUrl(url, maxLen) {
  try {
    const u = new URL(url);
    const display = u.hostname.replace(/^www\./, "") + u.pathname;
    return display.length > maxLen
      ? display.slice(0, maxLen - 1) + "\u2026"
      : display;
  } catch {
    return url.length > maxLen ? url.slice(0, maxLen - 1) + "\u2026" : url;
  }
}

// View mode state
let _viewCompact = false;

function toggleViewMode() {
  _viewCompact = !_viewCompact;
  const table = document.getElementById("results-table");
  const btn = document.getElementById("btn-view-toggle");
  if (_viewCompact) {
    table.classList.add("table-compact");
    btn.textContent = "\u229e Full view";
  } else {
    table.classList.remove("table-compact");
    btn.textContent = "\u229f Compact";
  }
}

// ── Table rendering ───────────────────────────────────────────────────────
function renderTable(data) {
  const rawCols = data.columns;
  const cols = sortColumnsByPriority(rawCols);
  const isWide = cols.length > 6;
  const table = document.getElementById("results-table");

  // Mark wide schemas
  table.classList.toggle("wide-schema", isWide);
  table.classList.remove("table-compact");
  _viewCompact = false;
  const toggleBtn = document.getElementById("btn-view-toggle");
  if (toggleBtn) {
    toggleBtn.textContent = "\u229f Compact";
    toggleBtn.style.display = cols.length > 4 ? "" : "none";
  }

  // Header
  tableHead.innerHTML = "";
  const headTr = document.createElement("tr");
  const thIdx = document.createElement("th");
  thIdx.textContent = "#";
  thIdx.className = "col-idx";
  headTr.appendChild(thIdx);
  cols.forEach((col) => {
    const th = document.createElement("th");
    th.textContent = humanColumn(col);
    th.className = colPriorityClass(col);
    th.dataset.col = col;
    headTr.appendChild(th);
  });
  const thTrust = document.createElement("th");
  thTrust.textContent = "Trust";
  thTrust.className = "col-trust";
  headTr.appendChild(thTrust);
  tableHead.appendChild(headTr);

  // Body
  tableBody.innerHTML = "";
  data.rows.forEach((row, rowIdx) => {
    const tr = document.createElement("tr");

    // Index
    const tdIdx = document.createElement("td");
    tdIdx.className = "col-idx";
    tdIdx.textContent = rowIdx + 1;
    tr.appendChild(tdIdx);

    // Data cells
    cols.forEach((col) => {
      const td = document.createElement("td");
      const cell = row.cells[col];
      const priClass = colPriorityClass(col);
      td.dataset.col = col;

      if (cell && cell.value) {
        td.className = `cell-filled ${priClass}`;
        td.title = "Click to view evidence";

        const span = document.createElement("span");

        if (isUrlColumn(col)) {
          span.className = "cell-value cell-url";
          span.textContent = truncateUrl(cell.value, 40);
          td.title = cell.value;
        } else if (isLongTextColumn(col)) {
          span.className = "cell-value cell-text-clamp";
          span.textContent = cell.value;
        } else {
          span.className = "cell-value";
          span.textContent = cell.value;
        }

        const dot = document.createElement("span");
        dot.className = `conf-dot ${confClass(cell.confidence)}`;
        dot.title = `Confidence: ${Math.round(cell.confidence * 100)}%`;

        td.appendChild(span);
        td.appendChild(dot);

        td.addEventListener("click", () => openModal(col, cell, row));
      } else {
        td.className = `cell-empty ${priClass}`;
        td.textContent = "—";
      }

      tr.appendChild(td);
    });

    // Trust badges column
    const tdTrust = document.createElement("td");
    tdTrust.className = "col-trust";
    tdTrust.innerHTML = buildTrustBadges(row);
    tr.appendChild(tdTrust);

    tableBody.appendChild(tr);
  });
}

// ── Row trust badges ──────────────────────────────────────────────────────
function buildTrustBadges(row) {
  const badges = [];

  // Sources count
  badges.push(
    `<span class="badge badge--neutral">${row.sources_count} src</span>`,
  );

  // Confidence
  const confLvl = confLabel(row.aggregate_confidence);
  const confCls =
    confLvl === "high"
      ? "badge--green"
      : confLvl === "medium"
        ? "badge--yellow"
        : "badge--red";
  badges.push(`<span class="badge ${confCls}">${confLvl} conf</span>`);

  // Source types
  const entityWebsite =
    row.cells.website?.value ||
    row.cells.url?.value ||
    row.cells.homepage?.value ||
    "";
  const types = classifySourceForRow(row.cells, entityWebsite);
  if (types.has("official"))
    badges.push('<span class="badge badge--green">official</span>');
  if (types.has("editorial"))
    badges.push('<span class="badge badge--blue">editorial</span>');
  if (types.has("directory"))
    badges.push('<span class="badge badge--neutral">directory</span>');
  if (types.has("marketplace"))
    badges.push('<span class="badge badge--yellow">marketplace</span>');

  // Source diversity
  const domains = new Set();
  for (const cell of Object.values(row.cells)) {
    if (cell && cell.source_url) domains.add(extractDomain(cell.source_url));
  }
  if (domains.size <= 1 && Object.keys(row.cells).length > 1) {
    badges.push('<span class="badge badge--red">single-source</span>');
  }

  return badges.join(" ");
}

// ── Modal ──────────────────────────────────────────────────────────────────
function openModal(col, cell, row) {
  modalColLabel.textContent = humanColumn(col);
  modalValue.textContent = cell.value;
  modalConf.textContent = `${Math.round(cell.confidence * 100)}%`;
  modalConf.className = `modal__conf ${confClass(cell.confidence)}`;
  modalSnippet.textContent = cell.evidence_snippet || "(no snippet)";
  modalSourceUrl.textContent = cell.source_url || "";
  modalSourceUrl.href = cell.source_url || "#";
  modalSourceTitle.textContent = cell.source_title ? cell.source_title : "";

  // Source type badge
  const entityWebsite =
    row?.cells?.website?.value || row?.cells?.url?.value || "";
  const srcDomain = extractDomain(cell.source_url || "");
  let srcType = classifySource(cell.source_url || "");
  if (entityWebsite && extractDomain(entityWebsite) === srcDomain) {
    srcType = "official";
  }
  const badgeClass =
    {
      official: "badge--green",
      editorial: "badge--blue",
      directory: "badge--neutral",
      marketplace: "badge--yellow",
      unknown: "badge--dim",
    }[srcType] || "badge--dim";
  modalSourceBadge.textContent = srcType;
  modalSourceBadge.className = `modal__source-badge badge ${badgeClass}`;

  // Flags
  const flags = [];
  if (cell.confidence < 0.5) flags.push("low-confidence");
  if (row && row.sources_count <= 1) flags.push("single-source");
  modalFlags.innerHTML = flags.length
    ? flags.map((f) => `<span class="flag">${esc(f)}</span>`).join(" ")
    : "";

  show(modalOverlay);
}

function closeModal() {
  hide(modalOverlay);
}

modalClose.addEventListener("click", closeModal);
modalOverlay.addEventListener("click", (e) => {
  if (e.target === modalOverlay) closeModal();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeModal();
});

// ── HTML escaper ──────────────────────────────────────────────────────────
function esc(str) {
  const d = document.createElement("div");
  d.textContent = str;
  return d.innerHTML;
}
