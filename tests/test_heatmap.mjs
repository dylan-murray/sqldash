import assert from "node:assert/strict";
import { test } from "node:test";

import { inferSpec, pruneSpecForType } from "../sqldash/static/js/charts.js";
import {
  buildCells,
  cellMeasure,
  colorRange,
  duplicateNote,
  heatmapScope,
  MAX_CATEGORIES,
} from "../sqldash/static/js/heatmap.js";

const result = (columns, rows, extra = {}) => ({
  columns: columns.map(([name, type]) => ({ name, type })),
  rows,
  row_count: rows.length,
  truncated: false,
  ...extra,
});

const SALES = result(
  [["region", "string"], ["category", "string"], ["amount", "float"]],
  [
    ["us", "toys", 10],
    ["us", "toys", 30],
    ["us", "home", 0],
    ["eu", "toys", -5],
    ["eu", "home", null],
    ["eu", "home", "7.5"],
  ]
);
const spec = (extra) => ({ type: "heatmap", x: "category", y: "region", value: "amount", ...extra });
const valueAt = (built, x, y) =>
  built.cells.find((c) => built.xs[c.i] === x && built.ys[c.j] === y)?.value;

test("every aggregation reads the cell's own rows and skips null values", () => {
  const expected = {
    sum: { toys_us: 40, toys_eu: -5, home_eu: 7.5 },
    avg: { toys_us: 20, toys_eu: -5, home_eu: 7.5 },
    min: { toys_us: 10, toys_eu: -5, home_eu: 7.5 },
    max: { toys_us: 30, toys_eu: -5, home_eu: 7.5 },
    count: { toys_us: 2, toys_eu: 1, home_eu: 2 },
  };
  for (const [aggregate, want] of Object.entries(expected)) {
    const built = buildCells(SALES, spec({ aggregate }));
    assert.equal(built.duplicates, 0);
    assert.equal(valueAt(built, "toys", "us"), want.toys_us, aggregate);
    assert.equal(valueAt(built, "toys", "eu"), want.toys_eu, aggregate);
    assert.equal(valueAt(built, "home", "eu"), want.home_eu, aggregate);
  }
});

test("duplicate cells without an aggregate are reported, never overwritten", () => {
  const built = buildCells(SALES, spec({}));
  assert.equal(built.duplicates, 2);
  assert.match(duplicateNote(built.duplicates), /^2 cells have more than one row\. Set aggregate/);
  assert.match(duplicateNote(1), /^1 cell has/);
});

test("a zero is a value; a missing combination and an all-null cell are not", () => {
  const rows = result(
    [["x", "string"], ["y", "string"], ["v", "integer"]],
    [
      ["a", "p", 0],
      ["b", "p", null],
      ["a", "q", 4],
    ]
  );
  const built = buildCells(rows, { x: "x", y: "y", value: "v" });
  assert.equal(valueAt(built, "a", "p"), 0);
  assert.equal(valueAt(built, "b", "p"), null);
  assert.equal(valueAt(built, "b", "q"), undefined);
  const counted = buildCells(rows, { x: "x", y: "y", value: "v", aggregate: "count" });
  assert.equal(valueAt(counted, "b", "p"), 1);
});

test("categories keep the result's order unless pinned; numbers and dates sort", () => {
  const rows = result(
    [["day", "string"], ["hour", "integer"], ["n", "integer"]],
    [
      ["Wed", 10, 1],
      ["Mon", 9, 1],
      ["Tue", 23, 1],
      [null, 2, 1],
      ["Mon", 11, 1],
    ]
  );
  const built = buildCells(rows, { x: "hour", y: "day", value: "n", aggregate: "sum" });
  assert.deepEqual(built.xs, ["2", "9", "10", "11", "23"]);
  assert.deepEqual(built.ys, ["Wed", "Mon", "Tue", "null"]);
  const pinned = buildCells(rows, {
    x: "hour",
    y: "day",
    value: "n",
    aggregate: "sum",
    y_order: ["Mon", "Tue", "Wed", "Thu"],
  });
  assert.deepEqual(pinned.ys, ["Mon", "Tue", "Wed", "Thu", "null"]);
});

test("high cardinality is capped per axis and the scope says so", () => {
  const rows = result(
    [["x", "string"], ["y", "string"], ["v", "integer"]],
    Array.from({ length: 150 }, (_, i) => [`x${i}`, `y${i % 3}`, i])
  );
  const built = buildCells(rows, { x: "x", y: "y", value: "v" });
  assert.equal(built.xs.length, MAX_CATEGORIES);
  assert.equal(built.xTotal, 150);
  assert.equal(built.hiddenRows, 90);
  assert.equal(
    heatmapScope(built, { x: "x", y: "y", value: "v" }, { truncated: true, row_count: 150 }),
    "First 150 rows only · 60 of 150 x values"
  );
});

test("non-numeric values are left out and disclosed", () => {
  const rows = result(
    [["x", "string"], ["y", "string"], ["v", "string"]],
    [
      ["a", "p", "n/a"],
      ["a", "q", "3"],
    ]
  );
  const built = buildCells(rows, { x: "x", y: "y", value: "v" });
  assert.equal(built.nonNumeric, 1);
  assert.equal(valueAt(built, "a", "p"), null);
  assert.match(heatmapScope(built, { x: "x", y: "y", value: "v" }, {}), /1 non-numeric v left out/);
});

test("a diverging range is symmetric around its midpoint", () => {
  assert.deepEqual(colorRange([-2, 10], "diverging", 0), { min: -10, max: 10 });
  assert.deepEqual(colorRange([80, 130], "diverging", 100), { min: 70, max: 130 });
  assert.deepEqual(colorRange([3, 9], "sequential"), { min: 3, max: 9 });
  assert.deepEqual(colorRange([5, 5], "sequential"), { min: 4, max: 5 });
});

test("the tooltip measure names the aggregation", () => {
  assert.equal(cellMeasure({ value: "order_total", aggregate: "sum" }), "Sum of order total");
  assert.equal(cellMeasure({ value: "order_total", aggregate: "count" }), "Rows");
  assert.equal(cellMeasure({ value: "order_total" }), "order total");
});

test("inference picks two dimensions and a numeric value", () => {
  const rows = result(
    [["weekday", "string"], ["hour", "integer"], ["orders", "integer"]],
    [["Mon", 9, 3]]
  );
  const s = inferSpec({ type: "heatmap" }, rows);
  assert.deepEqual([s.x, s.y, s.value], ["weekday", "hour", "orders"]);
  const stale = inferSpec({ type: "heatmap", y: ["gone"] }, rows);
  assert.equal(stale.y, "hour");
});

test("heatmap keys do not leak into other chart types", () => {
  const next = pruneSpecForType(
    spec({ aggregate: "sum", palette: "diverging", midpoint: 0, y_order: ["a"] }),
    "bar"
  );
  assert.deepEqual(next, { type: "bar", x: "category", y: ["region"] });
  const back = pruneSpecForType({ type: "bar", stacked: true, group_by: "g", x: "a" }, "heatmap");
  assert.deepEqual(back, { type: "heatmap", x: "a" });
});
