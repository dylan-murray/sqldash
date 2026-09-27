import {
  baseOption,
  chartNumber,
  cssVar,
  escapeHtml,
  exactDecimal,
  formatSettings,
  formatValue,
  humanize,
  seriesFormat,
} from "./charts.js";

export const MAX_BINS = 200;
const AUTO_MIN_BINS = 5;
const AUTO_MAX_BINS = 40;

function numericValue(value) {
  if (typeof value === "number") return Number.isFinite(value) ? value : undefined;
  if (typeof value === "string" && exactDecimal(value)) {
    const n = chartNumber(value);
    return Number.isFinite(n) ? n : undefined;
  }
  return undefined;
}

function niceStep(raw) {
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const norm = raw / magnitude;
  const factor = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10;
  return factor * magnitude;
}

function decimalsOf(step) {
  const [mantissa, exponent] = String(step).toLowerCase().split("e");
  const frac = (mantissa.split(".")[1] ?? "").length;
  return Math.min(15, Math.max(0, frac - Number(exponent ?? 0)));
}

function alignedEdges(min, max, width, anchor) {
  const digits = decimalsOf(width) + 2;
  const round = (v) => Number(v.toFixed(Math.min(20, digits)));
  const start = round(anchor + Math.floor((min - anchor) / width) * width);
  const count = Math.max(1, Math.ceil(round((max - start) / width)));
  return { start, count, edge: (i) => round(start + i * width) };
}

function autoCount(n) {
  return Math.min(AUTO_MAX_BINS, Math.max(AUTO_MIN_BINS, Math.ceil(Math.log2(n)) + 1));
}

export function binValues(values, { bins, bin_width: binWidth, bin_start: binStart } = {}) {
  const included = [];
  let nulls = 0;
  let nonNumeric = 0;
  for (const value of values) {
    if (value === null || value === undefined) {
      nulls += 1;
      continue;
    }
    const n = numericValue(value);
    if (n === undefined) nonNumeric += 1;
    else included.push(n);
  }
  const summary = { bins: [], included: included.length, nulls, nonNumeric, mode: "auto" };
  if (!included.length) return summary;
  let min = Infinity;
  let max = -Infinity;
  for (const v of included) {
    if (v < min) min = v;
    if (v > max) max = v;
  }
  summary.min = min;
  summary.max = max;

  let layout = null;
  if (binWidth > 0) {
    const aligned = alignedEdges(min, max, binWidth, Number.isFinite(binStart) ? binStart : 0);
    if (aligned.count <= MAX_BINS) {
      layout = aligned;
      summary.mode = "width";
      summary.width = binWidth;
    } else {
      summary.tooMany = aligned.count;
    }
  }
  if (!layout && min === max) {
    summary.bins = [{ lo: min, hi: max, count: included.length, last: true }];
    summary.mode = summary.tooMany ? "auto" : bins ? "count" : "auto";
    return summary;
  }
  if (!layout && Number.isInteger(bins) && bins >= 1 && !summary.tooMany) {
    const count = Math.min(bins, MAX_BINS);
    const width = (max - min) / count;
    layout = { start: min, count, edge: (i) => (i === count ? max : min + i * width) };
    summary.mode = "count";
    summary.width = width;
  }
  if (!layout) {
    const width = niceStep((max - min) / autoCount(included.length));
    layout = alignedEdges(min, max, width, 0);
    summary.width = width;
  }

  const { count, edge } = layout;
  const span = edge(count) - edge(0);
  const counts = new Array(count).fill(0);
  for (const v of included) {
    let i = Math.floor(((v - edge(0)) / span) * count);
    if (!(i >= 0)) i = 0;
    if (i > count - 1) i = count - 1;
    while (i > 0 && v < edge(i)) i -= 1;
    while (i < count - 1 && v >= edge(i + 1)) i += 1;
    counts[i] += 1;
  }
  summary.bins = counts.map((n, i) => ({
    lo: edge(i),
    hi: edge(i + 1),
    count: n,
    last: i === count - 1,
  }));
  return summary;
}

export function binLabel(bin, fmt) {
  if (bin.lo === bin.hi) return formatValue(bin.lo, fmt);
  return `${formatValue(bin.lo, fmt)} to ${bin.last ? "" : "under "}${formatValue(bin.hi, fmt)}`;
}

const CURRENCY_CODE = /^[A-Z]{3}$/;

export function edgeLabel(value, fmt) {
  const { locale, currency } = formatSettings();
  const big = Math.abs(value) >= 10_000;
  if (fmt === "currency" || CURRENCY_CODE.test(fmt)) {
    try {
      return new Intl.NumberFormat(locale, {
        style: "currency",
        currency: CURRENCY_CODE.test(fmt) ? fmt : currency,
        notation: big ? "compact" : "standard",
        minimumFractionDigits: 0,
        maximumFractionDigits: big ? 1 : 2,
      }).format(value);
    } catch {
      return formatValue(value, "compact", true);
    }
  }
  if (fmt === "percent") return formatValue(value, "percent");
  return new Intl.NumberFormat(locale, {
    notation: big ? "compact" : "standard",
    maximumFractionDigits: big ? 1 : 2,
  }).format(value);
}

export function histogramScope(summary, result, { full = false } = {}) {
  const parts = [];
  if (result.truncated) {
    parts.push(
      full
        ? `first ${result.row_count.toLocaleString()} rows only, not the full distribution`
        : `first ${result.row_count.toLocaleString()} rows only`
    );
  }
  parts.push(`${summary.included.toLocaleString()} values`);
  if (summary.nulls) parts.push(`${summary.nulls.toLocaleString()} null excluded`);
  if (summary.nonNumeric) {
    parts.push(`${summary.nonNumeric.toLocaleString()} non-numeric excluded`);
  }
  if (summary.tooMany) {
    parts.push(`bin_width would make ${summary.tooMany.toLocaleString()} bins, auto bins shown`);
  }
  const text = parts.join(" · ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

const TICK_TARGET = 8;

function axisExtent(summary) {
  const { bins } = summary;
  if (!bins.length) return null;
  const first = bins[0];
  if (bins.length === 1 && first.lo === first.hi) {
    const half = Math.abs(first.lo) >= 100 ? niceStep(Math.abs(first.lo) * 0.01) : 0.5;
    return { min: first.lo - half, max: first.hi + half, interval: half, only: first.lo };
  }
  const width = first.hi - first.lo;
  return {
    min: first.lo,
    max: bins[bins.length - 1].hi,
    interval: width * Math.ceil(bins.length / TICK_TARGET),
  };
}

export function histogramOption(spec, result, forcedColor, height = 0) {
  const xi = result.columns.findIndex((c) => c.name === spec.x);
  const summary = binValues(
    result.rows.map((row) => row[xi]),
    spec
  );
  const percent = spec.measure === "percent";
  const xFormat = seriesFormat(spec, spec.x);
  const yFormat = percent ? "percent" : "number";
  const compact = height > 0 && height < 170;
  const option = baseOption(spec, false, yFormat, compact);
  const color = forcedColor || cssVar("--series-1");
  const total = summary.included;
  const extent = axisExtent(summary);

  option.color = [color];
  option.grid.top = compact ? 22 : 34;
  option.grid.right = 16;
  option.xAxis = {
    ...option.xAxis,
    type: "value",
    scale: true,
    axisLabel: {
      ...option.xAxis.axisLabel,
      formatter: (v) => {
        if (extent?.only !== undefined && Math.abs(v - extent.only) > 1e-9) return "";
        return edgeLabel(v, xFormat);
      },
    },
    ...(extent ?? {}),
  };
  delete option.xAxis.only;
  if (extent && extent.only === undefined) {
    const steps = (extent.max - extent.min) / extent.interval;
    option.xAxis.axisLabel.showMaxLabel = Math.abs(steps - Math.round(steps)) < 1e-9;
  }
  option.yAxis.axisTick = { show: false };
  option.yAxis.minInterval = percent ? 0 : 1;
  option.legend.show = false;
  option.tooltip.trigger = "item";
  option.tooltip.formatter = (p) => {
    const bin = summary.bins[p.dataIndex];
    if (!bin) return "";
    const share = total ? bin.count / total : 0;
    return (
      `<div style="font-weight:600;margin-bottom:4px">${escapeHtml(humanize(spec.x))} ` +
      `${escapeHtml(binLabel(bin, xFormat))}</div>` +
      `${p.marker} ${escapeHtml(bin.count.toLocaleString())} ` +
      `${bin.count === 1 ? "row" : "rows"}` +
      `&nbsp;<span style="opacity:.6">${escapeHtml(formatValue(share, "percent"))}</span>` +
      `<div style="margin-top:6px;font-size:11px;opacity:.6">` +
      `${escapeHtml(histogramScope(summary, result, { full: true }))}</div>`
    );
  };
  const constant = extent?.only !== undefined;
  option.series = [
    {
      type: "custom",
      name: humanize(spec.x),
      encode: { x: [0, 1], y: 2 },
      clip: true,
      itemStyle: { color },
      emphasis: { itemStyle: { opacity: 0.82 } },
      renderItem: (params, api) => {
        const lo = constant ? extent.min + extent.interval * 0.4 : api.value(0);
        const hi = constant ? extent.max - extent.interval * 0.4 : api.value(1);
        const [x0, top] = api.coord([lo, api.value(2)]);
        const [x1, bottom] = api.coord([hi, 0]);
        const span = x1 - x0;
        const gap = span > 8 ? 1 : 0;
        const radius = Math.min(3, Math.max(0, span / 2 - gap));
        return {
          type: "rect",
          shape: {
            x: x0 + gap,
            y: top,
            width: Math.max(0.5, span - gap * 2),
            height: bottom - top,
            r: [radius, radius, 0, 0],
          },
          style: api.style(),
        };
      },
      data: summary.bins.map((bin) => [
        bin.lo,
        bin.hi,
        percent ? (total ? bin.count / total : 0) : bin.count,
      ]),
    },
  ];
  option.graphic = [
    {
      type: "text",
      left: 8,
      top: compact ? 2 : 8,
      silent: true,
      style: {
        text: histogramScope(summary, result),
        fill: cssVar("--ink-muted"),
        fontSize: 11,
        fontFamily: cssVar("--font") || "system-ui, sans-serif",
      },
    },
  ];
  return option;
}
