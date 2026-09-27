import assert from "node:assert/strict";
import { test } from "node:test";

import {
  activeValues,
  crossFilterPlan,
  dimUnpicked,
  offValue,
  picked,
  toggled,
} from "../sqldash/static/js/crossfilter.js";

const filters = [
  { name: "region", type: "select", resolved_default: "all" },
  { name: "channel", type: "text", resolved_default: null },
  { name: "dates", type: "daterange", resolved_default: {} },
];

const result = {
  columns: [
    { name: "region", type: "string" },
    { name: "channel", type: "string" },
    { name: "revenue", type: "float" },
  ],
  rows: [
    ["us", "web", 10],
    ["eu", "web", 20],
    ["us", "store", 30],
  ],
};

const plan = crossFilterPlan({ cross_filter: { region: "region", channel: "channel" } }, filters);
const offs = { region: "all", channel: "" };

test("the plan keeps scalar filters and names the ones a click cannot set", () => {
  assert.deepEqual(
    plan.entries.map((e) => e.name),
    ["region", "channel"],
  );
  const bad = crossFilterPlan({ cross_filter: { nope: "region", dates: "region" } }, filters);
  assert.equal(bad.entries.length, 0);
  assert.equal(bad.errors.length, 2);
  assert.equal(crossFilterPlan({ cross_filter: false }, filters), null);
});

test("a select turns off to all, anything else to its default", () => {
  assert.equal(offValue(filters[0], ["all", "us"]), "all");
  assert.equal(offValue({ type: "select", resolved_default: "us" }, ["us", "eu"]), "us");
  assert.equal(offValue(filters[1]), "");
});

test("a click sets every mapped filter, and the same click again turns them all off", () => {
  const { values } = picked(plan, result.rows[2], result.columns);
  assert.deepEqual(values, { region: "us", channel: "store" });
  const on = toggled(plan, values, { region: "all", channel: "" }, offs);
  assert.deepEqual(on, { region: "us", channel: "store" });
  assert.deepEqual(toggled(plan, values, on, offs), offs);
  const other = toggled(plan, values, { region: "us", channel: "web" }, offs);
  assert.deepEqual(other, { region: "us", channel: "store" });
});

test("the selection is only active while every mapped filter is on", () => {
  assert.equal(activeValues(plan, { region: "us", channel: "" }, offs), null);
  assert.deepEqual(activeValues(plan, { region: "us", channel: "web" }, offs), {
    region: "us",
    channel: "web",
  });
});

test("a missing column or a null is refused", () => {
  assert.match(picked(plan, ["us", null, 1], result.columns).error, /empty/);
  const other = crossFilterPlan({ cross_filter: { region: "area" } }, filters);
  assert.match(picked(other, result.rows[0], result.columns).error, /not in this tile's result/);
});

test("marks outside the selection are dimmed and the picked ones kept", () => {
  const single = crossFilterPlan({ cross_filter: { region: "region" } }, filters);
  const option = {
    series: [{ name: "revenue", data: result.rows.map((r) => [r[0], r[2]]) }],
  };
  const spec = { type: "bar", x: "region", y: ["revenue"] };
  const [series] = dimUnpicked(option, spec, result, single, { region: "us" });
  assert.deepEqual(
    series.data.map((d) => d.itemStyle?.opacity ?? 1),
    [1, 0.28, 1],
  );
  assert.deepEqual(series.data[0].value, ["us", 10]);
});
