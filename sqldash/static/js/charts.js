import { heatmapOption } from "./heatmap.js";

const NUMERIC_TYPES = new Set(["integer", "float", "decimal"]);
const TEMPORAL_TYPES = new Set(["date", "timestamp"]);

export function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
}

export function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

export function humanize(name) {
  return String(name ?? "").replace(/_/g, " ");
}

export function palette() {
  return [1, 2, 3, 4, 5, 6, 7, 8].map((i) => cssVar(`--series-${i}`));
}

const CURRENCY_CODE = /^[A-Z]{3}$/;
let formatConfig = { locale: undefined, currency: "USD" };

export function setFormatConfig({ locale, currency } = {}) {
  formatConfig = {
    locale: locale || undefined,
    currency: currency || "USD",
  };
}

function isPlainRecord(value) {
  if (Array.isArray(value)) return true;
  return Object.getPrototypeOf(value) === Object.prototype;
}

export function inspectText(value) {
  if (value === null || value === undefined) {
    return { display: "null", inspect: "null", pretty: false };
  }
  if (typeof value === "object" && isPlainRecord(value)) {
    return { display: JSON.stringify(value), inspect: JSON.stringify(value, null, 2), pretty: true };
  }
  const text = String(value);
  const trimmed = text.trim();
  if (
    (trimmed.startsWith("{") && trimmed.endsWith("}")) ||
    (trimmed.startsWith("[") && trimmed.endsWith("]"))
  ) {
    try {
      return { display: text, inspect: JSON.stringify(JSON.parse(trimmed), null, 2), pretty: true };
    } catch {
      /* not JSON */
    }
  }
  return { display: text, inspect: text, pretty: false };
}

let openCellPop = null;
let openCellAnchor = null;
let openCellScroll = null;

function closeCellPop() {
  if (openCellScroll) {
    openCellScroll.target.removeEventListener("scroll", openCellScroll.fn);
    openCellScroll = null;
  }
  if (!openCellPop) return;
  openCellPop.remove();
  openCellPop = null;
  openCellAnchor = null;
}

function cellHost(anchor) {
  return anchor.closest(".tile") || document.body;
}

function placeCellPop(pop, anchor) {
  const pad = 8;
  const host = pop.parentElement || cellHost(anchor);
  const inTile = host !== document.body;
  const rect = anchor.getBoundingClientRect();
  const boundW = inTile ? host.clientWidth : window.innerWidth;
  const boundH = inTile ? host.clientHeight : window.innerHeight;
  const origin = inTile ? host.getBoundingClientRect() : { left: 0, top: 0 };
  const originX = inTile ? origin.left + host.clientLeft : 0;
  const originY = inTile ? origin.top + host.clientTop : 0;
  const maxW = Math.min(520, boundW - pad * 2);
  const width = Math.min(Math.max(rect.width, 280), Math.max(maxW, 0));
  pop.style.width = `${Math.round(width)}px`;
  let left = rect.left - originX;
  if (left + width > boundW - pad) left = boundW - width - pad;
  if (left < pad) left = pad;
  const cellTop = rect.top - originY;
  const cellBottom = rect.bottom - originY;
  const below = boundH - pad - cellTop;
  const above = cellBottom - pad;
  const flip = below < 96 && above > below;
  const maxH = Math.max(96, flip ? above : below);
  pop.style.maxHeight = `${Math.round(maxH)}px`;
  let top = flip ? cellBottom - pop.offsetHeight : cellTop;
  if (top < pad) top = pad;
  if (top + pop.offsetHeight > boundH - pad) {
    top = Math.max(pad, boundH - pop.offsetHeight - pad);
  }
  pop.style.left = `${Math.round(left)}px`;
  pop.style.top = `${Math.round(top)}px`;
}

function cellInHost(anchor, host) {
  const a = anchor.getBoundingClientRect();
  const h = host.getBoundingClientRect();
  return a.bottom > h.top && a.top < h.bottom && a.right > h.left && a.left < h.right;
}

function placeOpenCellPop() {
  if (!openCellPop || !openCellAnchor?.isConnected) return;
  const host = openCellPop.parentElement;
  if (host && host !== document.body && !cellInHost(openCellAnchor, host)) {
    closeCellPop();
    return;
  }
  placeCellPop(openCellPop, openCellAnchor);
}

function showCellPop(anchor, text, pretty) {
  closeCellPop();
  const pop = document.createElement("div");
  pop.className = "cell-pop";
  pop.setAttribute("role", "dialog");
  pop.setAttribute("aria-label", "Cell value");
  const copy = document.createElement("button");
  copy.type = "button";
  copy.className = "cell-pop-copy";
  copy.setAttribute("aria-label", "Copy");
  copy.title = "Copy";
  copy.innerHTML =
    '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" aria-hidden="true"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>';
  copy.addEventListener("click", async (e) => {
    e.stopPropagation();
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      try {
        const ta = document.createElement("textarea");
        ta.value = text;
        document.body.appendChild(ta);
        ta.select();
        if (!document.execCommand("copy")) throw new Error("copy");
        ta.remove();
      } catch {
        copy.setAttribute("aria-label", "Copy failed");
        setTimeout(() => {
          if (copy.isConnected) copy.setAttribute("aria-label", "Copy");
        }, 1200);
        return;
      }
    }
    copy.classList.add("ok");
    copy.setAttribute("aria-label", "Copied");
    setTimeout(() => {
      if (!copy.isConnected) return;
      copy.classList.remove("ok");
      copy.setAttribute("aria-label", "Copy");
    }, 1200);
  });
  const body = document.createElement(pretty ? "pre" : "p");
  body.className = "cell-pop-body" + (pretty ? " is-pretty" : "");
  body.textContent = text;
  pop.appendChild(copy);
  pop.appendChild(body);
  pop.addEventListener("mousedown", (e) => e.stopPropagation());
  const host = cellHost(anchor);
  if (host === document.body) pop.style.position = "fixed";
  host.appendChild(pop);
  openCellPop = pop;
  openCellAnchor = anchor;
  placeCellPop(pop, anchor);
  requestAnimationFrame(placeOpenCellPop);
  const scroller = anchor.closest(".table-wrap");
  if (scroller) {
    scroller.addEventListener("scroll", placeOpenCellPop, { passive: true });
    openCellScroll = { target: scroller, fn: placeOpenCellPop };
  }
}

if (typeof document !== "undefined") {
  document.addEventListener("click", (e) => {
    if (openCellPop && !openCellPop.contains(e.target) && !e.target.closest("td.cell-inspect")) {
      closeCellPop();
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || !openCellPop) return;
    const el = document.activeElement;
    if (
      el &&
      !openCellPop.contains(el) &&
      (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT" || el.isContentEditable)
    ) {
      return;
    }
    closeCellPop();
  });
  window.addEventListener("resize", placeOpenCellPop);
}

/* Integers past 2^53 and decimals arrive as text so the browser never rounds
   them through a double. Intl.NumberFormat formats a decimal string exactly, so
   tables and big numbers hand it the text; charts plot `chartNumber` instead. */
const DECIMAL_TEXT = /^([+-]?)(\d*)(?:\.(\d*))?(?:[eE]([+-]?\d{1,3}))?$/;

export function exactDecimal(value) {
  if (typeof value !== "string") return null;
  const m = DECIMAL_TEXT.exec(value.trim());
  if (!m || !(m[2] || m[3])) return null;
  let digits = (m[2] ?? "") + (m[3] ?? "");
  let point = (m[2] ?? "").length + Number(m[4] ?? 0);
  if (point < 0) {
    digits = "0".repeat(-point) + digits;
    point = 0;
  }
  digits = digits.padEnd(point, "0");
  const int = digits.slice(0, point).replace(/^0+/, "") || "0";
  const frac = digits.slice(point).replace(/0+$/, "");
  const neg = m[1] === "-" && (int !== "0" || frac !== "");
  return { neg, int, frac, text: `${neg ? "-" : ""}${int}${frac ? `.${frac}` : ""}` };
}

/* A float the warehouse returned as NaN or ±Infinity arrives as this text,
   since JSON has no spelling for them. */
const NON_FINITE = new Map([
  ["NaN", NaN],
  ["Infinity", Infinity],
  ["-Infinity", -Infinity],
]);

export function chartNumber(value) {
  if (NON_FINITE.has(value)) return null;
  return exactDecimal(value) ? Number(value) : value;
}

function compareDigits(a, b) {
  if (a.int.length !== b.int.length) return a.int.length - b.int.length;
  if (a.int !== b.int) return a.int < b.int ? -1 : 1;
  const width = Math.max(a.frac.length, b.frac.length);
  const fa = a.frac.padEnd(width, "0");
  const fb = b.frac.padEnd(width, "0");
  return fa === fb ? 0 : fa < fb ? -1 : 1;
}

export function compareNumbers(a, b) {
  if (typeof a === "number" && typeof b === "number") return a - b;
  if (NON_FINITE.has(a) || NON_FINITE.has(b)) {
    const x = NON_FINITE.has(a) ? NON_FINITE.get(a) : Number(a);
    const y = NON_FINITE.has(b) ? NON_FINITE.get(b) : Number(b);
    if (Number.isNaN(x) || Number.isNaN(y)) return Number.isNaN(x) - Number.isNaN(y);
    return x === y ? 0 : x < y ? -1 : 1;
  }
  const x = exactDecimal(typeof a === "number" ? String(a) : a);
  const y = exactDecimal(typeof b === "number" ? String(b) : b);
  if (!x || !y) return Number(a) - Number(b);
  if (x.neg !== y.neg) return x.neg ? -1 : 1;
  return x.neg ? compareDigits(y, x) : compareDigits(x, y);
}

export function formatValue(value, format, compact = false) {
  if (value === null || value === undefined) return "—";
  const exact = exactDecimal(value);
  const num = typeof value === "number" ? value : Number(value);
  if (!Number.isFinite(num)) return String(value);
  const amount = exact ? exact.text : num;
  const { locale } = formatConfig;
  if (format === "currency" || CURRENCY_CODE.test(format)) {
    const currency = CURRENCY_CODE.test(format) ? format : formatConfig.currency;
    try {
      return new Intl.NumberFormat(locale, {
        style: "currency",
        currency,
        notation: compact || Math.abs(num) >= 1_000_000 ? "compact" : "standard",
        maximumFractionDigits: Math.abs(num) >= 1000 ? (compact ? 1 : 0) : 2,
      }).format(amount);
    } catch {
      return new Intl.NumberFormat(locale, { maximumFractionDigits: 2 }).format(amount);
    }
  }
  switch (format) {
    case "percent":
      return new Intl.NumberFormat(locale, {
        style: "percent",
        maximumFractionDigits: 1,
      }).format(amount);
    case "compact":
      return new Intl.NumberFormat(locale, {
        notation: "compact",
        maximumFractionDigits: 1,
      }).format(amount);
    case "date": {
      const parsed = new Date(value);
      return Number.isNaN(parsed.getTime())
        ? String(value)
        : new Intl.DateTimeFormat(locale, { dateStyle: "medium" }).format(parsed);
    }
    default:
      return new Intl.NumberFormat(locale, {
        maximumFractionDigits: exact
          ? Math.min(exact.frac.length, 100)
          : Number.isInteger(num)
            ? 0
            : 2,
      }).format(amount);
  }
}

function colIndex(result, name) {
  return result.columns.findIndex((c) => c.name === name);
}

function withAlpha(hex, alpha) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

function areaGradient(hex) {
  return {
    type: "linear",
    x: 0, y: 0, x2: 0, y2: 1,
    colorStops: [
      { offset: 0, color: withAlpha(hex, 0.22) },
      { offset: 1, color: withAlpha(hex, 0.01) },
    ],
  };
}

function firstColOfTypes(result, typeSet, exclude = []) {
  const idx = result.columns.findIndex(
    (c) => typeSet.has(c.type) && !exclude.includes(c.name)
  );
  return idx === -1 ? null : result.columns[idx].name;
}

const TYPE_FIELDS = {
  line: ["x", "y", "group_by", "legend", "format", "references"],
  bar: ["x", "y", "group_by", "stacked", "orientation", "color_by", "legend", "format", "references"],
  area: ["x", "y", "group_by", "stacked", "legend", "format", "references"],
  scatter: ["x", "y", "group_by", "legend", "format", "references"],
  pie: ["label", "value", "legend", "format"],
  heatmap: [
    "x", "y", "value", "aggregate", "palette", "midpoint", "x_order", "y_order", "legend", "format",
  ],
  big_number: ["value", "format"],
  table: ["format"],
};

export function pruneSpecForType(spec, type) {
  const keep = new Set(["type", ...(TYPE_FIELDS[type] || [])]);
  const next = { type };
  for (const [key, value] of Object.entries(spec)) {
    if (key !== "type" && keep.has(key)) next[key] = value;
  }
  if (typeof next.y === "string" && type !== "heatmap") next.y = [next.y];
  return next;
}

// A pinned encoding that names a column this result does not have is not an
// encoding, it is a stale alias (#357). Drop it so the inference below fills
// the slot from the columns that exist, instead of plotting every value null.
function dropStaleEncodings(spec, result) {
  const names = new Set(result.columns.map((c) => c.name));
  const s = { ...spec };
  const stale = [];
  for (const key of ["x", "group_by", "label", "value"]) {
    if (s[key] && !names.has(s[key])) {
      stale.push(`${key}: ${s[key]}`);
      s[key] = null;
    }
  }
  if (Array.isArray(s.y)) {
    const kept = s.y.filter((name) => names.has(name));
    if (kept.length !== s.y.length) {
      stale.push(`y: ${s.y.filter((name) => !names.has(name)).join(", ")}`);
      s.y = kept;
    }
  }
  if (stale.length) {
    console.warn(
      `chart spec names columns the result does not have (${stale.join("; ")}); ` +
        `columns are ${[...names].join(", ")} — inferring instead`
    );
  }
  return s;
}

export function inferSpec(spec, result) {
  const s = dropStaleEncodings(spec, result);
  if (["line", "bar", "area", "scatter"].includes(s.type)) {
    if (!s.x) {
      s.x =
        firstColOfTypes(result, TEMPORAL_TYPES) ??
        firstColOfTypes(result, new Set(["string"])) ??
        result.columns[0]?.name;
    }
    if (!s.y || !s.y.length) {
      const ys = result.columns
        .filter((c) => NUMERIC_TYPES.has(c.type) && c.name !== s.x && c.name !== s.group_by)
        .map((c) => c.name);
      s.y = ys.length ? ys : [result.columns[1]?.name].filter(Boolean);
    }
  }
  if (s.type === "pie") {
    if (!s.label) s.label = s.x ?? firstColOfTypes(result, new Set(["string"]));
    if (!s.value) {
      s.value = (s.y && s.y[0]) ?? firstColOfTypes(result, NUMERIC_TYPES, [s.label]);
    }
  }
  if (s.type === "heatmap") inferHeatmap(s, result);
  if (s.type === "big_number" && !s.value) {
    s.value = firstColOfTypes(result, NUMERIC_TYPES) ?? result.columns[0]?.name;
  }
  return s;
}

function inferHeatmap(s, result) {
  const names = new Set(result.columns.map((c) => c.name));
  let y = Array.isArray(s.y) ? s.y[0] : s.y;
  if (y && !names.has(y)) y = null;
  const taken = new Set([s.x, y, s.value].filter(Boolean));
  const free = (c) => !taken.has(c.name);
  const pool = [
    ...result.columns.filter((c) => !NUMERIC_TYPES.has(c.type) && free(c)),
    ...result.columns.filter((c) => c.type === "integer" && free(c)),
  ].map((c) => c.name);
  if (!s.x) s.x = pool.shift() ?? null;
  if (!y) y = pool.find((name) => name !== s.x) ?? null;
  s.y = y;
  if (!s.value && s.aggregate !== "count") {
    const numeric = result.columns.filter(
      (c) => NUMERIC_TYPES.has(c.type) && c.name !== s.x && c.name !== y
    );
    s.value = numeric.at(-1)?.name ?? null;
  }
}

function pivot(result, xName, yName, groupName) {
  const xi = colIndex(result, xName);
  const yi = colIndex(result, yName);
  const gi = colIndex(result, groupName);
  const groups = new Map();
  for (const row of result.rows) {
    const g = String(row[gi] ?? "∅");
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push([row[xi], chartNumber(row[yi])]);
  }
  return groups;
}

export function seriesFormat(spec, name) {
  if (typeof spec.format === "string") return spec.format;
  return (spec.format || {})[name] || "number";
}

export function baseOption(spec, isTemporal, yFormat, compact) {
  const ink2 = cssVar("--ink-2");
  const muted = cssVar("--ink-muted");
  const grid = cssVar("--grid-line");
  const baseline = cssVar("--baseline");
  const surface = cssVar("--surface");
  return {
    color: palette(),
    animationDuration: 400,
    animationEasing: "cubicOut",
    grid: { left: 8, right: 12, top: compact ? 12 : 32, bottom: compact ? 0 : 4, containLabel: true },
    textStyle: { fontFamily: cssVar("--font") || "system-ui, sans-serif" },
    tooltip: {
      backgroundColor: surface,
      borderColor: cssVar("--border"),
      borderWidth: 1,
      textStyle: { color: cssVar("--ink-1"), fontSize: 12 },
      extraCssText: "box-shadow: 0 4px 16px rgba(0,0,0,0.12); border-radius: 8px;",
      valueFormatter: (v) => formatValue(v, yFormat),
    },
    xAxis: {
      type: isTemporal ? "time" : "category",
      axisLine: { lineStyle: { color: baseline } },
      axisTick: { show: false },
      axisLabel: { color: muted, fontSize: 11, hideOverlap: true },
      splitLine: { show: false },
    },
    yAxis: {
      type: "value",
      splitNumber: compact ? 2 : 5,
      axisLabel: {
        color: muted,
        fontSize: compact ? 10 : 11,
        formatter: (v) => formatValue(v, yFormat === "number" ? "compact" : yFormat, true),
      },
      splitLine: { lineStyle: { color: grid, width: 1, type: "solid" } },
      axisLine: { show: false },
    },
    legend: {
      show: false,
      top: 0,
      right: 0,
      icon: "circle",
      itemWidth: 8,
      itemHeight: 8,
      textStyle: { color: ink2, fontSize: 12 },
    },
  };
}

export function translate(spec, result, forcedColor, height = 0, width = 0) {
  spec = inferSpec(spec, result);
  if (spec.type === "pie") return pieOption(spec, result);
  if (spec.type === "heatmap") return heatmapOption(spec, result, forcedColor, height);
  return xyOption(spec, result, forcedColor, height, width);
}

function xyOption(spec, result, forcedColor, height = 0, width = 0) {
  const surface = cssVar("--surface");
  const xType = result.columns[colIndex(result, spec.x)]?.type;
  const isTemporal = TEMPORAL_TYPES.has(xType) && spec.type !== "bar";
  const yFormat = seriesFormat(spec, spec.y[0]);
  const compact = height > 0 && height < 170;
  const option = baseOption(spec, isTemporal, yFormat, compact);

  let series = [];
  if (spec.group_by && spec.y.length === 1) {
    const groups = pivot(result, spec.x, spec.y[0], spec.group_by);
    series = [...groups.entries()].map(([name, data]) => ({ name, data }));
  } else {
    const xi = colIndex(result, spec.x);
    series = spec.y.map((yName) => ({
      name: humanize(yName),
      data: result.rows.map((row) => [row[xi], chartNumber(row[colIndex(result, yName)])]),
    }));
  }

  const showSymbols = series.every((s) => s.data.length <= 40);
  const stacked = spec.stacked || spec.type === "area";
  const horizontal = spec.type === "bar" && spec.orientation === "horizontal";

  if (horizontal) {
    const category = option.xAxis;
    const value = option.yAxis;
    option.xAxis = {
      ...value,
      type: "value",
      splitLine: value.splitLine,
    };
    option.yAxis = {
      ...category,
      type: "category",
      splitLine: { show: false },
      axisLabel: { ...category.axisLabel, hideOverlap: false, interval: 0 },
    };
  }

  const colors = forcedColor && series.length === 1 ? [forcedColor] : palette();
  option.color = colors;
  option.series = series.map((s, i) => {
    const seriesColor = colors[i % colors.length];
    const common = { name: s.name, data: s.data, emphasis: { focus: series.length > 1 ? "series" : "none" } };
    if (spec.type === "bar") {
      return {
        ...common,
        type: "bar",
        stack: spec.stacked ? "total" : undefined,
        barMaxWidth: 24,
        encode: horizontal ? { x: 1, y: 0 } : undefined,
        itemStyle: {
          borderRadius: spec.stacked ? 0 : horizontal ? [0, 4, 4, 0] : [4, 4, 0, 0],
          borderColor: spec.stacked ? surface : undefined,
          borderWidth: spec.stacked ? 1 : 0,
        },
      };
    }
    if (spec.type === "scatter") {
      return {
        ...common,
        type: "scatter",
        symbolSize: 10,
        itemStyle: { borderColor: surface, borderWidth: 2, opacity: 0.9 },
      };
    }
    return {
      ...common,
      type: "line",
      stack: spec.type === "area" && stacked && series.length > 1 ? "total" : undefined,
      lineStyle: { width: 2, cap: "round", join: "round" },
      symbol: "circle",
      symbolSize: 8,
      showSymbol: showSymbols,
      itemStyle: { borderColor: surface, borderWidth: 2 },
      areaStyle:
        spec.type === "area" ? { opacity: 1, color: areaGradient(seriesColor) } : undefined,
    };
  });

  option.tooltip.trigger = spec.type === "scatter" ? "item" : "axis";
  if (spec.type !== "scatter") {
    option.tooltip.axisPointer =
      spec.type === "bar"
        ? { type: "shadow", shadowStyle: { opacity: 0.06 } }
        : { type: "line", lineStyle: { color: cssVar("--baseline"), type: "solid", width: 1 } };
  }
  if (spec.color_by === "value" && spec.type === "bar" && series.length === 1) {
    let min = Infinity;
    let max = -Infinity;
    for (const d of series[0].data) {
      const v = Number(d[1]);
      if (Number.isNaN(v)) continue;
      if (v < min) min = v;
      if (v > max) max = v;
    }
    if (min <= max) {
      option.visualMap = {
        show: false,
        min,
        max,
        dimension: 1,
        seriesIndex: 0,
        inRange: { color: [withAlpha(colors[0], 0.3), colors[0]] },
      };
    }
  }

  option.legend.show = spec.legend !== false && option.series.length >= 2;
  if (option.legend.show) option.grid.top = 32;
  if (spec.type === "bar" && !isTemporal && !horizontal) {
    option.xAxis.axisLabel.interval = "auto";
  }
  addReferences(option, spec, result, { horizontal, isTemporal, yFormat, width, height });
  return option;
}

export function referenceKind(ref) {
  if (ref.metric != null) return "metric";
  if (ref.x != null) return Array.isArray(ref.x) ? "span" : "marker";
  return Array.isArray(ref.y) ? "band" : "line";
}

const filled = (v) => v != null && v !== "";

function numberOrNull(v) {
  if (!filled(v)) return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

export function cleanReference(ref) {
  const kind = referenceKind(ref);
  const out = {};
  if (kind === "line") {
    const y = numberOrNull(ref.y);
    if (y === null) return null;
    out.y = y;
  } else if (kind === "band") {
    const pair = (ref.y ?? []).map(numberOrNull);
    if (pair.length !== 2 || pair.includes(null)) return null;
    out.y = pair;
  } else if (kind === "marker") {
    if (!filled(ref.x)) return null;
    out.x = ref.x;
  } else if (kind === "span") {
    const pair = ref.x ?? [];
    if (pair.length !== 2 || !pair.every(filled)) return null;
    out.x = [...pair];
  } else {
    if (!filled(ref.metric)) return null;
    out.metric = ref.metric;
  }
  for (const key of ["label", "color", "style", "format"]) {
    if (filled(ref[key])) out[key] = ref[key];
  }
  return out;
}

const REFERENCE_TOKENS = {
  ink: "--ink-2",
  muted: "--ink-muted",
  accent: "--accent",
  good: "--good-text",
  bad: "--danger",
};

function referenceColor(name) {
  const token = REFERENCE_TOKENS[name ?? "ink"] ?? `--${name}`;
  return cssVar(token) || cssVar("--ink-2");
}

function finiteNumber(value) {
  const n = typeof value === "number" ? value : Number(value);
  return value !== null && value !== "" && Number.isFinite(n) ? n : null;
}

/* ECharts' own date parsing (echarts.number.parseDate in the vendored
   5.5.1): the time axis places series values with it, so a reference has to
   use it too or it drifts from an identical point. It reads only the hour of
   an offset, which is why +05:30 lands at +05:00. */
const ECHARTS_TIME =
  /^(?:(\d{4})(?:[-/](\d{1,2})(?:[-/](\d{1,2})(?:[T ](\d{1,2})(?::(\d{1,2})(?::(\d{1,2})(?:[.,](\d+))?)?)?(Z|[+-]\d\d:?\d\d)?)?)?)?)?$/;

function timeValue(value) {
  if (typeof value === "number") return Number.isFinite(value) ? Math.round(value) : null;
  const m = ECHARTS_TIME.exec(String(value));
  if (!m || !m[1]) return null;
  const day = [+m[1], +(m[2] || 1) - 1, +m[3] || 1];
  const rest = [+(m[5] || 0), +m[6] || 0, m[7] ? +m[7].substring(0, 3) : 0];
  if (m[8]) {
    const hour = (+m[4] || 0) - (m[8].toUpperCase() === "Z" ? 0 : +m[8].slice(0, 3));
    return Date.UTC(...day, hour, ...rest);
  }
  return new Date(...day, +m[4] || 0, ...rest).getTime();
}

function niceStep(raw) {
  const exp = Math.floor(Math.log10(raw));
  const f = raw / 10 ** exp;
  const nice = f < 1.5 ? 1 : f < 2.5 ? 2 : f < 4 ? 3 : f < 7 ? 5 : 10;
  return nice * 10 ** exp;
}

const tidy = (n) => Number(n.toPrecision(12));

/* A reference past the data still has to be on the chart, or a target the
   series never reaches is silently missing. ECharts leaves an axis bound it
   computes alone when these return null; otherwise both bounds are fixed to a
   nice step around the data (zero included, as the value axis does) and the
   references. */
export function referenceExtent(extent, values, splitNumber = 5) {
  let lo = Math.min(Number.isFinite(extent.min) ? extent.min : 0, 0);
  let hi = Math.max(Number.isFinite(extent.max) ? extent.max : 0, 0);
  if (!values.length || values.every((v) => v >= lo && v <= hi)) return null;
  lo = Math.min(lo, ...values);
  hi = Math.max(hi, ...values);
  const step = niceStep((hi - lo) / splitNumber);
  if (!Number.isFinite(step) || step <= 0) return null;
  return { min: tidy(Math.floor(lo / step) * step), max: tidy(Math.ceil(hi / step) * step) };
}

function reachValueAxis(axis, values) {
  const split = axis.splitNumber ?? 5;
  axis.min = (e) => referenceExtent(e, values, split)?.min ?? null;
  axis.max = (e) => referenceExtent(e, values, split)?.max ?? null;
}

function reachTimeAxis(axis, times) {
  if (!times.length) return;
  const lo = Math.min(...times);
  const hi = Math.max(...times);
  axis.min = (e) => (Number.isFinite(e.min) && lo >= e.min ? null : lo);
  axis.max = (e) => (Number.isFinite(e.max) && hi <= e.max ? null : hi);
}

/* A category axis numbers its categories in the order the series first
   show them, and a marker at a number is read as that position, not as the
   category named by it. So a marker resolves to the category's own text, or
   to its position when the axis holds numbers. */
function categoryPositions(series) {
  const positions = new Map();
  for (const s of series) {
    for (const d of s.data) {
      const x = Array.isArray(d) ? d[0] : d;
      const text = String(x);
      if (!positions.has(text)) positions.set(text, typeof x === "string" ? x : positions.size);
    }
  }
  return positions;
}

/* The time of day a timestamp category carries after its date. */
const TIME_OF_DAY = /^[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}(:?\d{2})?)?$/;

/* A category marker matches its category exactly. The one exception is a day
   on a date or timestamp column drawn as categories (a bar chart), where
   `2026-09-01` names the category `2026-09-01T00:00:00`: only there, and
   only when what follows the date is a time. */
function matchCategory(value, categories, temporal) {
  const text = String(value);
  if (categories.has(text)) return categories.get(text);
  if (!temporal || !/^\d{4}-\d{2}-\d{2}$/.test(text)) return null;
  for (const [c, at] of categories) {
    if (c.startsWith(text) && TIME_OF_DAY.test(c.slice(text.length))) return at;
  }
  return null;
}

/* A narrow tile keeps the reference's name and drops the number beside it,
   then truncates the name: a cut-off "$15…" would read as a different target. */
const NARROW_REFERENCE_WIDTH = 360;

function referenceLabel(color, surface, position, text, width) {
  let formatter = String(text ?? "");
  if (width > 0) {
    const room = Math.max(6, Math.floor((width * 0.6) / 6.5));
    if (formatter.length > room) formatter = `${formatter.slice(0, room - 1).trimEnd()}…`;
  }
  return {
    show: true,
    position,
    formatter,
    color,
    fontSize: 11,
    fontWeight: 500,
    backgroundColor: surface,
    padding: [2, 6],
    borderRadius: 4,
  };
}

/* A whole currency amount on a reference reads as a round target: "$0", not
   "$0.00". Everything else formats as the axis does. */
function referenceValue(value, fmt) {
  const currency = fmt === "currency" ? formatConfig.currency : CURRENCY_CODE.test(fmt) ? fmt : null;
  if (!currency || !Number.isInteger(value) || Math.abs(value) >= 1000) return formatValue(value, fmt);
  try {
    return new Intl.NumberFormat(formatConfig.locale, {
      style: "currency",
      currency,
      maximumFractionDigits: 0,
    }).format(value);
  } catch {
    return formatValue(value, fmt);
  }
}

/* A band label sits inside the band's top edge. When the band is too thin to
   hold it, or another reference line runs through the band, it would sit on
   that line, so it moves just below the band instead: a value line's own
   label is above its line, and that side stays clear for it. */
const BAND_LABEL_ROOM = 22;

function liftCrowdedBandLabels(bandLabels, valueLines, series, references, height) {
  if (!bandLabels.length) return;
  let lo = 0;
  let hi = 0;
  const reach = (v) => {
    if (v === null) return;
    if (v < lo) lo = v;
    if (v > hi) hi = v;
  };
  for (const s of series) for (const d of s.data) reach(finiteNumber(Array.isArray(d) ? d[1] : d));
  for (const v of references) reach(v);
  const span = hi - lo;
  const plotHeight = Math.max(height - 60, 0);
  for (const band of bandLabels) {
    const crossed = valueLines.some((y) => y >= band.lo && y <= band.hi);
    const tall = ((band.hi - band.lo) / span) * plotHeight;
    const thin = plotHeight > 0 && span > 0 && tall < BAND_LABEL_ROOM;
    if (crossed || thin) band.line.yAxis = band.lo;
  }
}

function addReferences(option, spec, result, { horizontal, isTemporal, yFormat, width = 0, height = 0 }) {
  const refs = (spec.references ?? []).filter(Boolean);
  if (!refs.length) return;
  const narrow = width > 0 && width < NARROW_REFERENCE_WIDTH;
  const surface = cssVar("--surface");
  const valueKey = horizontal ? "xAxis" : "yAxis";
  const categoryKey = horizontal ? "yAxis" : "xAxis";
  const categories = categoryPositions(option.series);
  const temporalCategories = TEMPORAL_TYPES.has(result.columns[colIndex(result, spec.x)]?.type);
  const lines = [];
  const bands = [];
  const bandLabels = [];
  const valueLines = [];
  const values = [];
  const times = [];
  const place = (value) => {
    if (isTemporal) {
      const t = timeValue(value);
      if (t === null) return null;
      times.push(t);
      return value;
    }
    return matchCategory(value, categories, temporalCategories);
  };
  for (const ref of refs) {
    const color = referenceColor(ref.color);
    const fmt = ref.format || yFormat;
    const label = ref.label ?? (ref.metric ? humanize(ref.metric) : null);
    const lineStyle = { color, width: 1.5, type: ref.style || "dashed", opacity: 0.9 };
    if (Array.isArray(ref.y)) {
      const [a, b] = ref.y.map(finiteNumber);
      if (a === null || b === null) continue;
      values.push(a, b);
      const text = label ?? `${referenceValue(Math.min(a, b), fmt)} – ${referenceValue(Math.max(a, b), fmt)}`;
      bands.push({
        from: { [valueKey]: Math.min(a, b) },
        to: { [valueKey]: Math.max(a, b) },
        color,
      });
      const bandLabel = {
        [valueKey]: Math.max(a, b),
        lineStyle: { color: "transparent", width: 0 },
        label: referenceLabel(color, surface, horizontal ? "insideEndBottom" : "insideStartBottom", text, width),
      };
      lines.push(bandLabel);
      if (!horizontal) bandLabels.push({ line: bandLabel, lo: Math.min(a, b), hi: Math.max(a, b) });
    } else if (Array.isArray(ref.x)) {
      const [a, b] = ref.x.map(place);
      if (a === null || b === null) {
        console.warn(`reference x ${JSON.stringify(ref.x)} is not on this chart's x axis`);
        continue;
      }
      bands.push({
        from: { [categoryKey]: a },
        to: { [categoryKey]: b },
        color,
        text: label,
        position: horizontal ? "insideTopLeft" : "insideTop",
      });
    } else if (ref.x !== null && ref.x !== undefined) {
      const at = place(ref.x);
      if (at === null) {
        console.warn(`reference x ${JSON.stringify(ref.x)} is not on this chart's x axis`);
        continue;
      }
      const text = label ?? (isTemporal ? formatValue(ref.x, "date") : String(typeof at === "number" ? ref.x : at));
      lines.push({
        [categoryKey]: at,
        lineStyle,
        label: referenceLabel(color, surface, horizontal ? "insideEndTop" : "end", text, width),
      });
    } else {
      const y = finiteNumber(ref.y);
      if (y === null) continue;
      values.push(y);
      const shown = referenceValue(y, fmt);
      valueLines.push(y);
      lines.push({
        [valueKey]: y,
        lineStyle,
        label: referenceLabel(
          color,
          surface,
          horizontal ? "end" : "insideEndTop",
          label ? (narrow ? label : `${label}  ${shown}`) : shown,
          width
        ),
      });
    }
  }
  if (values.length) reachValueAxis(option[valueKey], values);
  liftCrowdedBandLabels(bandLabels, valueLines, option.series, values, height);
  const labelsAbove = lines.some((line) => line.label.position === "end");
  if (labelsAbove && option.legend.show) option.grid.top += 18;
  if (isTemporal && times.length) reachTimeAxis(option[categoryKey], times);
  if (option.legend.show) option.legend.data = option.series.map((s) => s.name);
  const carrier = { type: "line", data: [], silent: true, animation: false, tooltip: { show: false } };
  if (bands.length) {
    option.series.push({
      ...carrier,
      name: "__reference_bands",
      z: 1,
      markArea: {
        silent: true,
        data: bands.map((band) => [
          { ...band.from, itemStyle: { color: band.color, opacity: 0.08 }, label: { show: false } },
          band.to,
        ]),
      },
    });
  }
  const labelled = bands.filter((band) => band.text);
  if (lines.length || labelled.length) {
    option.series.push({
      ...carrier,
      name: "__reference_lines",
      z: 5,
      markArea: labelled.length
        ? {
            silent: true,
            z: 5,
            data: labelled.map((band) => [
              {
                ...band.from,
                itemStyle: { color: "transparent" },
                label: referenceLabel(band.color, surface, band.position, band.text, width),
              },
              band.to,
            ]),
          }
        : undefined,
      markLine: lines.length
        ? { silent: true, symbol: ["none", "none"], animation: false, data: lines }
        : undefined,
    });
  }
}

export function pieTooltip(fmt) {
  return (p) =>
    `${p.marker} ${escapeHtml(p.name)}&nbsp;&nbsp;<b>${escapeHtml(formatValue(p.value, fmt))}</b>` +
    `&nbsp;<span style="opacity:.6">${escapeHtml(p.percent)}%</span>`;
}

function pieOption(spec, result) {
  const surface = cssVar("--surface");
  const li = colIndex(result, spec.label);
  const vi = colIndex(result, spec.value);
  const fmt = seriesFormat(spec, spec.value);
  const option = baseOption(spec, false, fmt);
  delete option.xAxis;
  delete option.yAxis;
  delete option.grid;
  option.tooltip.trigger = "item";
  option.tooltip.formatter = pieTooltip(fmt);
  option.legend.show = spec.legend !== false;
  option.series = [
    {
      type: "pie",
      radius: ["58%", "82%"],
      center: ["50%", "54%"],
      itemStyle: { borderColor: surface, borderWidth: 2, borderRadius: 3 },
      label: { color: cssVar("--ink-2"), fontSize: 11 },
      labelLine: { lineStyle: { color: cssVar("--baseline") } },
      data: result.rows.map((row) => ({ name: String(row[li]), value: chartNumber(row[vi]) })),
    },
  ];
  return option;
}

export function renderBigNumber(el, spec, result) {
  spec = inferSpec(spec, result);
  const vi = colIndex(result, spec.value);
  const value = result.rows.length ? result.rows[0][vi] : null;
  const fmt = seriesFormat(spec, spec.value);
  el.innerHTML = "";
  const box = document.createElement("div");
  box.className = "big-number";
  const val = document.createElement("div");
  val.className = "value";
  val.textContent = formatValue(value, fmt);
  box.appendChild(val);
  const caption = document.createElement("div");
  caption.className = "caption";
  caption.textContent = humanize(spec.value);
  box.appendChild(caption);
  el.appendChild(box);
}

export const RESULT_PAGE = 250;

// `page` renders that many rows (or `shown`, if more were already revealed) and
// offers more on demand. Sorting still sees every row. `onRows` runs after each
// body render, for callers that decorate rows.
export function renderTable(el, spec, result, { page = 0, shown: start = page, onRows } = {}) {
  closeCellPop();
  let shown = page ? Math.max(start, page) : Infinity;
  el.innerHTML = "";
  const wrap = document.createElement("div");
  wrap.className = "table-wrap";
  const table = document.createElement("table");
  table.className = "results";
  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  const sort = { index: -1, dir: 1 };

  const compareFor = (index) => {
    const numeric = NUMERIC_TYPES.has(result.columns[index].type);
    return (a, b) => {
      const va = a[index];
      const vb = b[index];
      if (va == null && vb == null) return 0;
      if (va == null) return 1;
      if (vb == null) return -1;
      if (numeric) return compareNumbers(va, vb) * sort.dir;
      return String(va).localeCompare(String(vb)) * sort.dir;
    };
  };

  const formats = spec?.format || {};
  const formatFor = (name) => (typeof formats === "string" ? formats : formats[name] || "number");

  const renderBody = () => {
    tbody.innerHTML = "";
    const rows = sort.index < 0 ? result.rows : [...result.rows].sort(compareFor(sort.index));
    for (const row of rows.slice(0, shown)) {
      const tr = document.createElement("tr");
      result.columns.forEach((col, i) => {
        const td = document.createElement("td");
        const v = row[i];
        const shown = inspectText(v);
        if (v === null || v === undefined) {
          td.textContent = "null";
          td.className = "null";
        } else if (NUMERIC_TYPES.has(col.type)) {
          td.textContent = formatValue(v, formatFor(col.name));
          td.className = "num";
        } else {
          td.textContent = shown.display;
        }
        td.title = shown.inspect;
        td.classList.add("cell-inspect");
        td.tabIndex = 0;
        const inspect = (e) => {
          e.stopPropagation();
          showCellPop(td, shown.inspect, shown.pretty);
        };
        td.addEventListener("mousedown", (e) => e.stopPropagation());
        td.addEventListener("click", inspect);
        td.addEventListener("keydown", (e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            inspect(e);
          }
        });
        tr.appendChild(td);
      });
      tbody.appendChild(tr);
    }
    onRows?.(tbody);
    updateMore(rows.length);
  };
  let more;
  const updateMore = (total) => {
    if (!page) return;
    if (!more) {
      more = document.createElement("div");
      more.className = "results-more";
      const label = document.createElement("span");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "btn btn-ghost";
      button.addEventListener("click", () => {
        shown += page;
        renderBody();
      });
      more.append(label, button);
    }
    const visible = Math.min(shown, total);
    more.hidden = visible >= total;
    more.firstChild.textContent = `Showing ${visible.toLocaleString()} of ${total.toLocaleString()} rows`;
    more.lastChild.textContent = `Show ${Math.min(page, total - visible).toLocaleString()} more`;
  };

  result.columns.forEach((col, index) => {
    const th = document.createElement("th");
    th.textContent = humanize(col.name);
    if (NUMERIC_TYPES.has(col.type)) th.className = "num";
    th.tabIndex = 0;
    th.setAttribute("role", "button");
    th.title = "Sort";
    const activate = () => {
      if (sort.index === index) sort.dir = -sort.dir;
      else {
        sort.index = index;
        sort.dir = 1;
      }
      for (const other of headRow.children) {
        other.removeAttribute("data-sort");
        other.removeAttribute("aria-sort");
      }
      th.dataset.sort = sort.dir === 1 ? "asc" : "desc";
      th.setAttribute("aria-sort", sort.dir === 1 ? "ascending" : "descending");
      renderBody();
    };
    th.addEventListener("click", activate);
    th.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        activate();
      }
    });
    headRow.appendChild(th);
  });
  thead.appendChild(headRow);
  table.appendChild(thead);
  const tbody = document.createElement("tbody");
  renderBody();
  table.appendChild(tbody);
  wrap.appendChild(table);
  if (more) wrap.appendChild(more);
  if (result.truncated) {
    const note = document.createElement("div");
    note.className = "truncated-note";
    note.textContent = `showing first ${result.row_count.toLocaleString()} rows (truncated)`;
    wrap.appendChild(note);
  }
  el.appendChild(wrap);
  const shade = () => {
    const more = wrap.scrollHeight - wrap.clientHeight - wrap.scrollTop > 1;
    wrap.classList.toggle("has-more", more);
  };
  wrap.addEventListener("scroll", shade, { passive: true });
  new ResizeObserver(shade).observe(wrap);
}

function plotted(point) {
  let value = Array.isArray(point) ? point[1] : point?.value;
  if (Array.isArray(value)) value = value.at(-1);
  return typeof value === "number" && Number.isFinite(value);
}

const emptyReasons = new WeakMap();

export function setEmptyReason(option, reason) {
  emptyReasons.set(option, reason);
}

export function markEmptyChart(body, option) {
  body.querySelector(":scope > .chart-empty")?.remove();
  const reason = emptyReasons.get(option);
  const empty = Boolean(reason) || !(option.series || []).some((s) => (s.data || []).some(plotted));
  body.classList.toggle("no-values", empty);
  if (!empty) return;
  const note = document.createElement("div");
  note.className = "chart-empty";
  note.textContent = reason ?? "No values to plot";
  body.appendChild(note);
}

export function markTruncated(body, result) {
  // `:scope >` so a table's own note, which lives inside .table-wrap, is left
  // to renderTable.
  body.querySelector(":scope > .truncated-note")?.remove();
  // The class is what reserves room for the note: a big number is height 100%
  // and a chart mount is absolutely positioned over the whole body, so an
  // appended note either falls outside the tile or paints underneath the canvas.
  body.classList.toggle("has-truncation", Boolean(result.truncated));
  if (!result.truncated) return;
  const note = document.createElement("div");
  note.className = "truncated-note";
  note.textContent = `showing first ${result.row_count.toLocaleString()} rows (truncated)`;
  body.appendChild(note);
}
