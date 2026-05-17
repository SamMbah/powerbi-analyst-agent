/**
 * Power BI Analyst Agent — frontend
 *
 * Key flow:
 *  1. On load → GET /api/workspaces → build sidebar tree
 *  2. Click workspace → GET /api/workspaces/:id/datasets → expand tree
 *  3. Click dataset  → GET /api/.../schema → show in topbar, enable question box
 *  4. Click Analyse  → open EventSource to /api/analyse?... → render events live
 *
 * EventSource (SSE) is the browser's native API for receiving a stream of
 * server-sent events. Each event is a JSON object from our FastAPI backend.
 */

// ── State ──────────────────────────────────────────────────────────────────────
const state = {
  workspaces:     [],
  selectedWs:     null,   // { id, name }
  selectedDs:     null,   // { id, name }
  schema:         null,
  stepCount:      0,
  currentSource:  null,   // active EventSource
  mode:           "analyse",  // "analyse" | "dashboard"
  // Dashboard-specific
  dashboardSpecs: [],     // per-chart: { dax, chart_type, x_column, y_column, base_table, title, plotlyJson, plotData }
  kpiSpecs:       [],     // per-KPI:   { dax, format, title }
  activeFilters:  [],     // [{ table, col, val, label }]
};

// ── DOM refs ───────────────────────────────────────────────────────────────────
const authBtn       = document.getElementById("auth-btn");
const treeContainer = document.getElementById("tree-container");
const topbar        = document.getElementById("topbar");
const content       = document.getElementById("content");
const questionEl    = document.getElementById("question");
const runBtn        = document.getElementById("run-btn");
const statusPill    = document.getElementById("status-pill");


// ── Auth ───────────────────────────────────────────────────────────────────────
authBtn.addEventListener("click", async () => {
  authBtn.textContent = "⏳  Starting...";
  authBtn.disabled = true;

  // Stage 1 — get the login code immediately (fast)
  const res  = await fetch("/api/auth/start", { method: "POST" });
  const data = await res.json();

  if (data.status !== "ok") {
    authBtn.textContent = "✗  Failed — retry";
    authBtn.disabled = false;
    return;
  }

  // Show the login instructions in the UI
  showEmptyState(
    `<strong>Sign in to Power BI</strong><br><br>
     Go to <a href="${data.verification_uri}" target="_blank" style="color:var(--accent)">${data.verification_uri}</a>
     <br>and enter code: <code style="background:var(--surface-2);padding:4px 10px;border-radius:6px;font-size:16px;letter-spacing:2px">${data.user_code}</code>
     <br><br><span style="color:var(--text-muted);font-size:12px">Waiting for login...</span>`,
    false
  );
  authBtn.textContent = "⏳  Waiting for login...";

  // Stage 2 — SSE stream that fires when login completes
  const pollEs = new EventSource("/api/auth/poll");
  pollEs.onmessage = (e) => {
    const event = JSON.parse(e.data);
    if (event.type === "done") {
      pollEs.close();
      authBtn.textContent = "✓  Connected";
      authBtn.classList.add("connected");
      document.getElementById("upload-btn").disabled = false;
      loadWorkspaces();
    } else if (event.type === "error") {
      pollEs.close();
      authBtn.textContent = "✗  Failed — retry";
      authBtn.disabled = false;
      showEmptyState(`Auth error: ${event.message}`, false);
    }
  };
  pollEs.onerror = () => {
    pollEs.close();
    authBtn.textContent = "✗  Failed — retry";
    authBtn.disabled = false;
  };
});


// ── Workspace loading ──────────────────────────────────────────────────────────
async function loadWorkspaces() {
  treeContainer.innerHTML = `<div class="tree-item"><div class="spinner"></div> Loading...</div>`;
  const res  = await fetch("/api/workspaces");
  const data = await res.json();

  if (data.error) {
    treeContainer.innerHTML = `<div class="tree-item" style="color:var(--red)">${data.error}</div>`;
    return;
  }

  state.workspaces = data.workspaces;
  renderTree();
  showEmptyState("Select a workspace and dataset from the sidebar to get started", false);
}

function renderTree() {
  treeContainer.innerHTML = "";

  const label = document.createElement("div");
  label.className = "tree-label";
  label.textContent = "Workspaces";
  treeContainer.appendChild(label);

  state.workspaces.forEach(ws => {
    const item = document.createElement("div");
    item.className = "tree-item";
    item.dataset.wsId = ws.id;
    item.innerHTML = `<span class="icon">◫</span> ${ws.name}`;
    item.addEventListener("click", () => toggleWorkspace(ws, item));
    treeContainer.appendChild(item);
  });
}

async function toggleWorkspace(ws, el) {
  // Remove any existing dataset lists
  document.querySelectorAll(".ds-child").forEach(n => n.remove());
  document.querySelectorAll(".tree-item").forEach(n => n.classList.remove("active"));
  el.classList.add("active");

  const loadingEl = document.createElement("div");
  loadingEl.className = "tree-item indent ds-child";
  loadingEl.innerHTML = `<div class="spinner"></div> Loading datasets...`;
  el.insertAdjacentElement("afterend", loadingEl);

  const res  = await fetch(`/api/workspaces/${ws.id}/datasets`);
  const data = await res.json();
  loadingEl.remove();

  if (data.error || !data.datasets.length) {
    const errEl = document.createElement("div");
    errEl.className = "tree-item indent ds-child";
    errEl.textContent = data.error || "No datasets found";
    el.insertAdjacentElement("afterend", errEl);
    return;
  }

  // Insert datasets in reverse so they appear in order after el
  [...data.datasets].reverse().forEach(ds => {
    const dsEl = document.createElement("div");
    dsEl.className = "tree-item indent ds-child";
    dsEl.dataset.dsId = ds.id;
    dsEl.innerHTML = `<span class="icon">◈</span> ${ds.name}`;
    dsEl.addEventListener("click", (e) => {
      e.stopPropagation();
      selectDataset(ws, ds, dsEl);
    });
    el.insertAdjacentElement("afterend", dsEl);
  });
}

async function selectDataset(ws, ds, el) {
  document.querySelectorAll(".tree-item").forEach(n => n.classList.remove("active"));
  el.classList.add("active");

  state.selectedWs = ws;
  state.selectedDs = ds;

  // Update topbar
  topbar.innerHTML = `
    <div class="dataset-badge">
      <div class="dot"></div>
      <span class="topbar-muted">${ws.name}</span>
      <span class="topbar-muted">/</span>
      <span class="topbar-title">${ds.name}</span>
    </div>
    <div id="status-pill"></div>
  `;

  // Load schema
  showEmptyState("Reading schema...", true);
  const res  = await fetch(`/api/workspaces/${ws.id}/datasets/${ds.id}/schema`);
  const data = await res.json();

  if (data.error) {
    showEmptyState(`Schema error: ${data.error}`, false);
    return;
  }

  state.schema = data.schema;
  questionEl.disabled = false;
  runBtn.disabled = false;

  renderSchemaPanel(data.schema);
}


// ── Mode toggle ────────────────────────────────────────────────────────────────
function setMode(mode) {
  state.mode = mode;
  document.getElementById("tab-analyse").classList.toggle("active", mode === "analyse");
  document.getElementById("tab-dashboard").classList.toggle("active", mode === "dashboard");

  const label = document.getElementById("input-label");
  if (mode === "dashboard") {
    label.textContent = "Describe the dashboard you want";
    questionEl.placeholder = "e.g. Give me an executive overview of sales performance by region, product, and time";
    runBtn.textContent = "Build Dashboard";
  } else {
    label.textContent = "Ask a business question";
    questionEl.placeholder = "e.g. Which product category had the highest revenue last quarter? (Ctrl+Enter to run)";
    runBtn.textContent = "Analyse";
  }
}

// ── Analysis ───────────────────────────────────────────────────────────────────
runBtn.addEventListener("click", () => {
  if (state.mode === "dashboard") startDashboard();
  else startAnalysis();
});
questionEl.addEventListener("keydown", e => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
    if (state.mode === "dashboard") startDashboard();
    else startAnalysis();
  }
});

function startAnalysis() {
  const question = questionEl.value.trim();
  if (!question || !state.selectedDs) return;

  // Cancel any existing stream
  if (state.currentSource) state.currentSource.close();

  state.stepCount = 0;
  content.innerHTML = "";
  runBtn.disabled = true;
  setStatus("running", "Analysing...");

  const params = new URLSearchParams({
    workspace_id:   state.selectedWs.id,
    workspace_name: state.selectedWs.name,
    dataset_id:     state.selectedDs.id,
    dataset_name:   state.selectedDs.name,
    question,
  });

  const es = new EventSource(`/api/analyse?${params}`);
  state.currentSource = es;

  es.onmessage = (e) => {
    const event = JSON.parse(e.data);
    handleEvent(event);
  };

  es.onerror = () => {
    setStatus("error", "Connection error");
    es.close();
    runBtn.disabled = false;
  };
}

// ── Dashboard mode ─────────────────────────────────────────────────────────────
function startDashboard() {
  const request = questionEl.value.trim();
  if (!request || !state.selectedDs) return;

  if (state.currentSource) state.currentSource.close();

  content.innerHTML = "";
  state.dashboardSpecs = [];
  state.kpiSpecs       = [];
  state.activeFilters  = [];
  runBtn.disabled = true;
  setStatus("running", "Planning dashboard...");

  // Planning banner
  const banner = document.createElement("div");
  banner.id = "plan-banner";
  banner.className = "plan-banner";
  banner.innerHTML = `<div class="spinner"></div><span>Claude is planning your dashboard…</span>`;
  content.appendChild(banner);

  const params = new URLSearchParams({
    workspace_id:   state.selectedWs.id,
    workspace_name: state.selectedWs.name,
    dataset_id:     state.selectedDs.id,
    dataset_name:   state.selectedDs.name,
    request,
  });

  const es = new EventSource(`/api/dashboard?${params}`);
  state.currentSource = es;

  es.onmessage = (e) => {
    const event = JSON.parse(e.data);
    handleDashboardEvent(event);
  };

  es.onerror = () => {
    setStatus("error", "Connection error");
    es.close();
    runBtn.disabled = false;
  };
}

// Plotly config shared across all dashboard charts
const PLOTLY_CONFIG = {
  responsive: true,
  displaylogo: false,
  modeBarButtonsToRemove: ["select2d", "lasso2d", "autoScale2d"],
  toImageButtonOptions: { format: "png", scale: 2 },
};

function handleDashboardEvent(event) {
  switch (event.type) {

    case "status": {
      const bannerSpan = document.querySelector("#plan-banner span");
      if (bannerSpan) bannerSpan.textContent = event.message;
      break;
    }

    case "plan": {
      const banner = document.getElementById("plan-banner");
      if (banner) banner.remove();

      // Store KPI specs for later filter refreshes
      state.kpiSpecs = event.kpis || [];

      // KPI skeleton row
      if (event.kpis && event.kpis.length) {
        const kpiRow = document.createElement("div");
        kpiRow.className = "kpi-row";
        kpiRow.id = "kpi-row";
        event.kpis.forEach((k, i) => {
          const card = document.createElement("div");
          card.className = "kpi-card";
          card.id = `kpi-slot-${i}`;
          card.innerHTML = `
            <div class="kpi-card-label">${escapeHtml(k.title)}</div>
            <div class="kpi-card-value loading" id="kpi-val-${i}">—</div>
          `;
          kpiRow.appendChild(card);
        });
        content.appendChild(kpiRow);
      }

      // Filter chips row (hidden until a filter is applied)
      const chipsRow = document.createElement("div");
      chipsRow.id = "filter-chips-row";
      chipsRow.style.display = "none";
      content.appendChild(chipsRow);

      // Chart grid heading
      const heading = document.createElement("div");
      heading.style.cssText = "font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--text-muted);margin-bottom:4px;margin-top:8px";
      heading.textContent = `Charts · ${event.charts.length}`;
      content.appendChild(heading);

      const grid = document.createElement("div");
      grid.className = "dashboard-grid";
      grid.id = "dashboard-grid";
      content.appendChild(grid);

      event.charts.forEach((ch, i) => {
        const ph = document.createElement("div");
        ph.className = "chart-placeholder";
        ph.id = `chart-slot-${i}`;
        ph.innerHTML = `
          <div class="spinner"></div>
          <div class="ph-title">${escapeHtml(ch.title)}</div>
          <div class="ph-insight">${escapeHtml(ch.insight || "")}</div>
        `;
        grid.appendChild(ph);
      });

      setStatus("running", `Building ${event.charts.length} charts…`);
      scrollToBottom();
      break;
    }

    case "kpi": {
      const valEl = document.getElementById(`kpi-val-${event.index}`);
      if (!valEl) break;
      valEl.classList.remove("loading");
      valEl.textContent = event.value != null
        ? formatKpiValue(event.value, event.format)
        : "—";
      break;
    }

    case "chart": {
      const slot = document.getElementById(`chart-slot-${event.index}`);
      if (!slot) break;

      // Store spec for cross-filter refreshes and CSV export
      const fig        = JSON.parse(event.plotly);
      const traceType  = (fig.data[0] || {}).type || "";
      const isWide     = traceType === "scatter" || traceType === "area";
      const plotHeight = isWide ? 360 : 340;

      state.dashboardSpecs[event.index] = {
        dax:        event.dax,
        chart_type: event.chart_type,
        x_column:   event.x_column,
        y_column:   event.y_column,
        base_table: event.base_table,
        title:      event.title,
        drill_dax_template: event.drill_dax_template,
        drill_next: event.drill_next,
        plotlyJson: event.plotly,
        plotData:   fig.data,
      };

      const hasDrill = !!event.drill_dax_template;
      const classes  = ["chart-card", isWide ? "span-2" : ""].filter(Boolean).join(" ");

      const card = document.createElement("div");
      card.className = classes;
      card.dataset.index = event.index;
      card.innerHTML = `
        <div class="chart-card-header">
          <div class="chart-card-title">${escapeHtml(event.title)}</div>
          <div style="display:flex;align-items:center;gap:6px;flex-shrink:0">
            ${hasDrill ? `<div class="drill-hint">🔍 ${escapeHtml(event.drill_next || "")}</div>` : ""}
            <div class="card-actions">
              <button class="card-menu-btn" title="Options" onclick="toggleCardMenu(event,${event.index})">⋯</button>
              <div class="card-menu" id="card-menu-${event.index}">
                <button class="card-menu-item" onclick="refreshChart(${event.index});closeAllMenus()">
                  <span class="mi-icon">↺</span> Refresh
                </button>
                <button class="card-menu-item" onclick="expandChart(${event.index});closeAllMenus()">
                  <span class="mi-icon">⤢</span> Expand
                </button>
                <button class="card-menu-item" onclick="downloadCSV(${event.index});closeAllMenus()">
                  <span class="mi-icon">⬇</span> Download CSV
                </button>
                ${hasDrill ? `<button class="card-menu-item" onclick="drillChartMenu(${event.index});closeAllMenus()">
                  <span class="mi-icon">🔍</span> Drill into ${escapeHtml(event.drill_next||"")}
                </button>` : ""}
              </div>
            </div>
          </div>
        </div>
        <div class="chart-card-plot" id="plotly-${event.index}" style="height:${plotHeight}px">
          <div class="chart-loading" id="loading-${event.index}"><div class="spinner"></div></div>
        </div>
        ${event.insight ? `<div class="chart-card-insight">${escapeHtml(event.insight)}</div>` : ""}
      `;
      slot.replaceWith(card);

      const plotDiv = document.getElementById(`plotly-${event.index}`);
      requestAnimationFrame(() => {
        Plotly.newPlot(plotDiv, fig.data, fig.layout, PLOTLY_CONFIG);
        if (window.ResizeObserver) {
          new ResizeObserver(() => Plotly.Plots.resize(plotDiv)).observe(plotDiv);
        }
        // Cross-filter on click (all charts)
        plotDiv.on("plotly_click", (clickData) => {
          const pt  = clickData.points[0];
          const val = pt.label ?? pt.x ?? pt.y;
          if (val == null) return;
          addCrossFilter(event.x_column, event.base_table, String(val), event.index);
        });
      });

      scrollToBottom();
      break;
    }

    case "error_chart": {
      const slot = document.getElementById(`chart-slot-${event.index}`);
      if (!slot) break;
      const errCard = document.createElement("div");
      errCard.className = "chart-error";
      errCard.innerHTML = `
        <div class="err-title">✗ ${escapeHtml(event.title)}</div>
        <div class="err-msg">${escapeHtml(event.message)}</div>
      `;
      slot.replaceWith(errCard);
      break;
    }

    case "done": {
      setStatus("done", "Dashboard ready");
      runBtn.disabled = false;
      state.currentSource?.close();
      break;
    }

    case "error": {
      const banner = document.getElementById("plan-banner");
      if (banner) banner.remove();
      const el = document.createElement("div");
      el.className = "pill error";
      el.style.padding = "10px 14px";
      el.innerHTML = `✗ ${escapeHtml(event.message)}`;
      content.appendChild(el);
      setStatus("error", "Error");
      runBtn.disabled = false;
      state.currentSource?.close();
      break;
    }
  }
}

// ── Cross-filter ───────────────────────────────────────────────────────────────
function addCrossFilter(col, table, val, sourceIndex) {
  // Toggle: clicking the same filter removes it
  const existing = state.activeFilters.findIndex(f => f.col === col && f.val === val);
  if (existing >= 0) {
    state.activeFilters.splice(existing, 1);
  } else {
    state.activeFilters.push({ col, table, val, label: `${col}: ${val}`, sourceIndex });
  }
  renderFilterChips();
  applyFilters();
}

function renderFilterChips() {
  const row = document.getElementById("filter-chips-row");
  if (!row) return;

  if (state.activeFilters.length === 0) {
    row.style.display = "none";
    return;
  }

  row.style.display = "flex";
  row.className = "filter-chips-row";
  row.innerHTML = `<span class="filter-chips-label">Filtered by</span>`;

  state.activeFilters.forEach((f, i) => {
    const chip = document.createElement("span");
    chip.className = "filter-chip";
    chip.innerHTML = `${escapeHtml(f.label)}
      <button class="filter-chip-remove" title="Remove" onclick="removeCrossFilter(${i})">×</button>`;
    row.appendChild(chip);
  });

  const clearBtn = document.createElement("button");
  clearBtn.className = "filter-clear-all";
  clearBtn.textContent = "Clear all";
  clearBtn.onclick = clearAllFilters;
  row.appendChild(clearBtn);
}

function removeCrossFilter(index) {
  state.activeFilters.splice(index, 1);
  renderFilterChips();
  applyFilters();
}

function clearAllFilters() {
  state.activeFilters = [];
  renderFilterChips();
  applyFilters();
}

async function applyFilters() {
  const filtersJson = JSON.stringify(
    state.activeFilters.map(f => ({ table: f.table, col: f.col, val: f.val }))
  );
  // Refresh all charts in parallel
  state.dashboardSpecs.forEach((spec, i) => {
    if (spec) refreshChartWithFilters(i, filtersJson);
  });
  // Refresh all KPIs in parallel
  state.kpiSpecs.forEach((kpi, i) => {
    if (kpi) refreshKpiWithFilters(i, filtersJson);
  });
}

async function refreshChartWithFilters(index, filtersJson) {
  const spec = state.dashboardSpecs[index];
  if (!spec) return;
  setChartLoading(index, true);
  try {
    const params = new URLSearchParams({
      workspace_id: state.selectedWs.id,
      dataset_id:   state.selectedDs.id,
      dax:          spec.dax,
      chart_type:   spec.chart_type,
      x_column:     spec.x_column,
      y_column:     spec.y_column,
      index,
      filters:      filtersJson,
    });
    const res  = await fetch(`/api/filter-chart?${params}`);
    const data = await res.json();
    const plotDiv = document.getElementById(`plotly-${index}`);
    if (!plotDiv || data.error) return;
    const fig = JSON.parse(data.plotly);
    Plotly.react(plotDiv, fig.data, fig.layout);  // smooth animated update
    state.dashboardSpecs[index].plotData   = fig.data;
    state.dashboardSpecs[index].plotlyJson = data.plotly;
  } catch (_) { /* silently fail per-chart */ }
  finally { setChartLoading(index, false); }
}

async function refreshKpiWithFilters(index, filtersJson) {
  const kpi   = state.kpiSpecs[index];
  const valEl = document.getElementById(`kpi-val-${index}`);
  if (!kpi || !valEl) return;
  valEl.classList.add("loading");
  try {
    const params = new URLSearchParams({
      workspace_id: state.selectedWs.id,
      dataset_id:   state.selectedDs.id,
      dax:          kpi.dax,
      filters:      filtersJson,
    });
    const res  = await fetch(`/api/filter-kpi?${params}`);
    const data = await res.json();
    valEl.classList.remove("loading");
    valEl.textContent = data.value != null
      ? formatKpiValue(data.value, kpi.format)
      : "—";
  } catch (_) { valEl.classList.remove("loading"); }
}

function setChartLoading(index, on) {
  const el = document.getElementById(`loading-${index}`);
  if (el) el.classList.toggle("active", on);
}

// ── Per-card actions ───────────────────────────────────────────────────────────
function toggleCardMenu(e, index) {
  e.stopPropagation();
  const menu = document.getElementById(`card-menu-${index}`);
  if (!menu) return;
  const wasOpen = menu.classList.contains("open");
  closeAllMenus();
  if (!wasOpen) menu.classList.add("open");
}

function closeAllMenus() {
  document.querySelectorAll(".card-menu.open").forEach(m => m.classList.remove("open"));
}

document.addEventListener("click", closeAllMenus);

async function refreshChart(index) {
  const filtersJson = JSON.stringify(
    state.activeFilters.map(f => ({ table: f.table, col: f.col, val: f.val }))
  );
  await refreshChartWithFilters(index, filtersJson);
}

function expandChart(index) {
  const spec = state.dashboardSpecs[index];
  if (!spec || !spec.plotlyJson) return;
  const titleEl     = document.getElementById("drill-title");
  const container   = document.getElementById("drill-chart-container");
  const modal       = document.getElementById("drill-modal");
  const drillLabel  = document.querySelector(".drill-label");
  if (drillLabel) drillLabel.textContent = "CHART";
  titleEl.textContent = spec.title;
  container.innerHTML = "";
  modal.style.display = "flex";
  requestAnimationFrame(() => {
    const fig = JSON.parse(spec.plotlyJson);
    Plotly.newPlot(container, fig.data, fig.layout, { ...PLOTLY_CONFIG, responsive: true });
    if (window.ResizeObserver) {
      new ResizeObserver(() => Plotly.Plots.resize(container)).observe(container);
    }
  });
}

function downloadCSV(index) {
  const spec = state.dashboardSpecs[index];
  if (!spec || !spec.plotData) return;
  const trace = spec.plotData[0];
  const xs    = trace.x || trace.labels || trace.y || [];
  const ys    = trace.y || trace.values || trace.x || [];
  const xCol  = spec.x_column || "x";
  const yCol  = spec.y_column || "y";
  const rows  = [`"${xCol}","${yCol}"`];
  xs.forEach((x, i) => rows.push(`"${String(x).replace(/"/g,'""')}",${ys[i] ?? ""}`));
  const blob = new Blob([rows.join("\n")], { type: "text/csv" });
  const a    = Object.assign(document.createElement("a"), {
    href:     URL.createObjectURL(blob),
    download: `${spec.title}.csv`,
  });
  a.click();
  URL.revokeObjectURL(a.href);
}

function drillChartMenu(index) {
  // Open drill modal by prompting which value to drill (show list)
  const spec    = state.dashboardSpecs[index];
  const plotDiv = document.getElementById(`plotly-${index}`);
  if (!spec || !spec.drill_dax_template || !plotDiv) return;
  const trace   = (plotDiv.data || [])[0];
  if (!trace) return;
  const values  = trace.x || trace.labels || [];
  if (!values.length) return;
  // Just drill into the first value as a demo; a future UX improvement would show a picker
  const val = String(values[0]);
  const dax = spec.drill_dax_template.replace(/\{value\}/g, val);
  openDrillModal(`${spec.drill_next} — ${val}`, dax);
}

// ── Drill-down modal ───────────────────────────────────────────────────────────
async function openDrillModal(title, dax) {
  const modal     = document.getElementById("drill-modal");
  const titleEl   = document.getElementById("drill-title");
  const container = document.getElementById("drill-chart-container");
  const emptyEl   = document.getElementById("drill-empty");

  titleEl.textContent = title;
  container.innerHTML = `<div style="display:flex;align-items:center;justify-content:center;height:100%;gap:12px;color:var(--text-muted)"><div class="spinner"></div> Querying…</div>`;
  emptyEl.style.display = "none";
  modal.style.display = "flex";

  try {
    const params = new URLSearchParams({
      workspace_id: state.selectedWs.id,
      dataset_id:   state.selectedDs.id,
      dax,
      title,
    });
    const res  = await fetch(`/api/drilldown?${params}`);
    const data = await res.json();

    if (data.error) {
      container.innerHTML = "";
      emptyEl.textContent = data.error;
      emptyEl.style.display = "block";
      return;
    }

    container.innerHTML = "";
    const fig = JSON.parse(data.plotly);
    if (fig.data[0]) fig.data[0].marker = { ...(fig.data[0].marker || {}), color: "#60A5FA" };
    requestAnimationFrame(() => {
      Plotly.newPlot(container, fig.data, fig.layout, { ...PLOTLY_CONFIG, responsive: true });
      if (window.ResizeObserver) {
        new ResizeObserver(() => Plotly.Plots.resize(container)).observe(container);
      }
    });

  } catch (err) {
    container.innerHTML = "";
    emptyEl.textContent = `Error: ${err.message}`;
    emptyEl.style.display = "block";
  }
}

function closeDrillModal() {
  document.getElementById("drill-modal").style.display = "none";
  Plotly.purge(document.getElementById("drill-chart-container"));
}

// Close on backdrop click or Escape
document.getElementById("drill-backdrop").addEventListener("click", closeDrillModal);
document.getElementById("drill-close").addEventListener("click", closeDrillModal);
document.addEventListener("keydown", e => { if (e.key === "Escape") closeDrillModal(); });

// ── Event rendering ────────────────────────────────────────────────────────────
let currentStepEl = null;

function handleEvent(event) {
  switch (event.type) {

    case "thinking": {
      state.stepCount++;
      currentStepEl = createStepCard(state.stepCount, event.thought);
      content.appendChild(currentStepEl);
      scrollToBottom();
      break;
    }

    case "dax": {
      if (!currentStepEl) break;
      const section = document.createElement("div");
      section.className = "step-section";
      section.innerHTML = `
        <div class="step-section-label">DAX Query</div>
        <div class="dax-block">${highlightDAX(escapeHtml(event.query))}</div>
      `;
      currentStepEl.querySelector(".step-body").appendChild(section);
      openStep(currentStepEl);
      scrollToBottom();
      break;
    }

    case "result": {
      if (!currentStepEl) break;
      const section = document.createElement("div");
      section.className = "step-section";
      section.innerHTML = `
        <div class="step-section-label">Result</div>
        <div class="result-pre">${escapeHtml(event.data)}</div>
      `;
      currentStepEl.querySelector(".step-body").appendChild(section);
      scrollToBottom();
      break;
    }

    case "chart": {
      const target = currentStepEl || content.lastElementChild;
      if (!target) break;
      const section = document.createElement("div");
      section.className = "step-section";
      section.innerHTML = `
        <div class="step-section-label">${event.title || "Chart"}</div>
        <img class="chart-img" src="data:image/png;base64,${event.image}" alt="chart" />
      `;
      target.querySelector(".step-body").appendChild(section);
      openStep(target);
      scrollToBottom();
      break;
    }

    case "complete": {
      const card = document.createElement("div");
      card.className = "answer-card";
      card.innerHTML = `
        <div class="answer-label">Answer</div>
        <div class="answer-text">${formatAnswer(event.answer)}</div>
      `;
      content.appendChild(card);
      setStatus("done", `Done · ${state.stepCount} step${state.stepCount !== 1 ? "s" : ""}`);
      runBtn.disabled = false;
      state.currentSource?.close();
      scrollToBottom();
      break;
    }

    case "error": {
      const el = document.createElement("div");
      el.className = "pill error";
      el.innerHTML = `✗ ${escapeHtml(event.message)}`;
      el.style.padding = "10px 14px";
      content.appendChild(el);
      setStatus("error", "Error");
      runBtn.disabled = false;
      state.currentSource?.close();
      break;
    }
  }
}

// ── Step card helpers ──────────────────────────────────────────────────────────
function createStepCard(num, thought) {
  const card = document.createElement("div");
  card.className = "step-card open";
  card.innerHTML = `
    <div class="step-header">
      <div class="step-number">${num}</div>
      <div class="step-thought">${escapeHtml(thought)}</div>
      <span class="step-chevron">▶</span>
    </div>
    <div class="step-body"></div>
  `;
  card.querySelector(".step-header").addEventListener("click", () => {
    card.classList.toggle("open");
  });
  return card;
}

function openStep(card) {
  card.classList.add("open");
}

// ── Utilities ──────────────────────────────────────────────────────────────────
function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function formatAnswer(text) {
  // Use marked.js for full markdown rendering (tables, bold, lists, code)
  if (typeof marked !== "undefined") {
    return marked.parse(text);
  }
  // Fallback if CDN fails
  return text.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>").replace(/\n/g, "<br>");
}

/**
 * Minimal DAX syntax highlighter.
 * Colours keywords, functions, strings, and numbers.
 * No external library needed — just regex over escaped HTML.
 */
function highlightDAX(code) {
  const keywords = /\b(EVALUATE|RETURN|VAR|SUMMARIZECOLUMNS|CALCULATETABLE|CALCULATE|FILTER|ALL|ALLEXCEPT|TOPN|ADDCOLUMNS|SELECTCOLUMNS|UNION|INTERSECT|EXCEPT|NATURALINNERJOIN|CROSSJOIN|ROW|DATATABLE|GENERATESERIES|CALENDAR|TODAY|NOW|BLANK|TRUE|FALSE|IN|NOT|AND|OR|IF|SWITCH|IFERROR)\b/g;
  const functions = /\b(SUM|AVERAGE|COUNT|COUNTROWS|COUNTBLANK|COUNTA|MAX|MIN|DISTINCTCOUNT|DIVIDE|ROUND|ABS|INT|FLOOR|CEILING|CONCATENATE|FORMAT|LEFT|RIGHT|MID|LEN|TRIM|UPPER|LOWER|YEAR|MONTH|DAY|DATE|DATEADD|DATESBETWEEN|DATESYTD|SAMEPERIODLASTYEAR|RELATED|RELATEDTABLE|LOOKUPVALUE|RANKX|PERCENTILE|STDEV|VAR\.P|HASONEVALUE|ISBLANK|ISNUMBER|ISTEXT)\b/g;

  return code
    .replace(keywords,  m => `<span class="dax-kw">${m}</span>`)
    .replace(functions, m => `<span class="dax-fn">${m}</span>`)
    .replace(/"([^"]*)"/g, m => `<span class="dax-str">${m}</span>`)
    .replace(/\b(\d+(\.\d+)?)\b/g, m => `<span class="dax-num">${m}</span>`);
}

function formatKpiValue(val, format) {
  const abs = Math.abs(val);
  if (format === "currency") {
    if (abs >= 1e6) return "$" + (val / 1e6).toFixed(1) + "M";
    if (abs >= 1e3) return "$" + (val / 1e3).toFixed(1) + "K";
    return "$" + val.toFixed(2);
  }
  if (format === "percent") return val.toFixed(1) + "%";
  // number
  if (abs >= 1e6) return (val / 1e6).toFixed(1) + "M";
  if (abs >= 1e3) return (val / 1e3).toFixed(1) + "K";
  return val.toLocaleString(undefined, { maximumFractionDigits: 0 });
}

function setStatus(type, text) {
  const el = document.getElementById("status-pill");
  if (!el) return;
  const icons = { running: "⟳", done: "✓", error: "✗" };
  el.innerHTML = `<span class="pill ${type}">${icons[type]} ${text}</span>`;
}

function renderSchemaPanel(schema) {
  const tables   = (schema.tables  || []).filter(t => !t.TableName.startsWith("$"));
  const measures = schema.measures || [];
  const columns  = schema.columns  || [];

  // Group columns by table
  const colsByTable = {};
  columns.forEach(c => {
    const t = c.TableName;
    if (!colsByTable[t]) colsByTable[t] = [];
    colsByTable[t].push(c.ColumnName);
  });

  let tableHTML = "";
  if (tables.length === 0) {
    tableHTML = `<p style="color:var(--text-muted);font-size:12px">No tables discovered yet — the agent will explore the model on its first query.</p>`;
  } else {
    tables.forEach(t => {
      const cols = colsByTable[t.TableName] || [];
      const colTags = cols.map(c =>
        `<span style="background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:2px 7px;font-size:11px;color:var(--text-muted)">${escapeHtml(c)}</span>`
      ).join(" ");
      tableHTML += `
        <div style="margin-bottom:12px">
          <div style="font-size:12px;font-weight:700;color:var(--accent);margin-bottom:6px">◈ ${escapeHtml(t.TableName)}</div>
          <div style="display:flex;flex-wrap:wrap;gap:4px">${colTags || '<span style="color:var(--text-muted);font-size:11px">columns loading...</span>'}</div>
        </div>`;
    });
  }

  let measuresHTML = "";
  if (measures.length > 0) {
    const tags = measures.map(m =>
      `<span style="background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:2px 7px;font-size:11px;color:#A78BFA">[${escapeHtml(m.MeasureName)}]</span>`
    ).join(" ");
    measuresHTML = `
      <div style="margin-top:16px">
        <div style="font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--text-muted);margin-bottom:8px">Measures</div>
        <div style="display:flex;flex-wrap:wrap;gap:4px">${tags}</div>
      </div>`;
  }

  content.innerHTML = `
    <div style="background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden">
      <div id="schema-header" style="display:flex;align-items:center;justify-content:space-between;padding:12px 16px;cursor:pointer" onclick="toggleSchema()">
        <div style="display:flex;align-items:center;gap:10px">
          <span style="font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:var(--text-muted)">Data Model</span>
          <span style="background:var(--surface-2);border:1px solid var(--border);border-radius:99px;padding:2px 8px;font-size:11px;color:var(--accent)">${tables.length} tables</span>
          ${measures.length ? `<span style="background:var(--surface-2);border:1px solid var(--border);border-radius:99px;padding:2px 8px;font-size:11px;color:#A78BFA">${measures.length} measures</span>` : ""}
        </div>
        <span id="schema-chevron" style="color:var(--text-muted);font-size:11px;transition:transform .2s">▶</span>
      </div>
      <div id="schema-body" style="padding:16px;border-top:1px solid var(--border)">
        ${tableHTML}
        ${measuresHTML}
      </div>
    </div>
    <div style="text-align:center;color:var(--text-muted);font-size:12px;padding:8px 0">
      Ask a question below to start the analysis
    </div>
  `;
}

function toggleSchema() {
  const body    = document.getElementById("schema-body");
  const chevron = document.getElementById("schema-chevron");
  const open    = body.style.display !== "none";
  body.style.display    = open ? "none" : "block";
  chevron.style.transform = open ? "rotate(0deg)" : "rotate(90deg)";
}

function showEmptyState(msg, spinner) {
  content.innerHTML = `
    <div class="empty-state">
      ${spinner ? '<div class="spinner" style="width:24px;height:24px"></div>' : '<div class="big-icon">◫</div>'}
      <p style="text-align:center;line-height:1.8">${msg}</p>
    </div>
  `;
}

function scrollToBottom() {
  content.scrollTop = content.scrollHeight;
}


// ── Init ───────────────────────────────────────────────────────────────────────
showEmptyState("Connect to Power BI to get started", false);
// Auto-load workspaces if already authenticated (token cached from previous session)
fetch("/api/auth/status").then(r => r.json()).then(data => {
  if (data.authenticated) {
    authBtn.textContent = "✓  Connected";
    authBtn.classList.add("connected");
    loadWorkspaces();
    document.getElementById("upload-btn").disabled = false;
  }
}).catch(() => {});


// ── Data Ingest ────────────────────────────────────────────────────────────────
const ingestOverlay    = document.getElementById("ingest-overlay");
const ingestFileInput  = document.getElementById("ingest-file-input");
const ingestDropzone   = document.getElementById("ingest-dropzone");
const ingestContent    = document.getElementById("ingest-content");

let ingestSession = null;  // {session_id, profile, suggestions, previewData}

function showIngestView() {
  // Populate workspace selector from state
  const sel = document.getElementById("ingest-workspace-select");
  sel.innerHTML = "";
  (state.workspaces || []).forEach(ws => {
    const opt = document.createElement("option");
    opt.value = ws.id;
    opt.textContent = ws.name;
    sel.appendChild(opt);
  });
  if (sel.options.length === 0) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "— No workspaces loaded —";
    sel.appendChild(opt);
  }
  ingestDropzone.style.display = "";
  ingestContent.style.display  = "none";
  document.getElementById("ingest-filename").textContent = "No file selected";
  ingestSession = null;
  ingestOverlay.style.display = "flex";
}

function closeIngestView() {
  ingestOverlay.style.display = "none";
}

// Drag-and-drop
ingestDropzone.addEventListener("dragover", e => {
  e.preventDefault();
  ingestDropzone.classList.add("drag-over");
});
ingestDropzone.addEventListener("dragleave", () => ingestDropzone.classList.remove("drag-over"));
ingestDropzone.addEventListener("drop", e => {
  e.preventDefault();
  ingestDropzone.classList.remove("drag-over");
  const file = e.dataTransfer.files[0];
  if (file) handleFileUpload(file);
});

ingestFileInput.addEventListener("change", e => {
  const file = e.target.files[0];
  if (file) handleFileUpload(file);
  ingestFileInput.value = "";
});

async function handleFileUpload(file) {
  ingestDropzone.innerHTML = `
    <div class="drop-zone-icon">⏳</div>
    <div class="drop-zone-text">Uploading and profiling…</div>
    <div class="drop-zone-hint">${file.name}</div>
  `;
  const form = new FormData();
  form.append("file", file);
  try {
    const res  = await fetch("/api/ingest/upload", { method: "POST", body: form });
    const data = await res.json();
    if (data.error) {
      showIngestError(data.error);
      return;
    }
    ingestSession = {
      session_id:  data.session_id,
      suggestions: data.suggestions || [],
      appliedIds:  new Set(),
    };
    document.getElementById("ingest-filename").textContent = data.filename;
    // Set suggested dataset name from filename (strip extension)
    const nameInput = document.getElementById("ingest-dataset-name");
    if (!nameInput.value) {
      nameInput.value = data.filename.replace(/\.[^.]+$/, "").replace(/[^a-zA-Z0-9 _-]/g, "_");
    }
    renderProfile(data.profile);
    renderSuggestions(data.suggestions);
    renderPreview(data.preview, data.profile.total_rows);
    ingestDropzone.style.display = "none";
    ingestContent.style.display  = "grid";
    // Reset push UI
    document.getElementById("ingest-push-progress").style.display = "none";
    document.getElementById("ingest-push-btn").disabled = false;
  } catch (err) {
    showIngestError(String(err));
  }
}

function showIngestError(msg) {
  ingestDropzone.innerHTML = `
    <div class="drop-zone-icon" style="color:var(--red)">✗</div>
    <div class="drop-zone-text" style="color:var(--red)">Upload failed</div>
    <div class="drop-zone-hint">${msg}</div>
    <div class="drop-zone-hint" style="margin-top:8px">
      <span class="drop-zone-link" onclick="document.getElementById('ingest-file-input').click()">Try again</span>
    </div>
  `;
}

function renderProfile(profile) {
  const container = document.getElementById("ingest-profile");
  const dupWarn   = profile.dup_rows > 0
    ? `<div style="color:#F87171;font-size:11px;margin-bottom:8px">⚠ ${profile.dup_rows} duplicate rows detected</div>`
    : "";

  let rows = "";
  for (const col of profile.columns) {
    const issueHtml = col.issues.length
      ? col.issues.map(i => `<span class="issue-badge">${i}</span>`).join(" ")
      : `<span class="issue-ok">✓</span>`;
    rows += `
      <tr>
        <td style="font-family:'Fira Code',monospace;color:var(--accent)">${col.name}</td>
        <td style="color:var(--text-muted)">${col.dtype}</td>
        <td>${issueHtml}</td>
      </tr>`;
  }

  container.innerHTML = `
    <div style="font-size:11px;color:var(--text-muted);margin-bottom:6px">
      ${profile.total_rows.toLocaleString()} rows × ${profile.total_cols} columns
    </div>
    ${dupWarn}
    <table class="profile-table">
      <thead><tr><th>Column</th><th>Type</th><th>Issues</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function renderSuggestions(suggestions) {
  const container = document.getElementById("ingest-suggestions");
  if (!suggestions || suggestions.length === 0) {
    container.innerHTML = `<div style="font-size:12px;color:var(--text-muted)">No issues detected — data looks clean.</div>`;
    return;
  }
  container.innerHTML = suggestions.map((s, i) => `
    <div class="transform-card" id="transform-card-${i}">
      <div class="transform-card-left">
        <div class="transform-title">${s.title}</div>
        ${s.column ? `<div class="transform-col">${s.column}</div>` : ""}
        <div class="transform-reason">${s.reason}</div>
      </div>
      <button class="transform-apply" id="apply-btn-${i}" onclick="applyTransform(${i})">Apply</button>
    </div>
  `).join("");
}

async function applyTransform(index) {
  if (!ingestSession) return;
  const suggestion = ingestSession.suggestions[index];
  const btn  = document.getElementById(`apply-btn-${index}`);
  const card = document.getElementById(`transform-card-${index}`);
  btn.disabled = true;
  btn.textContent = "…";

  const form = new FormData();
  form.append("session_id", ingestSession.session_id);
  form.append("code", suggestion.code);

  try {
    const res  = await fetch("/api/ingest/transform", { method: "POST", body: form });
    const data = await res.json();
    if (data.error) {
      btn.textContent = "Error";
      btn.title = data.error;
      btn.disabled = false;
      return;
    }
    card.classList.add("applied");
    btn.textContent = "✓";
    ingestSession.appliedIds.add(index);
    renderProfile(data.profile);
    renderPreview(data.preview, data.profile.total_rows);
  } catch (err) {
    btn.textContent = "Error";
    btn.disabled = false;
  }
}

function renderPreview(rows, totalRows) {
  const container = document.getElementById("ingest-preview");
  document.getElementById("preview-row-count").textContent = `(first 10 of ${totalRows.toLocaleString()})`;
  if (!rows || rows.length === 0) {
    container.innerHTML = `<div style="color:var(--text-muted);font-size:12px;padding:12px">No data</div>`;
    return;
  }
  const cols    = Object.keys(rows[0]);
  const headers = cols.map(c => `<th>${c}</th>`).join("");
  const bodyRows = rows.map(row =>
    `<tr>${cols.map(c => {
      const v = row[c];
      const isNull = v === null || v === undefined || v === "";
      return `<td class="${isNull ? "null-cell" : ""}">${isNull ? "null" : String(v)}</td>`;
    }).join("")}</tr>`
  ).join("");
  container.innerHTML = `
    <table class="preview-table">
      <thead><tr>${headers}</tr></thead>
      <tbody>${bodyRows}</tbody>
    </table>`;
}

async function pushDataset() {
  if (!ingestSession) return;
  const workspaceId  = document.getElementById("ingest-workspace-select").value;
  const datasetName  = document.getElementById("ingest-dataset-name").value.trim();
  if (!workspaceId || !datasetName) {
    alert("Please select a workspace and enter a dataset name.");
    return;
  }

  const pushBtn  = document.getElementById("ingest-push-btn");
  const progress = document.getElementById("ingest-push-progress");
  const barFill  = document.getElementById("push-bar-fill");
  const statusTx = document.getElementById("push-status-text");

  pushBtn.disabled     = true;
  progress.style.display = "";
  barFill.style.width  = "0%";
  statusTx.textContent = "Connecting…";

  const params = new URLSearchParams({
    session_id:   ingestSession.session_id,
    workspace_id: workspaceId,
    dataset_name: datasetName,
  });

  const es = new EventSource(`/api/ingest/push?${params}`);

  es.onmessage = e => {
    const ev = JSON.parse(e.data);
    if (ev.type === "status") {
      statusTx.textContent = ev.message;
      barFill.style.width  = "10%";
    } else if (ev.type === "progress") {
      const pct = Math.round((ev.done / ev.total) * 100);
      barFill.style.width  = `${pct}%`;
      statusTx.textContent = `Pushing rows… ${ev.done.toLocaleString()} / ${ev.total.toLocaleString()}`;
    } else if (ev.type === "done") {
      barFill.style.width  = "100%";
      statusTx.innerHTML   = `<span style="color:#34D399">✓ Dataset "${ev.dataset_name}" pushed! Refresh Power BI to see it.</span>`;
      es.close();
      ingestSession = null;
      // Re-enable button but session is gone
      pushBtn.textContent = "Done";
    } else if (ev.type === "error") {
      statusTx.innerHTML  = `<span style="color:#F87171">✗ ${ev.message}</span>`;
      barFill.style.background = "#F87171";
      es.close();
      pushBtn.disabled = false;
    }
  };

  es.onerror = () => {
    statusTx.innerHTML = `<span style="color:#F87171">✗ Connection lost</span>`;
    es.close();
    pushBtn.disabled = false;
  };
}
