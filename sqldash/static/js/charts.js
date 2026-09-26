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
  line: ["x", "y", "group_by", "legend", "format"],
  bar: ["x", "y", "group_by", "stacked", "orientation", "color_by", "legend", "format"],
  area: ["x", "y", "group_by", "stacked", "legend", "format"],
  scatter: ["x", "y", "group_by", "legend", "format"],
  pie: ["label", "value", "legend", "format"],
  big_number: ["value", "format"],
  table: ["format"],
};

export function pruneSpecForType(spec, type) {
  const keep = new Set(["type", ...(TYPE_FIELDS[type] || [])]);
  const next = { type };
  for (const [key, value] of Object.entries(spec)) {
    if (key !== "type" && keep.has(key)) next[key] = value;
  }
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
  if (s.type === "big_number" && !s.value) {
    s.value = firstColOfTypes(result, NUMERIC_TYPES) ?? result.columns[0]?.name;
  }
  return s;
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

function seriesFormat(spec, name) {
  if (typeof spec.format === "string") return spec.format;
  return (spec.format || {})[name] || "number";
}

function baseOption(spec, isTemporal, yFormat, compact) {
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

export function translate(spec, result, forcedColor, height = 0) {
  spec = inferSpec(spec, result);
  if (spec.type === "pie") return pieOption(spec, result);
  return xyOption(spec, result, forcedColor, height);
}

function xyOption(spec, result, forcedColor, height = 0) {
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
    const values = series[0].data.map((d) => Number(d[1])).filter((v) => !Number.isNaN(v));
    if (values.length) {
      option.visualMap = {
        show: false,
        min: Math.min(...values),
        max: Math.max(...values),
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
  return option;
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
  const value = Array.isArray(point) ? point[1] : point?.value;
  return typeof value === "number" && Number.isFinite(value);
}

export function markEmptyChart(body, option) {
  body.querySelector(":scope > .chart-empty")?.remove();
  const empty = !(option.series || []).some((s) => (s.data || []).some(plotted));
  body.classList.toggle("no-values", empty);
  if (!empty) return;
  const note = document.createElement("div");
  note.className = "chart-empty";
  note.textContent = "No values to plot";
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
