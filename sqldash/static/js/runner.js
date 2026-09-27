import { apiToken as readApiToken } from "/static/js/token.js";
import {
  chartNumber,
  cssVar,
  escapeHtml,
  inferSpec,
  markEmptyChart,
  markTruncated,
  renderBigNumber,
  renderTable,
  setFormatConfig,
  translate,
} from "/static/js/charts.js";
import {
  activeValues,
  crossFilterChip,
  crossFilterPlan,
  dimUnpicked,
  offValue,
  picked,
  rowIsPicked,
  toggled,
} from "/static/js/crossfilter.js";
import {
  chartKeys,
  drillUrl,
  followDrill,
  initDrillCrumb,
  markDrillTile,
  matchOption,
  refreshDrillLinks,
  rowForPoint,
  tableDrillCells,
  valueKind,
} from "/static/js/drill.js";
import { paramNamesIn } from "/static/js/params.js";
import { compareWindow, currentThenPrevious, presetRange } from "/static/js/period.js";
import { looksLikeSourceFailure } from "/static/js/sourceerror.js";

export const data = JSON.parse(document.getElementById("dashboard-data").textContent);
export const dashboard = data.dashboard;
export const dashboardName = data.name;

setFormatConfig({ locale: dashboard.locale, currency: dashboard.currency });

export const apiToken = readApiToken();

export function mutatingHeaders(extra = {}) {
  return { "X-Sqldash-Token": apiToken, ...extra };
}

export const charts = new Map();
export const tileResults = new Map();
export const tilePrevResults = new Map();
export const tileExecutions = new Map();
export const tileReferenceValues = new Map();
const drillPlans = new Map(Object.entries(data.drills ?? {}));

let etag = data.etag;
export const getEtag = () => etag;
export const setEtag = (value) => { etag = value; };

let runToken = 0;
const tileRunTokens = new Map();
export let editing = false;
export const setEditing = (value) => { editing = value; };

/* Per-tile hue: a tile's card color is a pure function of its id (FNV-1a hash
   onto the 8 palette slots), so it never changes when siblings are added, removed,
   or reordered. Adjacent same-hue twins are possible and accepted — stability beats
   perfect variety. Multi-series tiles stay on slot 1 since their charts lead with
   the fixed palette order. */
function hueSlot(id) {
  let h = 2166136261;
  for (let i = 0; i < id.length; i++) {
    h ^= id.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return ((h >>> 0) % 8) + 1;
}

export const tileHueSlots = new Map();
for (const w of dashboard.tiles) {
  if (w.type === "text") continue;
  const spec = w.chart ?? { type: "table" };
  const multi = (spec.y?.length ?? 0) > 1 || spec.group_by || spec.type === "pie";
  tileHueSlots.set(w.id, multi ? 1 : hueSlot(w.id));
}

export function applyTileHues() {
  for (const [id, slot] of tileHueSlots) {
    const el = document.querySelector(`.tile[data-tile-id="${id}"]`);
    if (el) el.style.setProperty("--wcolor", `var(--series-${slot})`);
  }
}

export function filterValues() {
  const values = {};
  for (const input of document.querySelectorAll(".filter-bar [data-filter]")) {
    values[input.dataset.filter] = input.value;
  }
  return values;
}

export async function submitRun(body) {
  const res = await fetch("/api/run", {
    method: "POST",
    headers: mutatingHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ dashboard: dashboardName, ...body }),
  });
  if (!res.ok) throw new Error((await res.json()).detail ?? `HTTP ${res.status}`);
  return (await res.json()).id;
}

export async function pollExecution(id) {
  let delay = 150;
  for (;;) {
    const res = await fetch(`/api/executions/${id}`);
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail ?? `HTTP ${res.status}`);
    }
    const ex = await res.json();
    if (ex.status === "done") return ex.result;
    if (ex.status === "error") throw new Error(ex.error ?? "query failed");
    if (ex.status === "cancelled") throw new Error("query cancelled");
    await new Promise((r) => setTimeout(r, delay));
    delay = Math.min(delay * 1.6, 2000);
  }
}

function setStatus(body, html) {
  body.querySelector(".tile-status")?.remove();
  if (html !== null) {
    const status = document.createElement("div");
    status.className = "tile-status";
    status.innerHTML = html;
    body.appendChild(status);
  }
}

export function disposeTile(tileId) {
  charts.get(tileId)?.dispose();
  // Every id-keyed map, not the three that used to be listed: a survivor that
  // renumbers into this id inherits whatever was left behind, since renameTile
  // only moves an entry when the old id has one.
  for (const map of tileStateMaps()) map.delete(tileId);
  refreshSourceBanner();
}

// Every per-tile map here is keyed by id, and derived ids move when a tile is
// deleted — so a tile that keeps its chart, results or error banner has to carry
// them to its new key, or it silently loses them. runErrors is the one that bites
// hardest if missed: left under the old id it is now *another* tile's id, and the
// shared source-banner counts it against a tile that is fine.
function tileStateMaps() {
  return [
    charts,
    tileResults,
    tilePrevResults,
    tileExecutions,
    tileHueSlots,
    runErrors,
    tileReferenceValues,
  ];
}

export function renameTile(oldId, newId) {
  if (oldId === newId) return;
  for (const map of tileStateMaps()) {
    if (!map.has(oldId)) continue;
    map.set(newId, map.get(oldId));
    map.delete(oldId);
  }
}

export function defaultChartSpec(tile) {
  if (tile.metric) {
    const format = dashboard.metric_formats?.[tile.metric.name];
    let type = "big_number";
    if (tile.metric.grain) type = "area";
    else if (tile.metric.dimensions?.length) type = "table";
    return {
      type,
      ...(format ? { format } : {}),
    };
  }
  return { type: "table" };
}

export function renderTile(el, tile, result, previous = null) {
  const body = el.querySelector(".tile-body");
  const referenceRun = tileReferenceValues.get(tile.id);
  let spec = withReferenceValues(tile.chart ?? defaultChartSpec(tile), referenceRun?.values);
  tileResults.set(tile.id, result);
  setStatus(body, null);
  const csvBtn = el.querySelector('[data-action="csv"]');
  if (csvBtn && tileExecutions.has(tile.id)) {
    csvBtn.hidden = editing;
    const csvName = encodeURIComponent(tile.title || tile.id);
    csvBtn.href = `/api/executions/${tileExecutions.get(tile.id)}/csv?name=${csvName}`;
    csvBtn.title = result.truncated
      ? `Download CSV (partial: first ${result.row_count.toLocaleString()} rows)`
      : "Download CSV";
    csvBtn.setAttribute("aria-label", csvBtn.title);
  }
  const compareMode = tile.metric?.compare;
  const compareLabel = compareMode === "yoy" ? "last year" : "previous period";
  if (previous) tilePrevResults.set(tile.id, previous);
  if (spec.type === "big_number" || spec.type === "table") {
    charts.get(tile.id)?.dispose();
    charts.delete(tile.id);
    body.innerHTML = "";
    const plan = drillPlan(tile.id);
    if (spec.type === "big_number") renderBigNumber(body, spec, result);
    else {
      const cells = plan
        ? tableDrillCells(plan, result, drillContext, (m) => toast(m, "error"))
        : tableCrossFilterCells(crossPlan(tile), result);
      renderTable(body, spec, result, { onCell: cells, onRows: cells?.onRows });
    }
    if (spec.type === "big_number") {
      markTruncated(body, result);
      attachBigNumberDrill(body, tile, spec, result);
    }
    if (spec.type === "big_number" && previous && metricHasTime(tile)) {
      renderDelta(body, spec, result, previous, compareLabel);
    }
    if (spec.type === "table") {
      el.dispatchEvent(
        new CustomEvent("sqldash:table-rendered", { bubbles: true, detail: { id: tile.id } })
      );
    }
    return;
  }
  if (previous && ["line", "area", "bar"].includes(spec.type) && !spec.group_by) {
    result = mergeCompareResult(spec, result, previous, compareMode);
    spec = { ...spec, group_by: "__period" };
  }
  let mount = body.querySelector(".chart-mount");
  if (!mount) {
    body.innerHTML = "";
    mount = document.createElement("div");
    mount.className = "chart-mount";
    body.appendChild(mount);
  }
  let chart = charts.get(tile.id);
  if (!chart || chart.getDom() !== mount) {
    chart?.dispose();
    chart = echarts.init(mount, null, { renderer: "canvas" });
    charts.set(tile.id, chart);
    new ResizeObserver(() => chart.resize()).observe(mount);
  }
  const slot = tileHueSlots.get(tile.id);
  const forced = slot && slot !== 1 ? cssVar(`--series-${slot}`) : undefined;
  const option = styleCompareSeries(translate(spec, result, forced, mount.clientHeight, mount.clientWidth));
  chart.setOption(option, { notMerge: true });
  dimCrossFilteredMarks(chart, tile, spec, result);
  markEmptyChart(body, option);
  if (drillPlan(tile.id)) attachDrill(chart, mount, tile, spec, result);
  else if (crossPlan(tile)) attachCrossFilterClicks(chart, mount, tile, spec, result);
  else {
    resetChartKeys(mount);
    if (tile.cross_filter === false) chart.off("click");
    else attachCrossFilter(chart, spec, result);
  }
  // A truncated table says so; a truncated chart just drew a shorter line, and
  // a line that stops early reads as the data ending rather than the row cap.
  // Same note renderTable uses, so the two agree about the same result.
  markTruncated(body, result);
  noteReferenceErrors(body, referenceRun?.errors);
}

/* A reference the warehouse refused is left off the chart, so the tile says
   which one and why, in the same strip a truncation note uses. */
function noteReferenceErrors(body, errors) {
  if (!errors?.length) return;
  let note = body.querySelector(":scope > .truncated-note");
  if (!note) {
    note = document.createElement("div");
    note.className = "truncated-note";
    body.appendChild(note);
  }
  body.classList.add("has-truncation");
  note.classList.add("reference-note");
  note.textContent = [note.textContent, ...errors].filter(Boolean).join(" · ");
  note.title = note.textContent;
}


/* Keyed by metric name, and `__proto__` is a valid one: read own entries only. */
const ownEntry = (map, key) => (map != null && Object.hasOwn(map, key) ? map[key] : undefined);

function withReferenceValues(spec, values) {
  if (!spec.references?.some((ref) => ref?.metric)) return spec;
  const references = spec.references.map((ref) =>
    ref?.metric
      ? {
          ...ref,
          y: ownEntry(values, ref.metric) ?? null,
          format: ref.format || ownEntry(dashboard.metric_formats, ref.metric) || null,
        }
      : ref
  );
  return { ...spec, references };
}

/* A metric reference is a scalar run of that metric under the same filter
   values as the tile, so a target moves with the dashboard's date range and
   region the way a big number would. It shares `pending` with the tiles, so a
   reference to a metric a big number already shows costs no second query. A
   failed reference drops out of the chart instead of failing the tile, and
   its error comes back to be shown on the tile. */
function metricReferenceValues(tile, values, pending) {
  const names = [...new Set((tile.chart?.references ?? []).filter((r) => r?.metric).map((r) => r.metric))];
  if (!names.length) return Promise.resolve(null);
  return Promise.all(
    names.map((name) => {
      const body = { metric: name, dimensions: [], grain: null, params: values };
      const key = JSON.stringify(body);
      if (!pending.has(key)) {
        pending.set(
          key,
          submitRun(body).then(async (id) => ({ id, result: await pollExecution(id) }))
        );
      }
      return pending.get(key).then(
        ({ result }) => {
          const col = result.columns.findIndex((c) => ["integer", "float", "decimal"].includes(c.type));
          const value = col < 0 ? null : chartNumber(result.rows[0]?.[col] ?? null);
          return [name, typeof value === "number" ? value : null];
        },
        (err) => [name, null, `reference '${name}' is not drawn: ${err.message}`]
      );
    })
  ).then((runs) => ({
    values: Object.fromEntries(runs.map(([name, value]) => [name, value])),
    errors: runs.map((run) => run[2]).filter(Boolean),
  }));
}

function daterangeBinds() {
  const filter = dashboard.filters.find((f) => f.type === "daterange");
  return filter ? filter.bind : null;
}

function metricHasTime(tile) {
  const name = tile.metric?.name;
  return Boolean(name && dashboard.metric_has_time?.[name]);
}

function mergeCompareResult(spec, current, previous, mode) {
  const merged = {
    columns: [...current.columns, { name: "__period", type: "string" }],
    rows: [],
    unshifted: [],
    row_count: 0,
    truncated: current.truncated || previous.truncated,
  };
  const timeIdx = current.columns.findIndex(
    (c) => c.type === "timestamp" || c.type === "date"
  );
  for (const row of current.rows) {
    merged.rows.push([...row, "current"]);
    merged.unshifted.push(merged.rows.at(-1));
  }
  for (const row of previous.rows) {
    const shifted = [...row];
    if (timeIdx >= 0 && shifted[timeIdx] != null) {
      const iso = String(shifted[timeIdx]).slice(0, 10);
      const cur = new Date(`${iso}T00:00:00Z`);
      if (mode === "yoy") cur.setUTCFullYear(cur.getUTCFullYear() + 1);
      else {
        const span =
          Math.round(
            (new Date(`${compareState.end}T00:00:00Z`) -
              new Date(`${compareState.start}T00:00:00Z`)) / 86400000
          ) + 1;
        cur.setUTCDate(cur.getUTCDate() + span);
      }
      shifted[timeIdx] = cur.toISOString().slice(0, 10);
    }
    merged.rows.push([...shifted, "previous"]);
    merged.unshifted.push([...row, "previous"]);
  }
  merged.row_count = merged.rows.length;
  return merged;
}

const compareState = { start: null, end: null };

function styleCompareSeries(option) {
  for (const series of option.series ?? []) {
    if (series.name === "previous") {
      series.lineStyle = { ...(series.lineStyle ?? {}), type: "dashed", opacity: 0.55 };
      series.itemStyle = { ...(series.itemStyle ?? {}), opacity: 0.55 };
      if (series.areaStyle) series.areaStyle = { ...series.areaStyle, opacity: 0.06 };
      series.emphasis = { ...(series.emphasis ?? {}), disabled: false };
    }
  }
  return option;
}

function renderDelta(body, spec, current, previous, label) {
  const valueCol = inferSpec({ ...spec }, current).value;
  const idx = current.columns.findIndex((c) => c.name === valueCol);
  const cur = current.rows[0]?.[idx];
  const prev = previous.rows[0]?.[idx];
  if (cur == null || prev == null || Number(prev) === 0) return;
  const delta = (Number(cur) - Number(prev)) / Math.abs(Number(prev));
  if (!Number.isFinite(delta)) return;
  const el = document.createElement("div");
  el.className = `bn-delta ${delta >= 0 ? "up" : "down"}`;
  el.textContent = `${delta >= 0 ? "▲" : "▼"} ${(Math.abs(delta) * 100).toFixed(1)}% vs ${label}`;
  body.querySelector(".big-number")?.appendChild(el);
}

export function drillPlan(tileId) {
  return drillPlans.get(tileId) ?? null;
}

function filterKinds() {
  const kinds = {};
  for (const select of document.querySelectorAll(".filter-bar select[data-filter]")) {
    const kind = select.selectedOptions[0]?.dataset.kind;
    if (kind) kinds[select.dataset.filter] = kind;
  }
  return kinds;
}

function drillContext() {
  return { dashboardName, filters: filterValues(), kinds: filterKinds(), search: location.search };
}

function drillFromPoint(plan, spec, result, point, event) {
  if (plan.errors.length) {
    toast(plan.errors[0], "error");
    return;
  }
  const drawn = rowForPoint(spec, result, point);
  const row = result.unshifted?.[result.rows.indexOf(drawn)] ?? drawn;
  const { href, error } = drillUrl(plan, row, result.columns, drillContext());
  if (!href) {
    toast(error, "error");
    return;
  }
  followDrill(plan, href, Boolean(event?.metaKey || event?.ctrlKey));
}

function attachDrill(chart, mount, tile, spec, result) {
  const plan = drillPlan(tile.id);
  chart.off("click");
  chart.on("click", (params) => {
    if (params.componentType !== "series") return;
    drillFromPoint(plan, spec, result, params, params.event?.event);
  });
  const what = tile.title || "this chart";
  chartKeys(
    mount,
    chart,
    `${what}. Arrow keys pick a point, Enter opens ${plan.title}`,
    (point, event) => drillFromPoint(plan, spec, result, point, event)
  );
}

function resetChartKeys(mount) {
  mount.removeAttribute("tabindex");
  mount.removeAttribute("role");
  mount.removeAttribute("aria-label");
  mount.onkeydown = null;
  mount.onblur = null;
}

function attachBigNumberDrill(body, tile, spec, result) {
  const plan = drillPlan(tile.id);
  const box = body.querySelector(".big-number");
  if (!plan || !box) return;
  box.classList.add("is-drill");
  box.tabIndex = 0;
  box.setAttribute("role", "link");
  box.setAttribute("aria-label", `Open ${plan.title}`);
  const go = (event) => drillFromPoint(plan, spec, result, { dataIndex: 0 }, event);
  box.addEventListener("click", go);
  box.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      go(e);
    }
  });
}

export function markTileClicks() {
  for (const tile of dashboard.tiles) {
    const el = document.querySelector(`.tile[data-tile-id="${CSS.escape(tile.id)}"]`);
    if (!el) continue;
    markDrillTile(el, drillPlan(tile.id));
    markCrossFilterTile(el, tile);
  }
}

function crossPlan(tile) {
  return crossFilterPlan(tile, dashboard.filters);
}

function filterInput(name) {
  return document.querySelector(`.filter-bar [data-filter="${CSS.escape(name)}"]`);
}

function crossOffs(plan) {
  const offs = {};
  for (const { name, def } of plan.entries) {
    const input = filterInput(name);
    const options = input?.tagName === "SELECT" ? [...input.options].map((o) => o.value) : [];
    offs[name] = offValue(def, options);
  }
  return offs;
}

function crossActive(plan) {
  return plan.errors.length ? null : activeValues(plan, filterValues(), crossOffs(plan));
}

function setFilters(next) {
  for (const [name, value] of Object.entries(next)) {
    const input = filterInput(name);
    if (!input || input.value === value) continue;
    const before = input.value;
    input.value = value;
    if (input.value !== value) {
      input.value = before;
      toast(`${filterLabel(name)} has no '${value}' to filter to`, "error");
      continue;
    }
    input.dispatchEvent(new Event("change", { bubbles: true }));
  }
}

function crossFilterFrom(plan, row, columns) {
  if (plan.errors.length) {
    toast(plan.errors[0], "error");
    return;
  }
  const { values, error } = picked(plan, row, columns);
  if (error) {
    toast(error, "error");
    return;
  }
  setFilters(toggled(plan, values, filterValues(), crossOffs(plan)));
}

function attachCrossFilterClicks(chart, mount, tile, spec, result) {
  const plan = crossPlan(tile);
  const pick = (point) => crossFilterFrom(plan, rowForPoint(spec, result, point), result.columns);
  chart.off("click");
  chart.on("click", (params) => {
    if (params.componentType === "series") pick(params);
  });
  const labels = plan.entries.map(({ def }) => def.label || def.name).join(" and ");
  const what = tile.title || "this chart";
  chartKeys(mount, chart, `${what}. Arrow keys pick a point, Enter filters by ${labels}`, pick);
}

function dimCrossFilteredMarks(chart, tile, spec, result) {
  const plan = crossPlan(tile);
  const active = plan && crossActive(plan);
  if (!active) return;
  chart.setOption({ series: dimUnpicked(chart.getOption(), spec, result, plan, active) });
}

function tableCrossFilterCells(plan, result) {
  if (!plan || plan.errors.length) return undefined;
  const column = plan.entries[0]?.column;
  const at = result.columns.findIndex((c) => c.name === column);
  if (at < 0) {
    toast(`cross_filter column '${column}' is not in this tile's result`, "error");
    return undefined;
  }
  const active = crossActive(plan);
  const labels = plan.entries.map(({ def }) => def.label || def.name).join(" and ");
  return (td, row, index) => {
    if (index !== at) return;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "cell-filter";
    button.append(...td.childNodes);
    td.replaceChildren(button);
    td.classList.add("has-filter");
    td.tabIndex = -1;
    const isPicked = Boolean(active) && rowIsPicked(plan, row, result.columns, active);
    td.classList.toggle("is-picked", isPicked);
    button.setAttribute("aria-pressed", String(isPicked));
    button.title = isPicked ? `Clear ${labels}` : `Filter by ${labels}`;
    button.addEventListener("click", (e) => {
      e.stopPropagation();
      crossFilterFrom(plan, row, result.columns);
    });
    button.addEventListener("keydown", (e) => e.stopPropagation());
  };
}

function markCrossFilterTile(el, tile) {
  const head = el.querySelector(".tile-head");
  head?.querySelector(".tile-xf")?.remove();
  const plan = crossPlan(tile);
  if (!head || !plan) return;
  const active = crossActive(plan);
  const chip = crossFilterChip(plan, active);
  if (active) {
    chip.addEventListener("click", () => setFilters(crossOffs(plan)));
  }
  head.querySelector(".tile-actions")?.before(chip);
}

function refreshCrossFilterTiles(affected) {
  for (const tile of dashboard.tiles) {
    if (!crossPlan(tile)) continue;
    const el = document.querySelector(`.tile[data-tile-id="${CSS.escape(tile.id)}"]`);
    if (!el) continue;
    markCrossFilterTile(el, tile);
    const result = tileResults.get(tile.id);
    if (result && !affected.has(tile.id)) renderTile(el, tile, result, tilePrevResults.get(tile.id));
  }
}

const CROSS_FILTER_TYPES = new Set(["line", "bar", "area", "scatter", "pie"]);

function attachCrossFilter(chart, spec, result) {
  chart.off("click");
  const inferred = inferSpec({ ...spec, y: spec.y ? [...spec.y] : spec.y }, result);
  if (!CROSS_FILTER_TYPES.has(inferred.type)) return;
  const column = inferred.type === "pie" ? inferred.label : inferred.x;
  if (!column) return;
  const select = document.querySelector(
    `.filter-bar select[data-filter="${CSS.escape(column)}"]`
  );
  if (!select) return;
  chart.on("click", (params) => {
    if (params.componentType !== "series") return;
    const value = String(params.name);
    const options = [...select.options].map((o) => o.value);
    if (!options.includes(value)) return;
    const reset = options.includes("all") ? "all" : options[0];
    select.value = select.value === value ? reset : value;
    select.dispatchEvent(new Event("change", { bubbles: true }));
  });
}

const runErrors = new Map();

// The driver's wording for one dead source is not stable: libpq reports a
// refused connect as "Connection refused\n\tIs the server running..." or as
// "could not receive data from server: Connection refused" depending on which
// syscall saw the RST. Grouping on the exact text split one outage into two
// singletons and the banner never rose. Source-class failures group by source.
function errorGroup(message, source) {
  return looksLikeSourceFailure(message) ? `source:${source ?? ""}` : `message:${message}`;
}

function sharedSourceGroup() {
  const counts = new Map();
  for (const { group } of runErrors.values()) counts.set(group, (counts.get(group) ?? 0) + 1);
  const found = [...counts].find(([group, n]) => n >= 2 && group.startsWith("source:"));
  if (!found) return null;
  const members = [...runErrors].filter(([, e]) => e.group === found[0]);
  return { group: found[0], message: members[0][1].message, ids: members.map(([id]) => id) };
}

export function noteRunError(tileId, message, source) {
  runErrors.set(tileId, { message, group: errorGroup(message, source) });
  const shared = sharedSourceGroup();
  if (!shared || !shared.ids.includes(tileId)) {
    refreshSourceBanner();
    return false;
  }
  let banner = document.getElementById("source-banner");
  if (!banner) {
    banner = document.createElement("div");
    banner.id = "source-banner";
    banner.className = "source-banner";
    banner.innerHTML =
      '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" ' +
      'stroke-width="1.7" stroke-linecap="round"><circle cx="12" cy="12" r="9"/>' +
      '<path d="M12 8v4M12 16h.01"/></svg><div><div class="sb-message"></div>' +
      '<div class="sb-hint">every affected tile failed to reach the same source, so this ' +
      "is a source problem, not a query problem. try: sqldash source test</div></div>";
    const grid = document.querySelector(".grid-stack") ?? document.querySelector("main");
    grid?.parentNode?.insertBefore(banner, grid);
  }
  banner.querySelector(".sb-message").textContent = shared.message;
  for (const id of shared.ids) {
    const el = document.querySelector(`.tile[data-tile-id="${id}"] .tile-body`);
    if (el?.querySelector(".tile-status .err")) {
      el.querySelector(".tile-status").innerHTML =
        '<div class="err-quiet">source error, see banner above</div>';
    }
  }
  return true;
}

export function clearRunError(tileId) {
  runErrors.delete(tileId);
  // One of two shared-source tiles recovering must drop the banner (or update
  // it): "every affected tile" is false the moment count falls below 2.
  refreshSourceBanner();
}

export function refreshSourceBanner() {
  // Raise is gated on source-class failures; dismiss must be too, and it must
  // be recomputed on delete and recovery, not only when a new error arrives.
  // A filter re-run that turns "connection refused" into a shared missing
  // table must not leave the banner claiming a source problem.
  const shared = sharedSourceGroup();
  const banner = document.getElementById("source-banner");
  if (shared) {
    banner?.querySelector(".sb-message")?.replaceChildren(shared.message);
    return;
  }
  banner?.remove();
  // Tiles sharing an error were quieted to "see banner above". Once the banner
  // goes, that line points at nothing, so hand each one its own error back.
  for (const [id, { message }] of runErrors) {
    const status = document.querySelector(`.tile[data-tile-id="${id}"] .tile-status`);
    if (status?.querySelector(".err-quiet")) {
      status.innerHTML = `<div class="err">${escapeHtml(message)}</div>`;
    }
  }
}

export async function runTiles(tiles) {
  const token = ++runToken;
  const values = filterValues();
  const pending = new Map();

  for (const tile of tiles) {
    const el = document.querySelector(`.tile[data-tile-id="${tile.id}"]`);
    if (!el || (!tile.query && !tile.metric)) continue;
    tileRunTokens.set(tile.id, token);
    const current = () => tileRunTokens.get(tile.id) === token;
    const body = el.querySelector(".tile-body");
    if (!tileResults.has(tile.id)) {
      const bones = Array.from({ length: 7 }, (_, i) => {
        const h = 30 + ((i * 37) % 55);
        return `<div class="bone" style="height:${h}%; animation-delay:${i * 0.08}s"></div>`;
      }).join("");
      setStatus(body, `<div class="skeleton"><div class="bones">${bones}</div></div>`);
    }

    let runBody;
    if (tile.metric) {
      runBody = {
        metric: tile.metric.name,
        dimensions: tile.metric.dimensions ?? [],
        grain: tile.metric.grain ?? null,
        params: values,
      };
      if (tile.source) runBody.source = tile.source;
    } else {
      const sql = dashboard.queries[tile.query] ?? "";
      const params = {};
      for (const name of paramNamesIn(sql)) {
        if (values[name] !== undefined && values[name] !== "") params[name] = values[name];
      }
      runBody = { query: tile.query, params, source: tile.source ?? "" };
    }
    let comparePromise = null;
    let compareError = null;
    if (tile.metric?.compare && metricHasTime(tile)) {
      const mode = tile.metric.compare;
      const binds = daterangeBinds();
      const window = binds && compareWindow(mode, values[binds.start], values[binds.end]);
      if (!binds) {
        compareError = `compare '${mode}' needs a time range but the dashboard has no daterange filter — add one, or omit compare`;
      } else if (!window) {
        compareError = `compare '${mode}' needs a time range — pick a start and end date`;
      } else {
        compareState.start = values[binds.start];
        compareState.end = values[binds.end];
        const prevBody = {
          ...runBody,
          params: { ...runBody.params, [binds.start]: window.start, [binds.end]: window.end },
        };
        comparePromise = submitRun(prevBody).then((id) => pollExecution(id));
      }
    }

    const key = JSON.stringify(runBody);
    if (!compareError && !pending.has(key)) {
      pending.set(
        key,
        submitRun(runBody).then(async (id) => {
          const result = await pollExecution(id);
          return { id, result };
        })
      );
    }
    const settled = compareError
      ? Promise.reject(new Error(compareError))
      : currentThenPrevious(pending.get(key), comparePromise);
    const references = metricReferenceValues(tile, values, pending);
    Promise.all([settled, references])
      .then(([[{ id, result }, previous], referenceValues]) => {
        if (current()) {
          tileReferenceValues.set(tile.id, referenceValues);
          tileExecutions.set(tile.id, id);
          clearRunError(tile.id);
          renderTile(el, tile, result, previous);
        }
      })
      .catch((err) => {
        if (current()) {
          if (compareError) clearRenderedTile(el, tile.id);
          if (noteRunError(tile.id, err.message, tile.source)) {
            setStatus(body, '<div class="err-quiet">source error, see banner above</div>');
          } else {
            setStatus(body, `<div class="err">${escapeHtml(err.message)}</div>`);
          }
        }
      });
  }
}

function clearRenderedTile(el, tileId) {
  charts.get(tileId)?.dispose();
  for (const map of [charts, tileResults, tilePrevResults, tileExecutions, tileReferenceValues]) {
    map.delete(tileId);
  }
  const csvBtn = el.querySelector('[data-action="csv"]');
  if (csvBtn) csvBtn.hidden = true;
  const body = el.querySelector(".tile-body");
  body.classList.remove("has-truncation");
  body.innerHTML = "";
}

export function tilesUsingParam(name) {
  return dashboard.tiles.filter((w) => {
    if (w.metric || w.chart?.references?.some((ref) => ref?.metric)) return true;
    const sql = w.query ? dashboard.queries[w.query] ?? "" : "";
    return paramNamesIn(sql).includes(name);
  });
}

export function rerenderChartsForTheme() {
  for (const tile of dashboard.tiles) {
    const result = tileResults.get(tile.id);
    const el = document.querySelector(`.tile[data-tile-id="${tile.id}"]`);
    if (result && el) renderTile(el, tile, result, tilePrevResults.get(tile.id));
  }
}

export function toast(message, kind = "info") {
  const host = document.getElementById("toasts");
  if (!host) return;
  const el = document.createElement("div");
  el.className = `toast toast-${kind}`;
  el.textContent = message;
  host.appendChild(el);
  setTimeout(() => {
    el.classList.add("out");
    setTimeout(() => el.remove(), 300);
  }, 4200);
}

function semanticLayerEventName(name) {
  const slash = name.indexOf("/");
  return slash === -1 ? "metrics" : `${name.slice(0, slash)}/metrics`;
}

export function connectEvents(onExternalChange) {
  const semanticLayerEvent = semanticLayerEventName(dashboardName);
  // The last revision reported for each file, starting from what this page
  // rendered. `repeat` marks a revision that is reported again, e.g. by the
  // `ready` after a reconnect, so a caller that only informs (an open editor)
  // can say it once. It is a hint, not a filter: an event can be lost, so a
  // caller that refreshes must still refresh.
  const last = { [dashboardName]: etag, [semanticLayerEvent]: data.metrics_etag ?? null };
  const report = (name, revision, payload) => {
    const repeat = Boolean(revision) && revision === last[name];
    last[name] = revision ?? null;
    onExternalChange({ ...payload, repeat });
  };
  const source = new EventSource(`/api/events?name=${encodeURIComponent(dashboardName)}`);
  // `ready` arrives on every (re)connect, once the server is subscribed, with
  // the current revisions. One this page does not have was written while
  // nobody was listening: between render and subscribe, or during an outage (#577).
  source.addEventListener("ready", (event) => {
    try {
      const current = JSON.parse(event.data);
      if (current.name !== dashboardName) return;
      if (current.etag !== etag) {
        report(dashboardName, current.etag, { type: "changed", name: dashboardName, etag: current.etag, missed: true });
      }
      if (current.metrics_etag !== last[semanticLayerEvent]) {
        report(semanticLayerEvent, current.metrics_etag, { type: "changed", name: semanticLayerEvent, etag: current.metrics_etag, missed: true });
      }
    } catch {
      /* ignore a malformed handshake; the next reconnect retries it */
    }
  });
  source.onmessage = (event) => {
    try {
      const payload = JSON.parse(event.data);
      if (payload.name !== dashboardName && payload.name !== semanticLayerEvent) return;
      if (payload.name === dashboardName && payload.etag && payload.etag === etag) return;
      report(payload.name, payload.etag, payload);
    } catch {
      /* ignore malformed events */
    }
  };
  return source;
}

/* ---------- filters: presets, URL state, debounced re-runs ---------- */

/* The day every preset in the filter bar resolves against, sent by the server with
   the page. The client clock is not a substitute: the server resolved the dashboard's
   own default against its local date and stamped that window into the inputs, so a
   browser resolving the same token against its UTC date would run a different window
   than the API, the CLI and MCP for the same dashboard (#673). */
let serverToday = data.today ?? null;

const pendingParams = new Set();
let filterFlush = null;

function queueFilterRun(paramName) {
  pendingParams.add(paramName);
  clearTimeout(filterFlush);
  filterFlush = setTimeout(() => {
    const affected = new Map();
    for (const name of pendingParams) {
      for (const w of tilesUsingParam(name)) affected.set(w.id, w);
    }
    pendingParams.clear();
    syncFiltersToUrl();
    refreshDrillLinks();
    runTiles([...affected.values()]);
    refreshCrossFilterTiles(affected);
  }, 60);
}

function syncFiltersToUrl() {
  const url = new URL(location);
  for (const input of document.querySelectorAll(".filter-bar [data-filter]")) {
    const key = `f_${input.dataset.filter}`;
    if (input.value) url.searchParams.set(key, input.value);
    else url.searchParams.delete(key);
  }
  history.replaceState(null, "", url);
}

function filterLabel(bind) {
  const f = dashboard.filters.find(
    (d) => d.name === bind || (d.bind && Object.values(d.bind).includes(bind))
  );
  return f?.label || f?.name || bind;
}

function refuseUrlValue(bind, value) {
  const label = filterLabel(bind);
  toast(`${label} has no '${value}' to filter to, so it is showing its default`, "error");
}

function selectOptions(select) {
  return [...select.options].map((o) => ({ value: o.value, kind: o.dataset.kind || "string" }));
}

function selectValue(select, value) {
  return matchOption(selectOptions(select), value, null) ?? value;
}

export function applyFiltersFromUrl() {
  const params = new URLSearchParams(location.search);
  let any = false;
  for (const input of document.querySelectorAll(".filter-bar [data-filter]")) {
    const asked = params.get(`f_${input.dataset.filter}`);
    const value = asked !== null && input.tagName === "SELECT" ? selectValue(input, asked) : asked;
    if (value !== null && value !== input.value) {
      const before = input.value;
      input.value = value;
      if (input.value !== value && !input.dataset.optionsSql) {
        input.value = before;
        refuseUrlValue(input.dataset.filter, value);
        continue;
      }
      any = true;
    }
  }
  return any;
}

function bindFilterInputs() {
  document.querySelectorAll(".filter-bar [data-filter]").forEach((input) => {
    input.addEventListener("change", () => queueFilterRun(input.dataset.filter));
  });
}

async function loadFilterOptions() {
  const pendingSelects = document.querySelectorAll(
    '.filter-bar select[data-filter][data-options-sql]'
  );
  for (const select of pendingSelects) {
    const name = select.dataset.filter;
    try {
      const id = await submitRun({ filter_options: name });
      const result = await pollExecution(id);
      const seen = new Set(["all"]);
      const values = [];
      for (const row of result.rows) {
        const v = row[0] == null ? null : String(row[0]);
        if (v && !seen.has(v)) {
          seen.add(v);
          values.push(v);
        }
      }
      const authored = select.dataset.default;
      const asked = new URLSearchParams(location.search).get(`f_${name}`);
      const desired = asked ?? (authored || select.value);
      select.innerHTML = "";
      const kind = valueKind(result.columns[0]?.type);
      for (const v of ["all", ...values]) {
        const opt = document.createElement("option");
        opt.value = v;
        opt.dataset.kind = v === "all" ? "string" : kind;
        opt.textContent = v;
        select.appendChild(opt);
      }
      const option = matchOption(selectOptions(select), desired, null);
      if (option !== undefined) select.value = option;
      else if (asked !== null) refuseUrlValue(name, asked);
      if (select.value !== "all" ) {
        select.dispatchEvent(new Event("change", { bubbles: true }));
      }
    } catch (err) {
      // semgrep: a console message, not a format string anyone parses
      // nosemgrep: unsafe-formatstring
      console.warn(`options_sql for filter '${name}' failed:`, err.message);
    }
  }
}
export function initFilters() {
  bindFilterInputs();
  loadFilterOptions();
  document.querySelectorAll("[data-daterange]").forEach((control) => {
    const preset = control.querySelector(".dr-preset");
    const [startInput, endInput] = control.querySelectorAll(".dr-date");

    const showDates = (custom) => {
      startInput.hidden = !custom;
      endInput.hidden = !custom;
    };

    /* Only a preset the user picks is resolved here. The default one arrives
       already resolved, in the inputs' stamped values. */
    const applyPreset = (value) => {
      showDates(value === "custom");
      const range = presetRange(value, serverToday);
      if (!range) return;
      startInput.value = range.start;
      endInput.value = range.end;
      queueFilterRun(startInput.dataset.filter);
      queueFilterRun(endInput.dataset.filter);
    };

    const defaultPreset = control.dataset.defaultPreset || "custom";
    if (![...preset.options].some((o) => o.value === defaultPreset)) {
      const opt = document.createElement("option");
      opt.value = defaultPreset;
      opt.textContent = defaultPreset.replace(/_/g, " ");
      preset.insertBefore(opt, preset.firstChild);
    }
    preset.value = defaultPreset;
    showDates(defaultPreset === "custom");
    preset.addEventListener("change", () => applyPreset(preset.value));
  });

  if (applyFiltersFromUrl()) {
    document.querySelectorAll("[data-daterange]").forEach((control) => {
      const preset = control.querySelector(".dr-preset");
      const inputs = control.querySelectorAll(".dr-date");
      const params = new URLSearchParams(location.search);
      if ([...inputs].some((i) => params.has(`f_${i.dataset.filter}`))) {
        const [start, end] = [...inputs].map((i) => i.value);
        const same = [...preset.options].find((o) => {
          const range = presetRange(o.value, serverToday);
          return range && range.start === start && range.end === end;
        });
        preset.value = same ? same.value : "custom";
        inputs.forEach((i) => (i.hidden = Boolean(same)));
      }
    });
  }

}
initFilters();
document.querySelector(".filter-bar")?.addEventListener("submit", (e) => e.preventDefault());

export function replaceDashboard(next) {
  tileRunTokens.clear();
  clearTimeout(filterFlush);
  pendingParams.clear();
  for (const tile of dashboard.tiles) disposeTile(tile.id);
  for (const key of Object.keys(dashboard)) delete dashboard[key];
  // semgrep: next.dashboard is this server's own same-origin reply
  // nosemgrep: insecure-object-assign
  Object.assign(dashboard, next.dashboard);
  setEtag(next.etag);
  drillPlans.clear();
  for (const [id, plan] of Object.entries(next.drills ?? {})) drillPlans.set(id, plan);
  if (next.today) serverToday = next.today;
  setFormatConfig({ locale: dashboard.locale, currency: dashboard.currency });
  configureRefresh();
  for (const tile of dashboard.tiles) {
    if (tile.type === "text") continue;
    const spec = tile.chart ?? { type: "table" };
    const multi = (spec.y?.length ?? 0) > 1 || spec.group_by || spec.type === "pie";
    tileHueSlots.set(tile.id, multi ? 1 : hueSlot(tile.id));
  }
}

window.addEventListener("sqldash:themechange", rerenderChartsForTheme);

function parseRefresh(s) {
  const m = /^(\d+)(s|m|h)$/.exec(s ?? "");
  if (!m) return null;
  return Number(m[1]) * { s: 1000, m: 60000, h: 3600000 }[m[2]];
}

let refreshInterval;
function configureRefresh() {
  clearInterval(refreshInterval);
  const refreshMs = parseRefresh(dashboard.refresh);
  if (refreshMs) {
    refreshInterval = setInterval(() => {
      if (!editing) runTiles(dashboard.tiles);
    }, Math.max(refreshMs, 5000));
  }
}
configureRefresh();

applyTileHues();
markTileClicks();
initDrillCrumb();

import("/static/js/dropdown.js").then(({ enhanceSelects }) => enhanceSelects());
