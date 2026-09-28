import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { test } from "node:test";

import {
  cleanReference,
  pruneSpecForType,
  referenceExtent,
  setFormatConfig,
  translate,
} from "../sqldash/static/js/charts.js";

setFormatConfig({ locale: "en-US" });
globalThis.document = { documentElement: {} };
globalThis.getComputedStyle = () => ({ getPropertyValue: () => "#123456" });

const weekly = {
  columns: [
    { name: "week", type: "date" },
    { name: "revenue", type: "float" },
  ],
  rows: [
    ["2026-08-03", 50000],
    ["2026-08-10", 51000],
    ["2026-08-17", 57000],
  ],
};

const categories = {
  columns: [
    { name: "category", type: "string" },
    { name: "revenue", type: "float" },
  ],
  rows: [
    ["electronics", 165000],
    ["home", 106000],
  ],
};

const carrier = (option, name) => option.series.find((s) => s.name === name);

test("a reference inside the data leaves the axis to ECharts", () => {
  assert.equal(referenceExtent({ min: 0, max: 57000 }, [40000]), null);
  assert.equal(referenceExtent({ min: -3000, max: 20000 }, [0]), null);
});

test("a reference past the data widens the axis to a nice bound that holds it", () => {
  assert.deepEqual(referenceExtent({ min: 24000, max: 57000 }, [120000]), { min: 0, max: 120000 });
  assert.deepEqual(referenceExtent({ min: -3000, max: 20000 }, [-30000]), { min: -30000, max: 20000 });
  assert.deepEqual(referenceExtent({ min: 0.1, max: 0.4 }, [0.95]), { min: 0, max: 1 });
});

test("a reference on an empty chart still gets an axis", () => {
  assert.deepEqual(referenceExtent({ min: Infinity, max: -Infinity }, [5]), { min: 0, max: 5 });
});

test("value lines, bands and x markers land on the right axes", () => {
  const option = translate(
    {
      type: "line",
      x: "week",
      y: ["revenue"],
      format: "currency",
      references: [
        { y: 120000, label: "Stretch goal" },
        { y: [40000, 60000], label: "Healthy range" },
        { x: "2026-08-10", label: "Launch" },
        { x: ["2026-08-03", "2026-08-17"], label: "Promo" },
      ],
    },
    weekly
  );
  const lines = carrier(option, "__reference_lines").markLine.data;
  assert.equal(lines[0].yAxis, 120000);
  assert.equal(lines[0].label.formatter, "Stretch goal  $120,000");
  assert.equal(lines[0].lineStyle.type, "dashed");
  assert.equal(lines.find((l) => l.xAxis === "2026-08-10").label.formatter, "Launch");
  const bands = carrier(option, "__reference_bands").markArea.data;
  assert.deepEqual([bands[0][0].yAxis, bands[0][1].yAxis], [40000, 60000]);
  assert.deepEqual([bands[1][0].xAxis, bands[1][1].xAxis], ["2026-08-03", "2026-08-17"]);
  assert.deepEqual(option.yAxis.max({ min: 50000, max: 57000 }), 120000);
});

test("reference carriers plot no data, stay out of the legend and tooltip", () => {
  const option = translate(
    { type: "line", x: "week", y: ["revenue", "revenue"], references: [{ y: 1 }] },
    weekly
  );
  const refs = carrier(option, "__reference_lines");
  assert.deepEqual(refs.data, []);
  assert.equal(refs.tooltip.show, false);
  assert.ok(!option.legend.data.includes("__reference_lines"));
});

test("a horizontal bar puts value references on the x axis", () => {
  const option = translate(
    {
      type: "bar",
      orientation: "horizontal",
      x: "category",
      y: ["revenue"],
      references: [{ y: 150000, label: "Quota" }, { x: "home", label: "Focus" }],
    },
    categories
  );
  const lines = carrier(option, "__reference_lines").markLine.data;
  assert.equal(lines[0].xAxis, 150000);
  assert.equal(lines[1].yAxis, "home");
});

test("a category marker that is not in the result is left off, not misplaced", () => {
  const option = translate(
    { type: "bar", x: "category", y: ["revenue"], references: [{ x: "garden" }, { y: 1 }] },
    categories
  );
  const lines = carrier(option, "__reference_lines").markLine.data;
  assert.equal(lines.length, 1);
  assert.equal(lines[0].yAxis, 1);
});

test("an unresolved metric reference draws nothing", () => {
  const option = translate(
    { type: "bar", x: "category", y: ["revenue"], references: [{ metric: "target", y: null }] },
    categories
  );
  assert.equal(carrier(option, "__reference_lines"), undefined);
});

test("a narrow chart keeps the reference name and drops its number", () => {
  const option = translate(
    { type: "bar", x: "category", y: ["revenue"], format: "currency", references: [{ y: 150000, label: "Goal" }] },
    categories,
    undefined,
    200,
    220
  );
  assert.equal(carrier(option, "__reference_lines").markLine.data[0].label.formatter, "Goal");
});

test("references survive a switch between x/y types and drop on pie", () => {
  const spec = { type: "bar", x: "c", y: ["n"], references: [{ y: 5 }] };
  assert.deepEqual(pruneSpecForType(spec, "line").references, [{ y: 5 }]);
  assert.equal(pruneSpecForType(spec, "pie").references, undefined);
});

test("cleanReference writes only what the author set, and nothing half-filled", () => {
  assert.deepEqual(
    cleanReference({ y: "5000", x: null, metric: null, label: "Target", color: null, style: null, format: null }),
    { y: 5000, label: "Target" }
  );
  assert.deepEqual(cleanReference({ y: ["1", "2"] }), { y: [1, 2] });
  assert.equal(cleanReference({ y: "" }), null);
  assert.equal(cleanReference({ y: ["1", ""] }), null);
  assert.equal(cleanReference({ x: "" }), null);
  assert.deepEqual(cleanReference({ x: "2026-09-01", label: "" }), { x: "2026-09-01" });
  assert.deepEqual(cleanReference({ metric: "aov", color: "good" }), { metric: "aov", color: "good" });
  assert.equal(cleanReference({ metric: "" }), null);
});

test("a category marker matches its category exactly, not a longer name", () => {
  const rows = {
    columns: categories.columns,
    rows: [
      ["homeware", 5],
      ["homes", 3],
    ],
  };
  const option = translate(
    { type: "bar", x: "category", y: ["revenue"], references: [{ x: "home" }, { x: "homes" }] },
    rows
  );
  const lines = carrier(option, "__reference_lines").markLine.data;
  assert.deepEqual(lines.map((l) => l.xAxis), ["homes"]);
});

test("a date marker still finds its day on a timestamp category axis", () => {
  const rows = {
    columns: [
      { name: "day", type: "timestamp" },
      { name: "n", type: "integer" },
    ],
    rows: [
      ["2026-09-01T00:00:00", 5],
      ["2026-09-02T00:00:00", 3],
    ],
  };
  const option = translate({ type: "bar", x: "day", y: ["n"], references: [{ x: "2026-09-02" }] }, rows);
  assert.equal(carrier(option, "__reference_lines").markLine.data[0].xAxis, "2026-09-02T00:00:00");
});

test("a whole currency reference drops the cents", () => {
  const option = translate(
    {
      type: "bar",
      x: "category",
      y: ["revenue"],
      format: "currency",
      references: [{ y: 0, label: "Break-even" }, { y: 12.5 }, { y: [-20, 20] }],
    },
    categories
  );
  const texts = carrier(option, "__reference_lines").markLine.data.map((l) => l.label.formatter);
  assert.deepEqual(texts, ["Break-even  $0", "$12.50", "-$20 – $20"]);
});

test("a band label moves below the band when a line runs through it or it is too thin", () => {
  const change = {
    columns: [
      { name: "week", type: "string" },
      { name: "change", type: "float" },
    ],
    rows: [
      ["Aug 03", 20000],
      ["Aug 10", -3000],
    ],
  };
  const edge = (references, height = 300) =>
    carrier(translate({ type: "bar", x: "week", y: ["change"], references }, change, undefined, height), "__reference_lines")
      .markLine.data.find((l) => l.lineStyle.color === "transparent").yAxis;
  assert.equal(edge([{ y: [-2000, 2000], label: "Noise" }, { y: 0, label: "Break-even" }], 0), -2000);
  assert.equal(edge([{ y: [-500, 500], label: "Noise" }]), -500);
  assert.equal(edge([{ y: [5000, 15000], label: "Healthy" }]), 15000);
  assert.equal(edge([{ y: [5000, 15000], label: "Healthy" }], 0), 15000);
});

test("a band on a large result finds the extent without spreading the data into a call", () => {
  const names = Array.from({ length: 16 }, (_, i) => `s${i}`);
  const big = {
    columns: [{ name: "x", type: "string" }, ...names.map((name) => ({ name, type: "float" }))],
    rows: Array.from({ length: 10000 }, (_, r) => [`r${r}`, ...names.map((_, i) => r + i)]),
  };
  const option = translate(
    { type: "line", x: "x", y: names, references: [{ y: [5, 10], label: "Band" }] },
    big,
    undefined,
    300
  );
  assert.ok(carrier(option, "__reference_bands"));
});

test("a color_by value bar on a large result sets its color range", () => {
  const big = {
    columns: [
      { name: "x", type: "string" },
      { name: "n", type: "float" },
    ],
    rows: Array.from({ length: 200000 }, (_, r) => [`r${r}`, r]),
  };
  const option = translate({ type: "bar", x: "x", y: ["n"], color_by: "value" }, big);
  assert.deepEqual([option.visualMap.min, option.visualMap.max], [0, 199999]);
});

const echarts = createRequire(import.meta.url)("../sqldash/static/vendor/echarts.min.js");

function renderedSvg(option) {
  const fake = globalThis.document;
  delete globalThis.document;
  try {
    const chart = echarts.init(null, null, { renderer: "svg", ssr: true, width: 400, height: 300 });
    chart.setOption(option);
    const svg = chart.renderToSVGString();
    chart.dispose();
    return svg;
  } finally {
    globalThis.document = fake;
  }
}

test("a marker on a numeric category draws on that category", () => {
  for (const rows of [
    [[1, 5], [2, 10]],
    [[1.5, 5], [2.5, 10]],
  ]) {
    const result = {
      columns: [
        { name: "k", type: "float" },
        { name: "n", type: "float" },
      ],
      rows,
    };
    const option = translate(
      { type: "bar", x: "k", y: ["n"], references: [{ x: rows[1][0], label: "Marker" }, { x: 7 }] },
      result
    );
    const lines = carrier(option, "__reference_lines").markLine.data;
    assert.deepEqual(lines.map((l) => l.xAxis), [1]);
    assert.ok(renderedSvg(option).includes(">Marker<"), rows);
  }
});
