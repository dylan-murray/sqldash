import assert from "node:assert/strict";
import { test } from "node:test";

import {
  cleanCombo,
  formatValue,
  pruneSpecForType,
  setFormatConfig,
  translate,
} from "../sqldash/static/js/charts.js";

setFormatConfig({ locale: "en-US" });
globalThis.document = { documentElement: {} };
globalThis.getComputedStyle = () => ({ getPropertyValue: () => "#123456" });

const weekly = {
  columns: [
    { name: "week", type: "string" },
    { name: "revenue", type: "float" },
    { name: "rate", type: "float" },
    { name: "orders", type: "integer" },
  ],
  rows: [
    ["w1", 50000, 0.12, 400],
    ["w2", 51000, null, 410],
    ["w3", -3000, -0.05, 390],
  ],
};

const combo = {
  type: "bar",
  x: "week",
  y: ["revenue", "rate"],
  format: { revenue: "currency", rate: "percent" },
  series: { rate: { type: "line", axis: "right", label: "Conversion" } },
  axes: { left: { title: "Revenue" }, right: { title: "Rate", min: 0, max: 1 } },
};

test("a legacy multi-series chart keeps one axis and one mark", () => {
  const option = translate({ type: "bar", x: "week", y: ["revenue", "orders"] }, weekly);
  assert.ok(!Array.isArray(option.yAxis));
  assert.deepEqual(option.series.map((s) => s.type), ["bar", "bar"]);
  assert.ok(option.series.every((s) => s.yAxisIndex === undefined && s.tooltip === undefined));
  assert.equal(option.legend.type, undefined);
});

test("series overrides pick the mark, axis and legend name per column", () => {
  const option = translate(combo, weekly);
  assert.deepEqual(option.series.map((s) => s.type), ["bar", "line"]);
  assert.deepEqual(option.series.map((s) => s.yAxisIndex), [0, 1]);
  assert.deepEqual(option.series.map((s) => s.name), ["revenue", "Conversion"]);
  assert.equal(option.series[1].data[1][1], null);
});

test("each axis formats in its own units and takes its title and bounds", () => {
  const option = translate(combo, weekly);
  const [left, right] = option.yAxis;
  assert.equal(left.position, "left");
  assert.equal(right.position, "right");
  assert.equal(left.axisLabel.formatter(20000), "$20K");
  assert.equal(right.axisLabel.formatter(0.25), "25%");
  assert.equal(left.name, "Revenue");
  assert.equal(right.name, "Rate");
  assert.equal(right.min, 0);
  assert.equal(right.max, 1);
  assert.equal(right.splitLine.show, false);
});

test("the tooltip formats each series in its own unit", () => {
  const option = translate(combo, weekly);
  assert.equal(option.series[0].tooltip.valueFormatter(51000), "$51,000");
  assert.equal(option.series[1].tooltip.valueFormatter(0.125), "12.5%");
});

test("stacked bars stack per axis and leave the line alone", () => {
  const option = translate(
    {
      type: "bar",
      stacked: true,
      x: "week",
      y: ["revenue", "orders", "rate"],
      series: { rate: { type: "line", axis: "right" } },
    },
    weekly
  );
  assert.deepEqual(option.series.map((s) => s.stack), ["bar-0", "bar-0", undefined]);
});

test("group_by and horizontal bars ignore series overrides", () => {
  const grouped = translate({ ...combo, y: ["revenue"], group_by: "week" }, weekly);
  assert.ok(!Array.isArray(grouped.yAxis));
  const horizontal = translate({ ...combo, orientation: "horizontal" }, weekly);
  assert.ok(horizontal.series.every((s) => s.type === "bar" && s.yAxisIndex === undefined));
});

test("a narrow chart shortens axis titles instead of letting them collide", () => {
  const option = translate(
    { ...combo, axes: { left: { title: "Revenue in dollars" }, right: { title: "Share of all orders" } } },
    weekly,
    undefined,
    240,
    260
  );
  assert.ok(option.yAxis[0].name.endsWith("…"));
  assert.ok(option.yAxis[1].name.length <= 10, option.yAxis[1].name);
});

test("combo keys survive line, bar and area and drop elsewhere", () => {
  assert.deepEqual(pruneSpecForType(combo, "area").series, combo.series);
  assert.equal(pruneSpecForType(combo, "scatter").series, undefined);
  assert.equal(pruneSpecForType(combo, "pie").axes, undefined);
});

test("cleanCombo writes only the settings that change something", () => {
  assert.deepEqual(
    cleanCombo({
      type: "bar",
      y: ["revenue", "rate"],
      series: {
        revenue: { type: "bar", axis: "left", label: null },
        rate: { type: "line", axis: "right", label: "" },
        gone: { type: "line" },
      },
      axes: { left: { title: "", min: null, max: null, format: null }, right: { title: "Rate", min: "0" } },
    }),
    { series: { rate: { type: "line", axis: "right" } }, axes: { right: { title: "Rate", min: 0 } } }
  );
  assert.deepEqual(
    cleanCombo({ type: "bar", y: ["a", "b"], series: { b: { axis: "left" } }, axes: { right: { title: "R" } } }),
    {}
  );
  assert.deepEqual(cleanCombo({ ...combo, group_by: "week" }), {});
});

test("compact currency drops trailing zeros on every Node, not only on newer engines", () => {
  assert.equal(formatValue(20000, "currency", true), "$20K");
  assert.equal(formatValue(21500, "currency", true), "$21.5K");
  assert.equal(formatValue(0, "currency", true), "$0");
  assert.equal(formatValue(12.5, "currency"), "$12.50");
});

test("per-series formats reach the tooltip without marks or a second axis", () => {
  const option = translate(
    { type: "bar", x: "week", y: ["revenue", "rate"], format: { revenue: "currency", rate: "percent" } },
    weekly
  );
  assert.ok(!Array.isArray(option.yAxis));
  assert.equal(option.series[0].tooltip.valueFormatter(51000), "$51,000");
  assert.equal(option.series[1].tooltip.valueFormatter(0.125), "12.5%");
});

test("a chart whose only series are on the right comes back to the left axis", () => {
  assert.deepEqual(
    cleanCombo({
      type: "bar",
      y: ["rate"],
      series: { rate: { type: "line", axis: "right" } },
      axes: { right: { title: "Rate" } },
    }),
    { series: { rate: { type: "line" } } }
  );
  assert.deepEqual(
    cleanCombo({ type: "line", y: ["rate"], series: { rate: { axis: "right" } } }),
    {}
  );
});

test("a series left on the default format reads as a plain number beside a currency one", () => {
  const option = translate(
    { type: "bar", x: "week", y: ["revenue", "orders"], format: { revenue: "currency" } },
    weekly
  );
  assert.equal(option.series[0].tooltip.valueFormatter(51000), "$51,000");
  assert.equal(option.series[1].tooltip.valueFormatter(12), "12");
});
