import {
  baseOption,
  chartNumber,
  cssVar,
  escapeHtml,
  exactDecimal,
  formatValue,
  humanize,
  seriesFormat,
  setEmptyReason,
} from "./charts.js";

export const MAX_CATEGORIES = 60;
const NUMERIC_TYPES = new Set(["integer", "float", "decimal"]);
const ORDERED_TYPES = new Set(["integer", "float", "decimal", "date", "timestamp"]);
const AGGREGATE_LABELS = { sum: "Sum", avg: "Average", count: "Count", min: "Min", max: "Max" };

export function categoryKey(value) {
  return value === null || value === undefined ? "null" : String(value);
}

function numericValue(value) {
  if (typeof value === "number") return Number.isFinite(value) ? value : undefined;
  if (typeof value === "string" && exactDecimal(value)) {
    const n = chartNumber(value);
    return Number.isFinite(n) ? n : undefined;
  }
  return undefined;
}

function compareKeys(type) {
  if (NUMERIC_TYPES.has(type)) return (a, b) => Number(a.raw) - Number(b.raw);
  return (a, b) => (a.key < b.key ? -1 : a.key > b.key ? 1 : 0);
}

function orderCategories(values, type, explicit) {
  const seen = new Map();
  for (const raw of values) {
    const key = categoryKey(raw);
    if (!seen.has(key)) seen.set(key, { key, raw });
  }
  const pinned = (explicit ?? []).map(categoryKey);
  const pinnedSet = new Set(pinned);
  let rest = [...seen.values()].filter((c) => !pinnedSet.has(c.key));
  const nullLast = rest.filter((c) => c.key === "null" && (c.raw === null || c.raw === undefined));
  rest = rest.filter((c) => !nullLast.includes(c));
  if (ORDERED_TYPES.has(type)) rest.sort(compareKeys(type));
  return [...new Set(pinned)].concat(rest.map((c) => c.key), nullLast.map((c) => c.key));
}

function aggregateOf(kind, values, rows) {
  if (kind === "count") return rows;
  if (!values.length) return null;
  if (kind === "sum" || kind === "avg") {
    const sum = values.reduce((a, b) => a + b, 0);
    return kind === "sum" ? sum : sum / values.length;
  }
  return kind === "min" ? Math.min(...values) : Math.max(...values);
}

export function buildCells(result, spec, { limit = MAX_CATEGORIES } = {}) {
  const col = (name) => result.columns.findIndex((c) => c.name === name);
  const xi = col(spec.x);
  const yi = col(spec.y);
  const vi = col(spec.value);
  const type = (i) => result.columns[i]?.type;
  const allX = orderCategories(result.rows.map((r) => r[xi]), type(xi), spec.x_order);
  const allY = orderCategories(result.rows.map((r) => r[yi]), type(yi), spec.y_order);
  const xs = allX.slice(0, limit);
  const ys = allY.slice(0, limit);
  const xIndex = new Map(xs.map((k, i) => [k, i]));
  const yIndex = new Map(ys.map((k, i) => [k, i]));
  const groups = new Map();
  let hiddenRows = 0;
  let nonNumeric = 0;
  for (const row of result.rows) {
    const i = xIndex.get(categoryKey(row[xi]));
    const j = yIndex.get(categoryKey(row[yi]));
    if (i === undefined || j === undefined) {
      hiddenRows += 1;
      continue;
    }
    const id = `${i}|${j}`;
    if (!groups.has(id)) groups.set(id, { i, j, rows: 0, values: [] });
    const cell = groups.get(id);
    cell.rows += 1;
    const raw = vi >= 0 ? row[vi] : null;
    const n = numericValue(raw);
    if (n !== undefined) cell.values.push(n);
    else if (raw !== null && raw !== undefined) nonNumeric += 1;
  }
  const kind = spec.aggregate ?? null;
  const cells = [];
  let duplicates = 0;
  for (const cell of groups.values()) {
    if (!kind && cell.rows > 1) duplicates += 1;
    const value = kind ? aggregateOf(kind, cell.values, cell.rows) : (cell.values[0] ?? null);
    cells.push({ i: cell.i, j: cell.j, rows: cell.rows, value });
  }
  return {
    xs,
    ys,
    cells,
    duplicates,
    hiddenRows,
    nonNumeric,
    xTotal: allX.length,
    yTotal: allY.length,
  };
}

function parseColor(text) {
  const value = String(text).trim();
  let m = /^#([0-9a-f]{3}|[0-9a-f]{6})$/i.exec(value);
  if (m) {
    const hex = m[1].length === 3 ? [...m[1]].map((c) => c + c).join("") : m[1];
    const n = parseInt(hex, 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }
  m = /^rgba?\(([^)]+)\)$/i.exec(value);
  if (m) return m[1].split(/[\s,/]+/).slice(0, 3).map(Number);
  return null;
}

export function mix(from, to, t) {
  const a = parseColor(from);
  const b = parseColor(to);
  if (!a || !b) return t < 0.5 ? from : to;
  const c = a.map((v, k) => Math.round(v + (b[k] - v) * t));
  return `rgb(${c[0]}, ${c[1]}, ${c[2]})`;
}

export function colorStops(palette, { accent, low, surface, neutral }) {
  if (palette === "diverging") {
    return [
      low,
      mix(surface, low, 0.55),
      neutral,
      mix(surface, accent, 0.55),
      accent,
    ];
  }
  return [0.14, 0.34, 0.56, 0.78, 1].map((t) => mix(surface, accent, t));
}

export function colorRange(values, palette, midpoint) {
  if (!values.length) return { min: 0, max: 1 };
  let min = Math.min(...values);
  let max = Math.max(...values);
  if (palette === "diverging") {
    const mid = Number.isFinite(midpoint) ? midpoint : 0;
    const span = Math.max(Math.abs(max - mid), Math.abs(mid - min)) || 1;
    return { min: mid - span, max: mid + span };
  }
  if (min === max) min -= 1;
  return { min, max };
}

export function heatmapScope(built, spec, result) {
  const parts = [];
  if (result.truncated) parts.push(`first ${result.row_count.toLocaleString()} rows only`);
  if (built.xTotal > built.xs.length) {
    parts.push(`${built.xs.length} of ${built.xTotal.toLocaleString()} ${humanize(spec.x)} values`);
  }
  if (built.yTotal > built.ys.length) {
    parts.push(`${built.ys.length} of ${built.yTotal.toLocaleString()} ${humanize(spec.y)} values`);
  }
  if (built.nonNumeric) {
    parts.push(`${built.nonNumeric.toLocaleString()} non-numeric ${humanize(spec.value)} left out`);
  }
  const text = parts.join(" · ");
  return text && text.charAt(0).toUpperCase() + text.slice(1);
}

export function cellMeasure(spec) {
  const kind = spec.aggregate;
  if (kind === "count") return "Rows";
  const name = humanize(spec.value ?? "value");
  return kind ? `${AGGREGATE_LABELS[kind]} of ${name}` : name;
}

export function duplicateNote(count) {
  return (
    `${count.toLocaleString()} ${count === 1 ? "cell has" : "cells have"} more than one row. ` +
    "Set aggregate (sum, avg, count, min or max) or aggregate in SQL."
  );
}

export function heatmapOption(spec, result, forcedColor, height = 0) {
  const built = buildCells(result, spec);
  const fmt = spec.aggregate === "count" ? "number" : seriesFormat(spec, spec.value);
  const compact = height > 0 && height < 170;
  const option = baseOption(spec, false, fmt, compact);
  const surface = cssVar("--surface");
  const muted = cssVar("--ink-muted");
  const palette = spec.palette ?? "sequential";
  const accent = forcedColor || cssVar("--series-1");
  const stops = colorStops(palette, {
    accent: palette === "diverging" ? cssVar("--series-1") : accent,
    low: cssVar("--series-6"),
    surface,
    neutral: mix(surface, muted, 0.22),
  });
  const scope = heatmapScope(built, spec, result);
  const showLegend = spec.legend !== false && !compact;
  const measure = cellMeasure(spec);

  option.grid = {
    left: 8,
    right: 12,
    top: scope ? 30 : 8,
    bottom: showLegend ? 40 : 4,
    containLabel: true,
  };
  const axisLabel = {
    color: muted,
    fontSize: 11,
    hideOverlap: true,
    width: 100,
    overflow: "truncate",
  };
  option.xAxis = {
    type: "category",
    data: built.xs,
    axisLine: { show: false },
    axisTick: { show: false },
    axisLabel: { ...axisLabel, interval: "auto" },
    splitArea: { show: false },
  };
  option.yAxis = {
    type: "category",
    data: built.ys,
    inverse: true,
    axisLine: { show: false },
    axisTick: { show: false },
    axisLabel: { ...axisLabel, interval: "auto" },
    splitArea: { show: false },
  };
  option.legend.show = false;
  option.tooltip.trigger = "item";
  option.tooltip.formatter = (p) => {
    const cell = p.data?.cell;
    if (!cell) return "";
    const head =
      `<div style="font-weight:600;margin-bottom:4px">${escapeHtml(built.ys[cell.j])}` +
      ` · ${escapeHtml(built.xs[cell.i])}</div>`;
    if (!cell.rows) return `${head}<span style="opacity:.7">No rows</span>`;
    const rows = `${cell.rows.toLocaleString()} ${cell.rows === 1 ? "row" : "rows"}`;
    const value =
      cell.value === null
        ? `<span style="opacity:.7">No ${escapeHtml(humanize(spec.value ?? "value"))}</span>`
        : `${escapeHtml(measure)}&nbsp;&nbsp;<b>${escapeHtml(formatValue(cell.value, fmt))}</b>`;
    return `${head}${p.marker} ${value}<div style="opacity:.6;margin-top:2px">${rows}</div>`;
  };

  const filled = built.duplicates ? [] : built.cells.filter((c) => c.value !== null);
  const range = colorRange(
    filled.map((c) => c.value),
    palette,
    spec.midpoint
  );
  const present = new Set(filled.map((c) => `${c.i}|${c.j}`));
  const gaps = [];
  if (!built.duplicates) {
    const byId = new Map(built.cells.map((c) => [`${c.i}|${c.j}`, c]));
    for (let j = 0; j < built.ys.length; j++) {
      for (let i = 0; i < built.xs.length; i++) {
        const id = `${i}|${j}`;
        if (!present.has(id)) gaps.push(byId.get(id) ?? { i, j, rows: 0, value: null });
      }
    }
  }
  const longest = Math.max(built.xs.length, built.ys.length);
  const dense = longest > 40;
  const gap = dense ? 0.5 : longest > 20 ? 1 : 2;
  const cellName = (c) => `${built.xs[c.i]} × ${built.ys[c.j]}`;
  option.series = [
    {
      type: "heatmap",
      name: measure,
      data: filled.map((c) => ({ name: cellName(c), value: [c.i, c.j, c.value], cell: c })),
      itemStyle: { borderColor: surface, borderWidth: gap, borderRadius: dense ? 1 : 3 },
      emphasis: {
        itemStyle: { borderColor: cssVar("--ink-1"), borderWidth: 1 },
      },
      progressive: 0,
    },
    {
      type: "heatmap",
      name: "missing",
      data: gaps.map((c) => ({ name: cellName(c), value: [c.i, c.j, 0], cell: c })),
      itemStyle: {
        color: mix(surface, muted, 0.1),
        borderColor: surface,
        borderWidth: gap,
        borderRadius: dense ? 1 : 3,
        decal: {
          symbol: "rect",
          symbolSize: 1,
          dashArrayX: [1, 0],
          dashArrayY: [1, 4],
          rotation: -Math.PI / 4,
          color: mix(surface, muted, 0.35),
        },
      },
      emphasis: { disabled: true },
      progressive: 0,
    },
  ];
  const legendLabel = (v) => formatValue(v, fmt === "number" ? "compact" : fmt, true);
  option.visualMap = {
    show: showLegend && filled.length > 0,
    seriesIndex: 0,
    type: "continuous",
    min: range.min,
    max: range.max,
    calculable: false,
    realtime: false,
    orient: "horizontal",
    left: "center",
    bottom: 0,
    itemWidth: 10,
    itemHeight: 96,
    inRange: { color: stops },
    textStyle: { color: muted, fontSize: 11 },
    text: [range.max, range.min].map(legendLabel),
    formatter: legendLabel,
    textGap: 8,
  };
  if (built.duplicates) setEmptyReason(option, duplicateNote(built.duplicates));
  else if (!filled.length) setEmptyReason(option, "No values to plot");
  if (scope) {
    option.graphic = [
      {
        type: "text",
        left: 8,
        top: 6,
        silent: true,
        style: {
          text: scope,
          fill: muted,
          fontSize: 11,
          fontFamily: cssVar("--font") || "system-ui, sans-serif",
        },
      },
    ];
  }
  return option;
}
