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
  setEmptyReason,
} from "./charts.js";

export const MAX_BINS = 200;
const MAX_FRACTION_DIGITS = 20;
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
  return Math.min(100, Math.max(0, frac - Number(exponent ?? 0)));
}

const SETTLE_STEPS = 4;
export const UNBINNABLE = "These values span too wide or too narrow a range to bin";

function clean(value, scale) {
  const noise = Math.abs(scale) * Number.EPSILON * 2;
  if (Math.abs(value) <= noise) return 0;
  const rounded = Number(value.toPrecision(15));
  return Math.abs(rounded - value) <= noise ? rounded : value;
}

function sum(a, b) {
  return clean(a + b, Math.abs(a) + Math.abs(b));
}

const SHORT_DECIMALS = 6;

function isShort(value) {
  return decimalsOf(value) <= SHORT_DECIMALS;
}

function crossesData(edge, shown, sorted) {
  if (shown === edge) return false;
  const next = sorted[firstAtLeast(sorted, Math.min(edge, shown))];
  return next !== undefined && next < Math.max(edge, shown);
}

function displayDigits(edges, width, sorted) {
  const tolerance = Math.abs(width) / 1000;
  for (let d = 0; d <= MAX_FRACTION_DIGITS; d += 1) {
    const shown = edges.map((e) => (isShort(e) ? e : Number(e.toFixed(d))));
    const faithful = shown.every(
      (v, i) =>
        Math.abs(v - edges[i]) <= tolerance &&
        (i === 0 || v > shown[i - 1]) &&
        !crossesData(edges[i], v, sorted)
    );
    if (faithful) return d;
  }
  return undefined;
}

function firstAtLeast(sorted, value) {
  let lo = 0;
  let hi = sorted.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (sorted[mid] >= value) hi = mid;
    else lo = mid + 1;
  }
  return lo;
}

function firstAbove(sorted, value) {
  let lo = 0;
  let hi = sorted.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (sorted[mid] > value) hi = mid;
    else lo = mid + 1;
  }
  return lo;
}

function settledEdge(raw, scale, sorted) {
  const tidy = clean(raw, scale);
  if (tidy === raw) return raw;
  const lo = Math.min(raw, tidy);
  const hi = Math.max(raw, tidy);
  const next = sorted[firstAbove(sorted, lo)];
  if (next !== undefined && next < hi) return raw;
  if (tidy > raw && sorted[firstAbove(sorted, raw) - 1] === raw) return raw;
  return tidy;
}

function resolvable(min, max, width) {
  const magnitude = Math.max(Math.abs(min), Math.abs(max));
  return (
    Number.isFinite(max - min) &&
    Number.isFinite(width) &&
    width > 0 &&
    width > magnitude * Number.EPSILON * 4
  );
}

function soundLayout({ count, edge }, min, max) {
  if (!(count >= 1 && count <= MAX_BINS)) return false;
  let previous = edge(0);
  if (!(previous <= min)) return false;
  for (let i = 1; i <= count; i += 1) {
    const next = edge(i);
    if (!Number.isFinite(next) || !(next > previous)) return false;
    previous = next;
  }
  return previous >= max && Number.isFinite(previous - edge(0));
}

function spanOver(min, max, n) {
  const span = max - min;
  return Number.isFinite(span) ? span / n : max / n - min / n;
}

function alignedEdges(min, max, width, anchor) {
  const at = (k) => anchor + k * width;
  let k0 = Math.floor(min / width - anchor / width);
  let count = Math.max(1, Math.ceil(max / width - at(k0) / width));
  if (!Number.isFinite(k0) || !Number.isFinite(count)) return { count: Infinity };
  if (count > MAX_BINS + 2) return { count };
  for (let n = 0; n < SETTLE_STEPS && at(k0) > min; n += 1) {
    k0 -= 1;
    count += 1;
  }
  for (let n = 0; n < SETTLE_STEPS && at(k0 + 1) <= min; n += 1) {
    k0 += 1;
    count -= 1;
  }
  count = Math.max(1, count);
  for (let n = 0; n < SETTLE_STEPS && count > 1 && at(k0 + count - 1) >= max; n += 1) count -= 1;
  for (let n = 0; n < SETTLE_STEPS && at(k0 + count) < max; n += 1) count += 1;
  return {
    count,
    width,
    raw: (i) => at(k0 + i),
    scale: (i) => Math.abs(anchor) + Math.abs((k0 + i) * width),
    exact: (i) => k0 + i === 0,
  };
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
  const unbinnable = () => {
    summary.unbinnable = true;
    return summary;
  };
  const anchor = Number.isFinite(binStart) ? binStart : 0;
  if (binWidth > 0) {
    const aligned = alignedEdges(min, max, binWidth, anchor);
    if (!(aligned.count <= MAX_BINS)) {
      summary.tooMany = aligned.count;
    } else if (min !== max && !resolvable(min, max, binWidth)) {
      return unbinnable();
    } else {
      layout = aligned;
      summary.mode = "width";
      summary.width = binWidth;
    }
  }
  const autoWidth = min === max ? 0 : niceStep(spanOver(min, max, autoCount(included.length)));
  if (!layout && min === max) {
    summary.bins = [{ lo: min, hi: max, count: included.length, last: true }];
    summary.mode = summary.tooMany ? "auto" : bins ? "count" : "auto";
    return summary;
  }
  if (!layout && Number.isInteger(bins) && bins >= 1 && !summary.tooMany) {
    const count = Math.min(bins, MAX_BINS);
    const width = spanOver(min, max, count);
    if (!resolvable(min, max, width)) return unbinnable();
    layout = {
      count,
      width,
      raw: (i) => (i === count ? max : min + i * width),
      scale: (i) => Math.abs(min) + Math.abs(i * width),
      exact: (i) => i === 0 || i === count,
    };
    summary.mode = "count";
    summary.width = width;
  }
  if (!layout) {
    if (!resolvable(min, max, autoWidth)) return unbinnable();
    layout = alignedEdges(min, max, autoWidth, 0);
    summary.width = autoWidth;
  }
  const sorted = Float64Array.from(included).sort();
  const edges = Array.from({ length: layout.count + 1 }, (_, i) => {
    const raw = layout.raw(i);
    return layout.exact(i) ? raw : settledEdge(raw, layout.scale(i), sorted);
  });
  const edge = (i) => edges[i];
  if (!soundLayout({ count: layout.count, edge }, min, max)) return unbinnable();

  summary.digits = displayDigits(edges, layout.width, sorted);

  const { count, width } = layout;
  const first = edge(0);
  const counts = new Array(count).fill(0);
  for (const v of included) {
    let i = Math.floor(v / width - first / width);
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

const CURRENCY_CODE = /^[A-Z]{3}$/;


function scientificDigits(value) {
  const mantissa = value.toExponential().split("e")[0];
  return (mantissa.split(".")[1] ?? "").length;
}

function numberText(value, fmt, digits, compact = false) {
  const { locale, currency } = formatSettings();
  if (digits > MAX_FRACTION_DIGITS) {
    return new Intl.NumberFormat(locale, {
      notation: "scientific",
      maximumFractionDigits: scientificDigits(value),
    }).format(value);
  }
  const notation = compact ? "compact" : "standard";
  if (fmt === "currency" || CURRENCY_CODE.test(fmt)) {
    try {
      return new Intl.NumberFormat(locale, {
        style: "currency",
        currency: CURRENCY_CODE.test(fmt) ? fmt : currency,
        notation,
        minimumFractionDigits: 0,
        maximumFractionDigits: digits,
      }).format(value);
    } catch {
      return formatValue(value, "compact", true);
    }
  }
  if (fmt === "percent") {
    return new Intl.NumberFormat(locale, {
      style: "percent",
      maximumFractionDigits: Math.max(0, digits - 2),
    }).format(value);
  }
  return new Intl.NumberFormat(locale, { notation, maximumFractionDigits: digits }).format(value);
}

export function exactText(value, fmt) {
  return numberText(value, fmt === "compact" ? "number" : fmt, decimalsOf(value));
}

export function binLabel(bin, fmt, digits) {
  if (bin.lo === bin.hi) return exactText(bin.lo, fmt);
  const text = (v) =>
    digits === undefined || isShort(v)
      ? exactText(v, fmt)
      : numberText(v, fmt === "compact" ? "number" : fmt, digits);
  return `${text(bin.lo)} to ${bin.last ? "" : "under "}${text(bin.hi)}`;
}

export function tickLabels(ticks, fmt) {
  const digits = Math.max(0, ...ticks.map(decimalsOf));
  const text = (v, compact) => {
    if (fmt === "percent") return numberText(v, fmt, digits);
    const big = compact && Math.abs(v) >= 10_000;
    return numberText(v, fmt, big ? Math.max(1, digits) : Math.max(2, digits), big);
  };
  const compactLabels = ticks.map((v) => text(v, true));
  const distinct = new Set(compactLabels).size === compactLabels.length;
  return distinct ? compactLabels : ticks.map((v) => text(v, false));
}

export function edgeLabel(value, fmt, interval = 0) {
  return tickLabels(interval > 0 ? [value, clean(interval, interval)] : [value], fmt)[0];
}

function countText(n) {
  return n.toLocaleString(formatSettings().locale);
}

export function histogramScope(summary, result, { full = false } = {}) {
  const parts = [];
  if (result.truncated && full) {
    parts.push(`first ${countText(result.row_count)} rows only, not the full distribution`);
  }
  parts.push(`${countText(summary.included)} ${summary.included === 1 ? "value" : "values"}`);
  if (summary.nulls) {
    parts.push(`${countText(summary.nulls)} ${summary.nulls === 1 ? "null" : "nulls"} excluded`);
  }
  if (summary.nonNumeric) {
    parts.push(`${countText(summary.nonNumeric)} non-numeric excluded`);
  }
  if (summary.tooMany) {
    parts.push(`bin_width would make ${countText(summary.tooMany)} bins, auto bins shown`);
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
  const max = bins[bins.length - 1].hi;
  const interval =
    summary.mode === "count"
      ? niceStep(spanOver(first.lo, max, TICK_TARGET))
      : (first.hi - first.lo) * Math.ceil(bins.length / TICK_TARGET);
  const base = summary.mode === "count" ? Math.ceil(first.lo / interval - 1e-9) * interval : first.lo;
  const ticks = [];
  for (let k = 0; ticks.length <= TICK_TARGET * 2; k += 1) {
    const tick = sum(base, k * interval);
    if (!(tick <= max + interval * 1e-9)) break;
    ticks.push(tick);
  }
  return { min: first.lo, max, interval, ticks };
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
  if (summary.unbinnable) setEmptyReason(option, UNBINNABLE);
  const ticks = extent?.ticks ?? [];
  const labels = tickLabels(ticks, xFormat);
  const tickText = new Map(ticks.map((t, i) => [t, labels[i]]));

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
        return tickText.get(v) ?? edgeLabel(v, xFormat, extent?.interval);
      },
    },
    ...(extent ?? {}),
  };
  delete option.xAxis.only;
  delete option.xAxis.ticks;
  if (extent && extent.only === undefined) {
    option.xAxis.axisLabel.customValues = extent.ticks;
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
      `${escapeHtml(binLabel(bin, xFormat, summary.digits))}</div>` +
      `${p.marker} ${escapeHtml(countText(bin.count))} ` +
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
  const scope = histogramScope(summary, result);
  option.series.push({
    type: "custom",
    name: "scope",
    silent: true,
    clip: false,
    tooltip: { show: false },
    encode: { x: [], y: [] },
    data: [[scope]],
    renderItem: (params, api) => ({
      type: "text",
      x: 8,
      y: compact ? 2 : 8,
      silent: true,
      style: {
        text: scope,
        width: Math.max(0, api.getWidth() - 16),
        overflow: "truncate",
        ellipsis: "…",
        fill: cssVar("--ink-muted"),
        fontSize: 11,
        fontFamily: cssVar("--font") || "system-ui, sans-serif",
      },
    }),
  });
  return option;
}
