import { ChartBuilder, slugify } from "/static/js/chartbuilder.js";
import { RESULT_PAGE, renderTable } from "/static/js/charts.js";
import {
  dashboard,
  dashboardName,
  defaultChartSpec,
  getEtag,
  mutatingHeaders,
  pollExecution,
  setEtag,
  submitRun,
  toast,
} from "/static/js/runner.js";
import { dashboardPath } from "/static/js/paths.js";
import { withoutPeriod } from "/static/js/sentence.js";

let queries = dashboard.queries;

const runBtn = document.getElementById("run-btn");
const cancelBtn = document.getElementById("cancel-btn");
const csvBtn = document.getElementById("csv-btn");
const picker = document.getElementById("query-picker");
const meta = document.getElementById("results-meta");
const body = document.getElementById("results-body");
const addBtn = document.getElementById("qb-add");
const saveFeedback = document.getElementById("qb-save-feedback");
const saveStatus = document.getElementById("qb-save-status");
const anotherBtn = document.getElementById("qb-another");
const refreshBtn = document.getElementById("qb-refresh");
let saving = false;
let added = false;
let needsRefresh = false;
const titleInput = document.getElementById("qb-title");
const sourcePicker = document.getElementById("source-picker");
const schemaStatus = document.getElementById("schema-status");
const metricPane = document.getElementById("metric-pane");
const metricPicker = document.getElementById("metric-picker");
const metricDims = document.getElementById("metric-dims");
const metricDimsField = document.getElementById("metric-dims-field");
const metricGrain = document.getElementById("metric-grain");
const metricGrainField = document.getElementById("metric-grain-field");
const modeToggle = document.getElementById("mode-toggle");
const textEditor = document.getElementById("text-editor");
const textMarkdown = document.getElementById("text-markdown");
const hint = document.getElementById("qb-hint");

const params = new URLSearchParams(location.search);
const editingTileId = params.get("tile");
const editingTile = editingTileId
  ? dashboard.tiles.find((t) => t.id === editingTileId)
  : null;

const editor = ace.edit("sql-editor", {
  mode: "ace/mode/sql",
  showPrintMargin: false,
  fontSize: 13,
  fontFamily: "Geist Mono Var, ui-monospace, monospace",
  highlightActiveLine: false,
  wrap: true,
});
const langTools = ace.require("ace/ext/language_tools");
editor.setOptions({
  enableBasicAutocompletion: true,
  enableLiveAutocompletion: true,
});

let schemaCompletions = [];
let mode = "sql";

function setSchemaStatus(text, { error = false } = {}) {
  if (!schemaStatus) return;
  schemaStatus.hidden = !text;
  schemaStatus.textContent = text;
  schemaStatus.classList.toggle("is-error", error);
  schemaStatus.title = error ? text : "";
}

let roleGeneration = 0;
let roleContext;
let roleSwitchPending = false;
let switchNote = null;
const publishRoles = detail => window.dispatchEvent(new CustomEvent('sqldash:roles', {detail}));
async function requestRoles(source, role, path = 'roles') {
  const url = new URL(`/api/dashboards/${dashboardPath(dashboardName)}/${path}`, location);
  const switching = role !== undefined;
  if (!switching && source) url.searchParams.set('source', source);
  const field = path === 'warehouses' ? 'warehouse' : 'role';
  const response = await fetch(url, switching ? {
    method:'POST', headers:mutatingHeaders({'Content-Type':'application/json'}), body:JSON.stringify({source, [field]:role}),
  } : {});
  const data = await response.json();
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Could not load roles');
  return data;
}
function addRoleSource(context) {
  const existing = [...sourcePicker.options].find(option=>option.value===context.source);
  const label = [context.source_label, context.database, context.current.join(', '), context.selected_warehouse].filter(Boolean).join(' · ');
  if (existing) {
    if (existing.dataset.unavailable || existing.dataset.context) {
      delete existing.dataset.unavailable;
      existing.textContent = label;
    }
    return;
  }
  sourcePicker.add(new Option(label, context.source));
  sourcePicker.options[sourcePicker.options.length - 1].dataset.context = 'true';
}
async function loadRoles() {
  if (!params.has('embedded')) return;
  const generation = ++roleGeneration;
  const source = sourcePicker.value;
  roleContext = null;
  publishRoles({loading:true, source});
  try {
    let context = await requestRoles(source);
    if (generation !== roleGeneration) return;
    if (switchNote?.source === context.source) context = {...context, ...switchNote.notes};
    addRoleSource(context);
    roleContext = context; publishRoles(context);
  } catch (error) {
    if (generation !== roleGeneration) return;
    publishRoles({source, error:error.message, current:[], roles:[], switchable:false});
  }
}
export function selectWorkspaceWarehouse(warehouse) {
  return selectWorkspaceRole(warehouse, 'warehouses');
}
export async function selectWorkspaceRole(role, path = 'roles') {
  if (roleSwitchPending) return;
  if (runBtn.disabled || saving) {
    publishRoles({...roleContext, pending:false});
    return;
  }
  const generation = ++roleGeneration;
  const source = sourcePicker.value;
  roleSwitchPending = true;
  const layout = document.querySelector('.workspace-editor');
  if (layout) layout.inert = true;
  publishRoles({...roleContext, source, pending:true});
  try {
    const context = await requestRoles(source, role, path);
    if (generation !== roleGeneration || source !== sourcePicker.value) return;
    const notes = Object.fromEntries(['warehouse_note', 'database_note', 'database_warning'].filter(field => context[field]).map(field => [field, context[field]]));
    switchNote = Object.keys(notes).length ? {source:context.source, notes} : null;
    addRoleSource(context);
    sourcePicker.value = context.source;
    sourcePicker.dispatchEvent(new Event('change', {bubbles:true}));
    roleContext = context; publishRoles(context);
  } catch (error) {
    if (generation === roleGeneration) publishRoles({...roleContext, source, error:error.message});
  } finally {
    roleSwitchPending = false;
    if (layout) layout.inert = false;
  }
}
sourcePicker.addEventListener('change', loadRoles);
window.addEventListener('sqldash:schema-refresh', loadRoles);
loadRoles();

let schemaGeneration = 0;
let browseDatabase = '';
let databaseSource;
let databaseContext;
let databaseRequest;
async function selectWorkspaceDatabase(database) {
  const republish = () => databaseContext && window.dispatchEvent(new CustomEvent('sqldash:databases',{detail:{...databaseContext,database:browseDatabase,source:sourcePicker.value}}));
  if (roleSwitchPending || runBtn.disabled || saving || database === browseDatabase) { republish(); return; }
  const source = sourcePicker.value;
  roleSwitchPending = true;
  const layout = document.querySelector('.workspace-editor');
  if (layout) layout.inert = true;
  try {
    const response = await fetch(new URL(`/api/dashboards/${dashboardPath(dashboardName)}/databases`, location), {
      method:'POST', headers:mutatingHeaders({'Content-Type':'application/json'}), body:JSON.stringify({source, database}),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Could not switch database');
    if (source !== sourcePicker.value) return;
    if (![...sourcePicker.options].some(option => option.value === data.source)) {
      const option = new Option(`${sourcePicker.selectedOptions[0]?.textContent || 'Connection'} · ${data.database}`, data.source);
      option.dataset.context = 'true';
      sourcePicker.add(option);
    }
    sourcePicker.value = data.source;
    sourcePicker.dispatchEvent(new Event('change', {bubbles:true}));
  } catch (error) {
    republish();
    setSchemaStatus(error.message, {error:true});
  } finally {
    roleSwitchPending = false;
    if (layout) layout.inert = false;
  }
}
window.addEventListener('sqldash:browse-database', event => selectWorkspaceDatabase(event.detail.database));
async function loadSchema() {
  const generation = ++schemaGeneration;
  schemaCompletions=[];
  const source=sourcePicker?.value || '';
  if(databaseSource!==source){databaseSource=source;databaseContext=null;browseDatabase='';}
  window.dispatchEvent(new CustomEvent("sqldash:schema", { detail: { status:"loading", source, database:browseDatabase } }));
  if (mode !== "sql") return;
  const url = new URL(`/api/dashboards/${dashboardPath(dashboardName)}/schema`, location);
  if (sourcePicker?.value) url.searchParams.set("source", sourcePicker.value);
  setSchemaStatus("loading schema…");
  try {
    if(!databaseContext){
      // Boot and the restored source both call loadSchema; they share one
      // discovery request instead of each running SHOW DATABASES.
      if(databaseRequest?.source!==source){
        const contextUrl=new URL(`/api/dashboards/${dashboardPath(dashboardName)}/databases`,location);
        if(source)contextUrl.searchParams.set('source',source);
        databaseRequest={source,result:fetch(contextUrl).then(async response=>{
          if(!response.ok)throw new Error((await response.json().catch(()=>({}))).detail || 'Database discovery failed');
          return response.json();
        })};
      }
      const pending=databaseRequest;
      let context;
      try{context=await pending.result;}
      finally{if(databaseRequest===pending)databaseRequest=null;}
      if(generation!==schemaGeneration)return;
      databaseContext=context;
      browseDatabase=databaseContext.current || '';
    }
    window.dispatchEvent(new CustomEvent('sqldash:databases',{detail:{...databaseContext,database:browseDatabase,source}}));
    if(browseDatabase)url.searchParams.set('database',browseDatabase);
    const res = await fetch(url);
    if (mode !== "sql" || generation !== schemaGeneration) return;
    if (!res.ok) {
      const detail = (await res.json().catch(() => ({}))).detail;
      schemaCompletions = [];
      setSchemaStatus(detail || "schema unavailable", { error: true });
      window.dispatchEvent(new CustomEvent("sqldash:schema", { detail: { status:"error", message:detail || "Schema unavailable" } }));
      return;
    }
    const { tables } = await res.json();
    if (generation !== schemaGeneration) return;
    window.dispatchEvent(new CustomEvent("sqldash:schema", { detail: { status:"ready", tables, source, database:browseDatabase } }));
    const completions = [];
    for (const table of tables) {
      const qualified = table.schema ? `${table.schema}.${table.name}` : table.name;
      completions.push({ value: browseDatabase ? table.sql : table.name_sql, caption: qualified, meta: "table", score: 200 });
      if (qualified !== table.name) {
        completions.push({ value: table.sql, meta: "table", score: 180 });
      }
      for (const col of table.columns) {
        completions.push({
          value: col.sql,
          caption: col.name,
          meta: `${table.name} · ${col.type}`,
          score: 100,
        });
      }
    }
    const seen = new Set();
    schemaCompletions = completions.filter((c) => {
      const key = c.value + "|" + c.meta;
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
    if (mode !== "sql") return;
    const n = tables.length;
    setSchemaStatus(n === 0 ? "no tables" : `${n} table${n === 1 ? "" : "s"}`);
  } catch (err) {
    if (mode !== "sql" || generation !== schemaGeneration) return;
    window.dispatchEvent(new CustomEvent("sqldash:schema", { detail: { status:"error", message:err.message } }));
    schemaCompletions = [];
    setSchemaStatus(err.message || "schema unavailable", { error: true });
  }
}

langTools.addCompleter({
  getCompletions(_ed, _session, _pos, _prefix, callback) {
    callback(null, schemaCompletions);
  },
});
loadSchema();

const builder = new ChartBuilder({
  typeEl: document.getElementById("qb-type"),
  encodingEl: document.getElementById("qb-encoding"),
  previewEl: document.getElementById("qb-preview"),
});
builder.renderAll();

function applyEditorTheme() {
  const dark = document.documentElement.dataset.theme === "dark";
  editor.setTheme(dark ? "ace/theme/tomorrow_night" : "ace/theme/tomorrow");
  requestAnimationFrame(() => {
    const el = document.getElementById("sql-editor");
    el.style.backgroundColor = "transparent";
    el.querySelector(".ace_scroller").style.backgroundColor = "transparent";
  });
}
applyEditorTheme();
window.addEventListener("sqldash:themechange", applyEditorTheme);

let loadedQueryName = null;
picker.addEventListener("change", (event) => {
  if (event.detail?.refreshOnly) return;
  if (picker.value && queries[picker.value]) {
    editor.setValue(queries[picker.value].trim(), -1);
    loadedQueryName = picker.value;
    editor.focus();
  }
});
sourcePicker?.addEventListener("change", loadSchema);

/* ---------- SQL vs Text vs Metric mode ---------- */

function setMode(next) {
  mode = next;
  const isText = mode === "text";
  const isMetric = mode === "metric";
  modeToggle.querySelectorAll(".seg-btn").forEach((b) =>
    b.classList.toggle("active", b.dataset.mode === mode)
  );
  document.getElementById("sql-editor").style.display = isText || isMetric ? "none" : "";
  textEditor.hidden = !isText;
  metricPane.hidden = !isMetric;
  runBtn.hidden = isText;
  const hideQueryPicker = isText || isMetric;
  picker.closest(".dd")?.toggleAttribute("hidden", hideQueryPicker);
  picker.hidden = hideQueryPicker;
  sourcePicker?.closest(".dd")?.toggleAttribute("hidden", isText);
  if (sourcePicker) sourcePicker.hidden = isText;
  csvBtn.hidden = true;
  document.querySelector(".query-layout").classList.toggle("text-mode", isText);
  document.querySelector(".query-layout").classList.toggle("metric-mode", isMetric);
  if (isText) {
    hint.textContent = editingTile
      ? hint.dataset.editHint
      : "Adds a markdown text tile at the bottom of the dashboard.";
  } else if (isMetric) {
    hint.textContent = editingTile
      ? hint.dataset.editHint
      : "Adds a metric tile — the compiler writes the SQL from metrics.yaml.";
    if (schemaStatus) schemaStatus.hidden = true;
    clearResults();
  } else if (!editingTile) {
    hint.textContent = hint.dataset.sqlHint ?? hint.textContent;
  }
  relabelDefinitionSource();
  updateAddState();
  if (isText) textMarkdown.focus();
  else if (!isMetric) editor.focus();
}

modeToggle.addEventListener("click", (e) => {
  const btn = e.target.closest(".seg-btn");
  if (!btn || btn.dataset.mode === mode) return;
  if (!hint.dataset.sqlHint) hint.dataset.sqlHint = hint.textContent;
  setMode(btn.dataset.mode);
});
if (hint) hint.dataset.editHint = hint.dataset.editHint ?? hint.textContent;
if (hint && !editingTile) hint.dataset.sqlHint = hint.textContent;

function updateAddState() {
  updateQuerySharing();
  anotherBtn.hidden = !added;
  anotherBtn.disabled = saving || needsRefresh;
  refreshBtn.hidden = !needsRefresh;
  refreshBtn.disabled = saving;
  addBtn.textContent = saving ? "Saving…" : added ? "Added ✓"
    : editingTile ? "Save tile" : "Add to dashboard";
  if (saving || added || needsRefresh) {
    addBtn.disabled = true;
    return;
  }
  if (mode === "text") {
    addBtn.disabled = !textMarkdown.value.trim();
    return;
  }
  if (mode === "metric") {
    const name = metricPicker.value;
    addBtn.disabled = !name || (!catalogEntry(name) && !editingTile);
    return;
  }
  if (editingTile) {
    addBtn.disabled = !editor.getValue().trim();
    return;
  }
  addBtn.disabled = !lastRunSql;
}
textMarkdown.addEventListener("input", updateAddState);
editor.session.on("change", () => {
  if (mode === "sql") updateAddState();
});

let currentExecution = null;
let currentSubmission = null;
let lastRunSql = null;
let lastRunMetric = null;

function setMeta(html) {
  meta.innerHTML = html;
}

function showRunning(running) {
  runBtn.disabled = running;
  cancelBtn.hidden = !running;
}

let dimensionOrder = [];

function selectedDimensions() {
  const checked = [...metricDims.querySelectorAll("input:checked")].map((el) => el.value);
  const kept = dimensionOrder.filter((name) => checked.includes(name));
  return [...kept, ...checked.filter((name) => !kept.includes(name))];
}

function trackDimension(name, checked) {
  dimensionOrder = dimensionOrder.filter((existing) => existing !== name);
  if (checked) dimensionOrder.push(name);
}

function currentMetricRef() {
  const name = metricPicker.value;
  if (!name) return null;
  const authored = editingTile?.metric?.name === name ? editingTile.metric : null;
  const known = Boolean(catalogEntry(name));
  const ref = { name };
  const dimensions = known ? selectedDimensions() : authored?.dimensions ?? [];
  if (dimensions.length) ref.dimensions = [...dimensions];
  const grain = known ? metricGrain.value || null : authored?.grain ?? null;
  if (grain) ref.grain = grain;
  if (authored?.compare) ref.compare = authored.compare;
  return ref;
}

function metricRunBody() {
  const ref = currentMetricRef();
  if (!ref) return null;
  const body = {
    metric: ref.name,
    dimensions: ref.dimensions ?? [],
    grain: ref.grain ?? null,
    params: {},
  };
  if (sourcePicker?.value) body.source = sourcePicker.value;
  return body;
}

let metricsCatalog = [];

function catalogEntry(name) {
  return metricsCatalog.find((m) => m.name === name);
}

function rebuildMetricFields() {
  const def = catalogEntry(metricPicker.value);
  const same = Boolean(
    editingTile?.metric && editingTile.metric.name === metricPicker.value
  );
  const chosen = same ? editingTile.metric : { name: metricPicker.value };
  metricDims.innerHTML = "";
  const dims = def?.dimensions ?? [];
  metricDimsField.hidden = dims.length === 0;
  dimensionOrder = Array.isArray(chosen?.dimensions) ? [...chosen.dimensions] : [];
  const selected = new Set(dimensionOrder);
  for (const dim of dims) {
    const lab = document.createElement("label");
    const box = document.createElement("input");
    box.type = "checkbox";
    box.value = dim.name;
    box.checked = selected.has(dim.name);
    box.addEventListener("change", () => {
      trackDimension(box.value, box.checked);
      updateAddState();
      syncMetricChartSpec();
    });
    lab.append(box, ` ${dim.name}`);
    metricDims.appendChild(lab);
  }
  const hasTime = Boolean(def?.time_dimension);
  metricGrainField.hidden = !hasTime;
  if (hasTime) {
    const grain = chosen?.grain ?? "";
    if (grain && ![...metricGrain.options].some((o) => o.value === grain)) {
      metricGrain.add(new Option(grain, grain));
    }
    metricGrain.value = grain;
  } else {
    metricGrain.value = "";
  }
  relabelDefinitionSource();
  updateAddState();
}

function relabelDefinitionSource() {
  const opt = sourcePicker?.querySelector('option[value=""]');
  if (!opt) return;
  if (!opt.dataset.sqlLabel) opt.dataset.sqlLabel = opt.textContent;
  const type = catalogEntry(metricPicker.value)?.source_type;
  opt.textContent =
    mode === "metric" ? (type ? `definition · ${type}` : "definition") : opt.dataset.sqlLabel;
  if (sourcePicker.value === "") {
    const label = sourcePicker.closest(".dd")?.querySelector(".dd-label");
    if (label) label.textContent = opt.textContent;
  }
}

function addMetricOption(value, label) {
  metricPicker.add(new Option(label, value));
}

function rebuildMetricPicker() {
  const keep = metricPicker.value || editingTile?.metric?.name || "";
  metricPicker.options.length = 0;
  addMetricOption("", metricsCatalog.length ? "Choose a metric…" : "No metrics defined");
  for (const m of metricsCatalog) {
    const label = m.title && m.title !== m.name ? `${m.title} (${m.name})` : m.name;
    addMetricOption(m.name, label);
  }
  if (keep && ![...metricPicker.options].some((o) => o.value === keep)) {
    addMetricOption(keep, keep);
  }
  if (keep) metricPicker.value = keep;
  rebuildMetricFields();
}

async function loadMetrics() {
  let lastErr = null;
  for (let attempt = 0; attempt < 4; attempt++) {
    try {
      const res = await fetch(
        `/api/metrics?dashboard=${encodeURIComponent(dashboardName)}`
      );
      if (!res.ok) {
        lastErr = new Error(`HTTP ${res.status}`);
        await new Promise((r) => setTimeout(r, 150 * (attempt + 1)));
        continue;
      }
      metricsCatalog = (await res.json()).metrics ?? [];
      rebuildMetricPicker();
      return;
    } catch (err) {
      lastErr = err;
      await new Promise((r) => setTimeout(r, 150 * (attempt + 1)));
    }
  }
  metricsCatalog = [];
  rebuildMetricPicker();
  if (lastErr) console.warn("metrics catalog failed", lastErr);
}
let runGeneration = 0;

function clearResults() {
  runGeneration += 1;
  currentExecution = null;
  showRunning(false);
  csvBtn.hidden = true;
  setMeta('<span class="stat">Results appear below — <kbd>⌘⏎</kbd> to run</span>');
  body.innerHTML =
    '<div class="placeholder"><div>No results yet</div>' +
    '<div class="hint">Tables from your source are queryable directly</div></div>';
  builder.setResult(null, { infer: false });
  window.dispatchEvent(new CustomEvent("sqldash:result", {detail:null}));
}

function syncMetricChartSpec({ force = false } = {}) {
  const ref = currentMetricRef();
  if (!ref) return;
  const sameAuthored = Boolean(
    editingTile?.metric && editingTile.metric.name === metricPicker.value
  );
  if (!force && sameAuthored && !builder.result && !runBtn.disabled) return;
  clearResults();
  builder.setSpec(defaultChartSpec({ metric: ref }));
}

const metricsReady = loadMetrics();
metricPicker.addEventListener("change", () => {
  rebuildMetricFields();
  const same = Boolean(
    editingTile?.metric && editingTile.metric.name === metricPicker.value
  );
  if (same) {
    if (lastRunMetric !== metricPicker.value) clearResults();
    builder.setSpec(editingTile.chart ?? defaultChartSpec(editingTile));
    return;
  }
  syncMetricChartSpec({ force: true });
});
metricGrain.addEventListener("change", () => {
  updateAddState();
  syncMetricChartSpec();
});

async function run() {
  if (runBtn.disabled) return;
  currentExecution = null;
  if (roleSwitchPending) return;
  if (sourcePicker.selectedOptions[0]?.dataset.unavailable) return toast("Choose an available source before running this draft.", "error");
  csvBtn.hidden = true;
  showRunning(true);
  setMeta('<span class="status-pill running">running</span>');
  const generation = ++runGeneration;
  try {
    let payload;
    if (mode === "metric") {
      payload = metricRunBody();
      if (!payload) throw new Error("no metric on this tile");
    } else {
      const sql = editor.getSession().getTextRange(editor.getSelectionRange()) || editor.getValue();
      if (!sql.trim()) return;
      payload = { sql };
      if (sourcePicker?.value) payload.source = sourcePicker.value;
    }
    const submission = submitRun(payload);
    currentSubmission = submission;
    const execution = await submission;
    if (currentSubmission === submission) currentSubmission = null;
    if (generation !== runGeneration) {
      await fetch(`/api/executions/${execution}/cancel`, { method:"POST", headers:mutatingHeaders(), keepalive:true });
      return;
    }
    currentExecution = execution;
    const result = await pollExecution(execution);
    if (generation !== runGeneration) return;
    if (mode === "sql") lastRunSql = (payload.sql || "").trim();
    lastRunMetric = mode === "metric" ? payload.metric : null;
    renderResult(result);
  } catch (err) {
    if (generation !== runGeneration) return;
    setMeta('<span class="status-pill error">error</span>');
    body.innerHTML = "";
    const box = document.createElement("div");
    box.className = "err";
    box.textContent = err.message;
    body.appendChild(box);
    builder.setResult(null);
    window.dispatchEvent(new CustomEvent("sqldash:result", {detail:null}));
    lastRunSql = null;
    if (!editingTile) addBtn.disabled = true;
  } finally {
    if (generation === runGeneration) showRunning(false);
  }
}

function renderResult(result) {
  const secs = result.elapsed_ms >= 1000
    ? `${(result.elapsed_ms / 1000).toFixed(2)}s`
    : `${Math.round(result.elapsed_ms)}ms`;
  setMeta(
    `<span class="status-pill done">done</span>
     <span class="stat"><b>${result.row_count.toLocaleString()}</b> row${result.row_count === 1 ? "" : "s"}${result.truncated ? " (truncated)" : ""}</span>
     <span class="stat"><b>${secs}</b> query</span>
     <span class="stat"><b>${result.columns.length}</b> column${result.columns.length === 1 ? "" : "s"}</span>`
  );
  body.innerHTML = "";
  if (!params.has("embedded")) renderTable(body, { format: {} }, result, { page: RESULT_PAGE });
  window.dispatchEvent(new CustomEvent("sqldash:result", {detail:result}));
  const csvName = encodeURIComponent(titleInput.value.trim() || "query");
  csvBtn.href = `/api/executions/${currentExecution}/csv?name=${csvName}`;
  csvBtn.hidden = false;
  builder.setResult(result);
  updateAddState();
}

function errorDetail(data, status) {
  const detail = data?.detail;
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object" && detail.message) return detail.message;
  return `HTTP ${status}`;
}

function showSaveStatus(message, error = false) {
  saveFeedback.hidden = false;
  saveFeedback.classList.toggle("is-error", error);
  saveStatus.textContent = message;
}

async function refreshDashboard() {
  const res = await fetch(`/api/dashboards/${dashboardPath(dashboardName)}`, { cache: "no-store" });
  const latest = await res.json();
  if (!res.ok) throw new Error(errorDetail(latest, res.status));
  Object.assign(dashboard, latest.dashboard);
  queries = dashboard.queries;
  setEtag(latest.etag);
  const selected = picker.value;
  picker.replaceChildren(new Option("saved queries", ""));
  for (const name of Object.keys(queries)) picker.add(new Option(name, name));
  picker.value = Object.hasOwn(queries, selected) ? selected : "";
  picker.dispatchEvent(new CustomEvent("change", { detail: { refreshOnly: true } }));
  needsRefresh = false;
}

async function putTile(method, url, payload, failLabel) {
  if (saving || added || needsRefresh) return;
  saving = true;
  updateAddState();
  try {
    const res = await fetch(url, {
      method,
      headers: mutatingHeaders({ "Content-Type": "application/json", "If-Match": getEtag() }),
      body: JSON.stringify(payload),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      if (res.status === 409 && !editingTile) {
        needsRefresh = true;
        showSaveStatus("The dashboard changed. Review it, then refresh to add your tile. Your work is still here.", true);
      } else {
        showSaveStatus(`${failLabel}: ${errorDetail(data, res.status)}`, true);
      }
      return;
    }
    if (editingTile) {
      location.href = `/d/${dashboardPath(dashboardName)}?edit=1`;
      return;
    }
    added = true;
    setEtag(data.etag);
    showSaveStatus(payload.tile.title ? `Added “${payload.tile.title}”.` : "Tile added.");
    try {
      await refreshDashboard();
      if (payload.tile.query) {
        loadedQueryName = payload.tile.query;
        picker.value = loadedQueryName;
        picker.dispatchEvent(new CustomEvent("change", { detail:{refreshOnly:true} }));
      }
    } catch {
      needsRefresh = true;
      showSaveStatus("Tile added. Refresh the dashboard state before creating another tile.", true);
    }
  } catch (err) {
    showSaveStatus(`${failLabel}: ${err.message}`, true);
  } finally {
    saving = false;
    updateAddState();
    if (needsRefresh) refreshBtn.scrollIntoView({ block: "nearest" });
    else if (added) anotherBtn.scrollIntoView({ block: "nearest" });
  }
}

anotherBtn.addEventListener("click", () => {
  if (saving || needsRefresh) return;
  added = false;
  saveFeedback.hidden = true;
  updateAddState();
  titleInput.focus();
  titleInput.select();
});

refreshBtn.addEventListener("click", async () => {
  if (saving) return;
  saving = true;
  updateAddState();
  try {
    await refreshDashboard();
    showSaveStatus(added ? "Tile added. Ready to create another."
      : "Dashboard refreshed. Your work is unchanged; review it and add when ready.");
  } catch (err) {
    showSaveStatus(`Refresh failed: ${withoutPeriod(err.message)}. Your work is still here.`, true);
  } finally {
    saving = false;
    updateAddState();
  }
});

function assignedSource() {
  if (mode === "text") return editingTile?.source || null;
  return sourcePicker?.value || null;
}

function textPayload(title) {
  return {
    tile: {
      type: "text",
      title: title || null,
      query: null,
      metric: null,
      position: editingTile?.position ?? null,
      source: assignedSource(),
      chart: null,
      markdown: textMarkdown.value.trim(),
    },
    sql: null,
  };
}

function sqlPayload(title, queryName, sql) {
  const tile = {
    type: "chart",
    title: title || null,
    query: queryName,
    metric: null,
    position: editingTile?.position ?? null,
    source: assignedSource(),
    chart: builder.spec,
  };
  return { tile, sql };
}

function metricPayload(title) {
  const metric = currentMetricRef() ?? editingTile?.metric ?? null;
  if (!metric) return null;
  return {
    tile: {
      type: "chart",
      title: title || null,
      query: null,
      metric,
      position: editingTile?.position ?? null,
      source: assignedSource(),
      chart: builder.spec,
    },
    sql: null,
  };
}

function nextTileId(title) {
  const taken = new Set((dashboard.tiles || []).map((t) => t.id).filter(Boolean));
  const base = slugify(title, `t_${Date.now().toString(36)}`);
  if (!taken.has(base)) return base;
  let n = 2;
  while (taken.has(`${base}_${n}`)) n++;
  return `${base}_${n}`;
}

function nextQueryName(title, tileId, sql) {
  if (document.getElementById("independent-query").checked) return independentQueryName(title || loadedQueryName || tileId);
  let queryName = loadedQueryName && queries[loadedQueryName]?.trim() === sql
    ? loadedQueryName
    : slugify(title, tileId);
  const base = queryName;
  let n = 2;
  while (queries[queryName] !== undefined && queries[queryName].trim() !== sql) {
    queryName = `${base}_${n++}`;
  }
  return queryName;
}

addBtn.addEventListener("click", async () => {
  if (saving || added || needsRefresh) return;
  const title = titleInput.value.trim();
  const failLabel = editingTile ? "Save failed" : "Add failed";
  const method = editingTile ? "PUT" : "POST";
  const url = editingTile
    ? `/api/dashboards/${dashboardPath(dashboardName)}/tiles/${encodeURIComponent(editingTile.id)}`
    : `/api/dashboards/${dashboardPath(dashboardName)}/tiles`;

  const send = (payload) => {
    if (!editingTile) payload.tile.id = nextTileId(title);
    return putTile(method, url, payload, failLabel);
  };

  if (mode === "text") {
    if (!textMarkdown.value.trim()) return;
    await send(textPayload(title));
    return;
  }

  if (mode === "metric") {
    const payload = metricPayload(title);
    if (!payload?.tile?.metric) return;
    await send(payload);
    return;
  }

  const sql = editor.getValue().trim();
  if (editingTile) {
    if (!sql) return toast("SQL is required", "error");
    let queryName = editingTile.query || slugify(title, editingTile.id);
    if (sharedConsumers().length > 1 && sql !== queries[editingTile.query]?.trim()
        && document.getElementById("shared-edit-scope").value === "copy") {
      queryName = independentQueryName(queryName);
    }
    await putTile(method, url, sqlPayload(title, queryName, sql), failLabel);
    return;
  }
  if (!lastRunSql) return;
  const tileId = nextTileId(title);
  const queryName = nextQueryName(title, tileId, lastRunSql);
  const payload = sqlPayload(title, queryName, lastRunSql);
  payload.tile.id = tileId;
  await putTile(method, url, payload, failLabel);
});

runBtn.addEventListener("click", run);
cancelBtn.addEventListener("click", async () => {
  if (currentExecution) {
    await fetch(`/api/executions/${currentExecution}/cancel`, { method: "POST", headers: mutatingHeaders() });
  }
});
editor.commands.addCommand({
  name: "run",
  bindKey: { win: "Ctrl-Enter", mac: "Command-Enter" },
  exec: run,
});

// A tile may name the dashboard's default connection by its own name, which the
// picker spells as the empty "default" option: assigning it would blank the select.
function selectTileSource(name) {
  if (!name || !sourcePicker) return;
  if (sourcePicker.querySelector(`option[value="${name}"]`)) sourcePicker.value = name;
}

function loadEditingTile() {
  if (!editingTile) return;
  titleInput.value = editingTile.title ?? "";
  if (editingTile.type === "text") {
    textMarkdown.value = editingTile.markdown ?? "";
    setMode("text");
    return;
  }
  if (editingTile.metric) {
    const name = editingTile.metric.name;
    if (![...metricPicker.options].some((o) => o.value === name)) {
      addMetricOption(name, name);
    }
    metricPicker.value = name;
    rebuildMetricFields();
    selectTileSource(editingTile.source);
    setMode("metric");
    builder.setSpec(editingTile.chart ?? defaultChartSpec(editingTile));
    return;
  }
  const sql = (queries[editingTile.query] ?? "").trim();
  editor.setValue(sql, -1);
  loadedQueryName = editingTile.query ?? null;
  if (loadedQueryName && picker.querySelector(`option[value="${loadedQueryName}"]`)) {
    picker.value = loadedQueryName;
  }
  selectTileSource(editingTile.source);
  builder.setSpec(editingTile.chart ?? defaultChartSpec(editingTile));
  lastRunSql = sql || null;
  setMode("sql");
}

loadEditingTile();
if (mode === "sql") editor.focus();

window.addEventListener("sqldash:schema-refresh", loadSchema);

export function workspaceSnapshot() {
  return {
    sql: editor.getValue(), title: titleInput.value, source: sourcePicker.value,
    chart: builder.spec, mode, markdown: textMarkdown.value,
    metric: currentMetricRef(), query: loadedQueryName,
  };
}

export async function restoreWorkspace(state) {
  await metricsReady;
  if (state.query && queries[state.query]) {
    picker.value = state.query;
    picker.dispatchEvent(new Event("change", { bubbles:true }));
  }
  if (typeof state.sql === "string") editor.setValue(state.sql, -1);
  else if (!state.query) editor.setValue("", -1);
  if (typeof state.title === "string") titleInput.value = state.title;
  if (state.metric?.name) {
    if (![...metricPicker.options].some(option => option.value === state.metric.name)) {
      addMetricOption(state.metric.name, `${state.metric.name} (unavailable)`);
    }
    metricPicker.value = state.metric.name;
    metricPicker.dispatchEvent(new Event("change", { bubbles:true }));
    metricDims.querySelectorAll("input").forEach(input => { input.checked = state.metric.dimensions?.includes(input.value) || false; });
    dimensionOrder = Array.isArray(state.metric.dimensions) ? [...state.metric.dimensions] : [];
    metricGrain.value = state.metric.grain || "";
    metricGrain.dispatchEvent(new Event("change", { bubbles:true }));
  }
  textMarkdown.value = typeof state.markdown === "string" ? state.markdown : "";
  setMode(["sql", "text", "metric"].includes(state.mode) ? state.mode : "sql");
  if (state.source && ![...sourcePicker.options].some(option => option.value === state.source)) {
    try {
      const context = await requestRoles(state.source);
      addRoleSource(context);
    } catch {
      sourcePicker.add(new Option('Unavailable connection or role — choose a source', state.source));
      sourcePicker.options[sourcePicker.options.length - 1].dataset.unavailable = 'true';
    }
  }
  sourcePicker.value = state.source || "";
  sourcePicker.dispatchEvent(new Event("change", { bubbles:true }));
  if (state.chart && typeof state.chart === "object") builder.setSpec(state.chart);
  lastRunSql = null;
  clearResults();
  updateAddState();
  editor.resize();
}

export async function disposeWorkspace() {
  const running = runBtn.disabled;
  runGeneration += 1;
  const execution = currentSubmission ? await currentSubmission.catch(() => null) : currentExecution;
  if (execution && running) {
    await fetch(`/api/executions/${execution}/cancel`, { method:"POST", headers:mutatingHeaders(), keepalive:true });
  }
}

function sharedConsumers() {
  const name = editingTile?.query || loadedQueryName;
  return name ? dashboard.tiles.filter(tile => tile.query === name) : [];
}

function independentQueryName(title) {
  const base = slugify(title, "query") + "_copy";
  let name = base;
  let n = 2;
  while (Object.hasOwn(queries, name)) name = `${base}_${n++}`;
  return name;
}

function updateQuerySharing() {
  const name = editingTile?.query || loadedQueryName;
  const consumers = sharedConsumers();
  const area = document.getElementById("query-sharing");
  if (!area) return;
  area.hidden = mode !== "sql" || !name;
  const changed = Boolean(editingTile && editor.getValue().trim() !== queries[name]?.trim());
  document.getElementById("independent-query-label").hidden = Boolean(editingTile);
  document.getElementById("shared-edit-choice").hidden = !changed || consumers.length < 2;
  const titles = consumers.map(tile => tile.title || tile.id).join(", ");
  document.getElementById("query-sharing-summary").textContent =
    consumers.length ? `Query “${name}” is used by ${titles}. Chart settings apply to this tile only.`
      : `Uses saved query “${name}”.`;
}
picker.addEventListener("change", updateQuerySharing);

export function workspaceChart(spec) {
  if (spec) {
    builder.setSpec(spec);
    if (builder.result) builder.setResult(builder.result);
  }
  return structuredClone(builder.spec);
}

if (params.has("embedded")) sourcePicker.addEventListener("change", () => {
  lastRunSql = null;
  clearResults();
  updateAddState();
});
