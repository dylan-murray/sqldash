import assert from "node:assert/strict";
import { test } from "node:test";

import { pruneSpecForType, setFormatConfig } from "../sqldash/static/js/charts.js";
import {
  binLabel,
  binValues,
  edgeLabel,
  histogramScope,
  MAX_BINS,
} from "../sqldash/static/js/histogram.js";

setFormatConfig({ locale: "en-US" });

const edges = (summary) => summary.bins.map((b) => [b.lo, b.hi, b.count]);
const total = (summary) => summary.bins.reduce((n, b) => n + b.count, 0);

test("a value on an inner edge goes to the bin above; the maximum stays in the last bin", () => {
  const summary = binValues([0, 9.99, 10, 20, 30], { bin_width: 10 });
  assert.deepEqual(edges(summary), [
    [0, 10, 2],
    [10, 20, 1],
    [20, 30, 2],
  ]);
  assert.equal(summary.mode, "width");
});

test("bin_width edges sit on multiples of the width, negatives included", () => {
  const summary = binValues([-25, -0.5, 0, 0.5, 14], { bin_width: 10 });
  assert.deepEqual(edges(summary), [
    [-30, -20, 1],
    [-20, -10, 0],
    [-10, 0, 1],
    [0, 10, 2],
    [10, 20, 1],
  ]);
});

test("bin_start shifts the edges without dropping values below it", () => {
  const summary = binValues([1, 5, 14, 15], { bin_width: 10, bin_start: 5 });
  assert.deepEqual(edges(summary), [
    [-5, 5, 1],
    [5, 15, 3],
  ]);
});

test("decimal widths do not drift into the wrong bin", () => {
  const summary = binValues([0.1, 0.2, 0.3, 0.7], { bin_width: 0.1 });
  assert.deepEqual(
    summary.bins.map((b) => b.count),
    [1, 1, 1, 0, 0, 1]
  );
  assert.equal(summary.bins[2].lo, 0.3);
});

test("bins asks for exactly that many equal bins from min to max", () => {
  const summary = binValues([1, 2, 3, 4], { bins: 3 });
  assert.deepEqual(edges(summary), [
    [1, 2, 1],
    [2, 3, 1],
    [3, 4, 2],
  ]);
  assert.equal(summary.mode, "count");
});

test("counts sum to the numeric rows; nulls and non-numbers are counted, not binned", () => {
  const values = [3, null, "abc", "NaN", "Infinity", "12.50", undefined, true, 7, -2];
  const summary = binValues(values, {});
  assert.equal(summary.included, 4);
  assert.equal(summary.nulls, 2);
  assert.equal(summary.nonNumeric, 4);
  assert.equal(total(summary), 4);
});

test("decimal and big-integer text from the warehouse bins as numbers", () => {
  const summary = binValues(["9007199254740993", "9007199254740995", "0.5"], { bins: 2 });
  assert.equal(summary.included, 3);
  assert.equal(total(summary), 3);
});

test("a constant column is one bin holding every row", () => {
  for (const options of [{}, { bins: 8 }]) {
    const summary = binValues([42, 42, 42], options);
    assert.deepEqual(edges(summary), [[42, 42, 3]]);
    assert.equal(binLabel(summary.bins[0], "number"), "42");
  }
  assert.deepEqual(edges(binValues([42, 42], { bin_width: 5 })), [[40, 45, 2]]);
});

test("auto bins use a rounded width and cover every value", () => {
  const values = Array.from({ length: 1000 }, (_, i) => (i * 37) % 997);
  const summary = binValues(values, {});
  assert.equal(summary.mode, "auto");
  assert.equal(total(summary), 1000);
  assert.ok([1, 2, 2.5, 5].some((f) => Number.isInteger(summary.width / f)), summary.width);
  assert.ok(summary.bins[0].lo <= 0);
  assert.ok(summary.bins.at(-1).hi >= 996);
});

test("a bin_width too fine for the range falls back to auto and says so", () => {
  const summary = binValues([1, 1e9], { bin_width: 0.01 });
  assert.ok(summary.tooMany > MAX_BINS);
  assert.equal(summary.mode, "auto");
  assert.ok(summary.bins.length <= MAX_BINS);
  assert.match(histogramScope(summary, { truncated: false }), /auto bins shown/);
});

test("huge ranges still bin", () => {
  const summary = binValues([-1e12, 3, 1e12], {});
  assert.equal(total(summary), 3);
  assert.ok(summary.bins.length <= 40);
});

test("the scope line names what was left out and leaves truncation to the tile note", () => {
  const summary = binValues([1, 2, null, "x"], {});
  const scope = histogramScope(summary, { truncated: true, row_count: 4 });
  assert.equal(scope, "2 values · 1 null excluded · 1 non-numeric excluded");
  assert.match(
    histogramScope(summary, { truncated: true, row_count: 4 }, { full: true }),
    /^First 4 rows only, not the full distribution/
  );
  const nulls = binValues([1, null, null], {});
  assert.equal(histogramScope(nulls, { truncated: false }), "1 value · 2 nulls excluded");
});

test("a value just past an edge opens the next bin instead of being rounded into the last", () => {
  assert.deepEqual(edges(binValues([0, 10, 10.01], { bin_width: 10 })), [
    [0, 10, 1],
    [10, 20, 2],
  ]);
  const auto = binValues([0, 1000.01], {});
  assert.ok(auto.bins.at(-1).hi >= 1000.01, JSON.stringify(edges(auto)));
  assert.equal(total(auto), 2);
});

test("bins: N edges are clean decimals and stay lower-inclusive", () => {
  const summary = binValues([0, 0.3, 1], { bins: 10 });
  assert.equal(summary.bins[3].lo, 0.3);
  assert.equal(summary.bins[3].count, 1);
  assert.equal(summary.bins[2].count, 0);
  assert.ok(summary.bins.every((b) => String(b.lo).length < 6), JSON.stringify(edges(summary)));
});

test("fine bins get labels precise enough to tell them apart", () => {
  const summary = binValues([1, 1.001, 1.002], { bin_width: 0.001 });
  assert.deepEqual(
    summary.bins.map((b) => binLabel(b, "number")),
    ["1 to under 1.001", "1.001 to 1.002"]
  );
  assert.equal(edgeLabel(1.001, "number", 0.001), "1.001");
});

test("tiny values bin across their own range", () => {
  const summary = binValues([1e-18, 2e-18], {});
  assert.ok(summary.bins.length > 1);
  assert.ok(summary.bins.every((b) => b.hi > b.lo));
  assert.equal(summary.bins[0].count, 1);
  assert.equal(summary.bins.at(-1).count, 1);
});

test("values too far apart or too close together to bin are flagged, not drawn wrong", () => {
  for (const [values, options] of [
    [[-1e308, 1e308], {}],
    [[-1e308, 1e308], { bins: 10 }],
    [[1e16, 1e16 + 2], {}],
    [[1e16, 1e16 + 2], { bin_width: 0.5 }],
  ]) {
    const summary = binValues(values, options);
    assert.equal(summary.unbinnable, true, JSON.stringify([values, options]));
    assert.deepEqual(summary.bins, []);
  }
});

test("large but representable edges are kept exactly instead of rounded together", () => {
  assert.deepEqual(edges(binValues([1e15, 1e15 + 1, 1e15 + 2], { bins: 2 })), [
    [1e15, 1e15 + 1, 1],
    [1e15 + 1, 1e15 + 2, 2],
  ]);
});

test("labels keep the authored bin_start offset", () => {
  const summary = binValues([0.001, 1.001, 2.001], { bin_width: 1, bin_start: 0.001 });
  assert.deepEqual(
    summary.bins.map((b) => binLabel(b, "number", summary.digits)),
    ["0.001 to under 1.001", "1.001 to 2.001"]
  );
});

test("tiny edges switch to scientific labels instead of all reading 0", () => {
  const summary = binValues([1e-22, 2e-22], {});
  const labels = summary.bins.map((b) => binLabel(b, "number", summary.digits));
  assert.equal(new Set(labels).size, labels.length, labels.join(" | "));
  assert.equal(labels[0], "1E-22 to under 1.25E-22");
});

test("counts in the scope line follow the dashboard locale", () => {
  const summary = binValues([...Array(1500).keys(), null], {});
  setFormatConfig({ locale: "de-DE" });
  try {
    assert.equal(histogramScope(summary, { truncated: false }), "1.500 values · 1 null excluded");
  } finally {
    setFormatConfig({ locale: "en-US" });
  }
  assert.equal(histogramScope(summary, { truncated: false }), "1,500 values · 1 null excluded");
});

test("interval labels say which edge is included", () => {
  const summary = binValues([0, 5, 10], { bin_width: 5 });
  assert.equal(binLabel(summary.bins[0], "number"), "0 to under 5");
  assert.equal(binLabel(summary.bins.at(-1), "number"), "5 to 10");
});

test("histogram fields do not leak into other chart types", () => {
  const spec = { type: "histogram", x: "amount", bins: 10, bin_width: null, measure: "percent" };
  const bar = pruneSpecForType(spec, "bar");
  assert.equal(bar.bins, undefined);
  assert.equal(bar.measure, undefined);
  assert.equal(bar.x, "amount");
  const back = pruneSpecForType({ type: "bar", x: "a", y: ["b"], stacked: true }, "histogram");
  assert.deepEqual(back, { type: "histogram", x: "a" });
});
