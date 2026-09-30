import assert from "node:assert/strict";
import { test } from "node:test";

import { inferSpec, pruneSpecForType, setFormatConfig } from "../sqldash/static/js/charts.js";
import {
  buildCells,
  cellMeasure,
  colorRange,
  colorStops,
  divergingNeutral,
  isDark,
  duplicateNote,
  heatmapScope,
  MAX_CATEGORIES,
} from "../sqldash/static/js/heatmap.js";

setFormatConfig({ locale: "en-US" });

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
    "60 of 150 x values"
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

test("a null category and the text 'null' stay two categories, and objects keep their own", () => {
  const rows = [
    [null, "a", 10],
    ["null", "a", 20],
    [{ k: 1 }, "a", 1],
    [{ k: 2 }, "a", 2],
  ];
  const r = result([["x", "string"], ["y", "string"], ["v", "integer"]], rows);
  const built = buildCells(r, { x: "x", y: "y", value: "v", aggregate: "sum" });
  assert.deepEqual(built.xs, ["null", '{"k":1}', '{"k":2}', "null"]);
  assert.deepEqual(
    [...built.cells].sort((a, b) => a.i - b.i).map((c) => c.value),
    [20, 1, 2, 10]
  );
  assert.equal(buildCells(r, { x: "x", y: "y", value: "v" }).duplicates, 0);
});

test("min and max over a very large cell do not blow the stack", () => {
  const rows = Array.from({ length: 200_000 }, (_, i) => ["a", "b", i]);
  const r = result([["x", "string"], ["y", "string"], ["v", "integer"]], rows);
  assert.equal(buildCells(r, { x: "x", y: "y", value: "v", aggregate: "max" }).cells[0].value, 199_999);
  assert.equal(buildCells(r, { x: "x", y: "y", value: "v", aggregate: "min" }).cells[0].value, 0);
  assert.deepEqual(colorRange(rows.map((row) => row[2]), "sequential"), { min: 0, max: 199_999 });
});

test("big integer categories sort by their exact value", () => {
  const r = result(
    [["x", "integer"], ["y", "string"], ["v", "integer"]],
    [
      ["9007199254740993", "a", 1],
      ["9007199254740992", "a", 2],
    ]
  );
  assert.deepEqual(buildCells(r, { x: "x", y: "y", value: "v" }).xs, [
    "9007199254740992",
    "9007199254740993",
  ]);
});

test("switching to a heatmap keeps one y column, with or without a result", () => {
  const line = { type: "line", x: "day", y: ["hour", "orders"] };
  assert.equal(pruneSpecForType(line, "heatmap").y, "hour");
  assert.deepEqual(pruneSpecForType({ type: "heatmap", x: "a", y: "b" }, "bar").y, ["b"]);
  const r = result([["x", "string"], ["r", "string"], ["v", "integer"]], [["a", "us", 1]]);
  assert.deepEqual(buildCells(r, { x: "x", y: ["r"], value: "v" }).ys, ["us"]);
});

test("the diverging midpoint stands apart from the tile in both themes", () => {
  const themes = {
    light: { surface: "#fdfdfc", ink: "#0b0b0b" },
    dark: { surface: "#161617", ink: "#f5f5f4" },
  };
  const lum = (rgb) => {
    const [r, g, b] = rgb.match(/\d+/g).map(Number);
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255;
  };
  const surfaceLum = { light: lum("rgb(253, 253, 252)"), dark: lum("rgb(22, 22, 23)") };
  for (const [name, { surface, ink }] of Object.entries(themes)) {
    assert.equal(isDark(surface), name === "dark");
    const neutral = divergingNeutral(surface, ink);
    const stops = colorStops("diverging", { accent: "#3987e5", low: "#d95926", surface, neutral });
    assert.equal(stops[2], neutral);
    assert.ok(Math.abs(lum(neutral) - surfaceLum[name]) > 0.1, `${name} ${neutral}`);
    assert.equal(stops[0], "#d95926");
    assert.equal(stops[4], "#3987e5");
  }
});

test("JSON scalars of different types are different categories", () => {
  const r = result(
    [["x", "json"], ["y", "string"], ["v", "integer"]],
    [
      [1, "a", 10],
      ["1", "a", 20],
      [true, "a", 1],
      ["true", "a", 2],
    ]
  );
  const built = buildCells(r, { x: "x", y: "y", value: "v", aggregate: "sum" });
  assert.equal(built.cells.length, 4);
  assert.equal(buildCells(r, { x: "x", y: "y", value: "v" }).duplicates, 0);
});

test("numeric order entries pin decimal categories by value", () => {
  const r = result(
    [["x", "decimal"], ["y", "string"], ["v", "integer"]],
    [
      ["1.00", "a", 1],
      ["2.00", "a", 2],
    ]
  );
  const built = buildCells(r, { x: "x", y: "y", value: "v", x_order: [2, 1] });
  assert.deepEqual(built.xs, ["2.00", "1.00"]);
  assert.equal(built.cells.length, 2);
  const big = result(
    [["x", "integer"], ["y", "string"], ["v", "integer"]],
    [
      ["9007199254740992", "a", 1],
      ["9007199254740993", "a", 2],
    ]
  );
  const pinned = buildCells(big, { x: "x", y: "y", value: "v", x_order: ["9007199254740993"] });
  assert.deepEqual(pinned.xs, ["9007199254740993", "9007199254740992"]);
});

test("number and boolean pins still match a text column", () => {
  const r = result(
    [["x", "string"], ["y", "string"], ["v", "integer"]],
    [
      ["2020", "a", 1],
      ["2021", "a", 2],
      ["true", "a", 3],
    ]
  );
  assert.deepEqual(buildCells(r, { x: "x", y: "y", value: "v", x_order: [2021, true] }).xs, [
    "2021",
    "true",
    "2020",
  ]);
});

test("an average of huge values stays finite, and a sum too big to hold is flagged", () => {
  const r = result(
    [["x", "string"], ["y", "string"], ["v", "float"]],
    [
      ["a", "b", 1e308],
      ["a", "b", 1e308],
    ]
  );
  assert.equal(buildCells(r, { x: "x", y: "y", value: "v", aggregate: "avg" }).cells[0].value, 1e308);
  const summed = buildCells(r, { x: "x", y: "y", value: "v", aggregate: "sum" });
  assert.equal(summed.overflow, 1);
  assert.equal(buildCells(r, { x: "x", y: "y", value: "v", aggregate: "avg" }).overflow, 0);
});

test("timestamps with offsets sort by instant", () => {
  const r = result(
    [["x", "timestamp"], ["y", "string"], ["v", "integer"]],
    [
      ["2026-01-01T00:30:00+00:00", "a", 1],
      ["2026-01-01T01:00:00+02:00", "a", 2],
    ]
  );
  assert.deepEqual(buildCells(r, { x: "x", y: "y", value: "v" }).xs, [
    "2026-01-01T01:00:00+02:00",
    "2026-01-01T00:30:00+00:00",
  ]);
});

test("count keeps every row, so nothing is reported as left out", () => {
  const r = result([["x", "string"], ["y", "string"], ["v", "string"]], [["a", "b", "hello"]]);
  const spec = { x: "x", y: "y", value: "v", aggregate: "count" };
  const built = buildCells(r, spec);
  assert.equal(built.cells[0].value, 1);
  assert.equal(heatmapScope(built, spec, r), "");
});

test("the row cap is left to the tile note, and counts follow the dashboard locale", () => {
  const rows = result(
    [["x", "string"], ["y", "string"], ["v", "integer"]],
    Array.from({ length: 1500 }, (_, i) => [`x${i}`, "y", i]),
    { truncated: true, row_count: 1500 }
  );
  const spec = { x: "x", y: "y", value: "v" };
  const built = buildCells(rows, spec);
  assert.equal(heatmapScope(built, spec, rows), "60 of 1,500 x values");
  setFormatConfig({ locale: "de-DE" });
  try {
    assert.equal(heatmapScope(built, spec, rows), "60 of 1.500 x values");
  } finally {
    setFormatConfig({ locale: "en-US" });
  }
});
