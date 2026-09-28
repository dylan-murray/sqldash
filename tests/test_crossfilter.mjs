import assert from "node:assert/strict";
import { test } from "node:test";

import {
  activeValues,
  crossFilterPlan,
  dimUnpicked,
  offValue,
  picked,
  rowIsPicked,
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

test("the selection is active while any mapped filter is on, and an off one matches any row", () => {
  assert.equal(activeValues(plan, { region: "all", channel: "" }, offs), null);
  assert.deepEqual(activeValues(plan, { region: "us", channel: "web" }, offs), {
    region: "us",
    channel: "web",
  });
  const partial = activeValues(plan, { region: "us", channel: "" }, offs);
  assert.deepEqual(partial, { region: "us" });
  assert.equal(rowIsPicked(plan, result.rows[2], result.columns, partial), true);
  assert.equal(rowIsPicked(plan, result.rows[1], result.columns, partial), false);
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

test("a line selection fades the stroke and area and draws only the picked point until hovered", () => {
  const days = Array.from({ length: 50 }, (_, i) => [`2026-01-${String(i + 1).padStart(2, "0")}`, i]);
  const trend = { columns: [{ name: "day", type: "date" }, { name: "n", type: "integer" }], rows: days };
  const single = crossFilterPlan({ cross_filter: { day: "day" } }, [{ name: "day", type: "date" }]);
  const option = {
    series: [
      {
        type: "line",
        name: "N",
        showSymbol: false,
        lineStyle: { opacity: 0.55 },
        areaStyle: { opacity: 1 },
        data: days,
      },
    ],
  };
  const spec = { type: "area", x: "day", y: ["n"] };
  const [series] = dimUnpicked(option, spec, trend, single, { day: "2026-01-10" });
  assert.equal(series.showSymbol, true);
  assert.equal(series.showAllSymbol, true);
  assert.ok(Math.abs(series.lineStyle.opacity - 0.55 * 0.28) < 1e-9);
  assert.equal(series.areaStyle.opacity, 0.28);
  assert.equal(series.data[9].itemStyle, undefined);
  assert.equal(series.data[0].itemStyle.opacity, 0);
  assert.equal(series.data[0].emphasis.itemStyle.opacity, 1);
  const bars = dimUnpicked({ series: [{ ...option.series[0], type: "bar" }] }, spec, trend, single, {
    day: "2026-01-10",
  });
  assert.equal(bars[0].lineStyle, undefined);
  assert.equal(bars[0].data[0].itemStyle.opacity, 0.28);
});

test("a filter sitting at a real default still has to match, only an unset or all one is open", () => {
  const withDefault = [
    { name: "region", type: "select", resolved_default: "all" },
    { name: "channel", type: "text", resolved_default: "web" },
  ];
  const pair = crossFilterPlan({ cross_filter: { region: "region", channel: "channel" } }, withDefault);
  const defaults = { region: "all", channel: "web" };
  assert.equal(activeValues(pair, { region: "all", channel: "web" }, defaults), null);
  const active = activeValues(pair, { region: "eu", channel: "web" }, defaults);
  assert.deepEqual(active, { region: "eu", channel: "web" });
  assert.equal(rowIsPicked(pair, ["eu", "web", 1], result.columns, active), true);
  assert.equal(rowIsPicked(pair, ["eu", "store", 1], result.columns, active), false);
});

test("a compare chart dims against the row a point came from, not where it is drawn", () => {
  const byDay = crossFilterPlan({ cross_filter: { day: "day" } }, [{ name: "day", type: "date" }]);
  const merged = {
    columns: [
      { name: "day", type: "date" },
      { name: "n", type: "integer" },
      { name: "__period", type: "string" },
    ],
    rows: [
      ["2026-01-10", 5, "current"],
      ["2026-01-10", 3, "previous"],
    ],
    unshifted: [
      ["2026-01-10", 5, "current"],
      ["2025-01-10", 3, "previous"],
    ],
  };
  const option = {
    series: [
      { type: "bar", name: "current", data: [["2026-01-10", 5]] },
      { type: "bar", name: "previous", data: [["2026-01-10", 3]] },
    ],
  };
  const spec = { type: "bar", x: "day", y: ["n"], group_by: "__period" };
  const opacity = (active) =>
    dimUnpicked(option, spec, merged, byDay, active).map((s) => s.data[0].itemStyle?.opacity ?? 1);
  assert.deepEqual(opacity({ day: "2025-01-10" }), [0.28, 1]);
  assert.deepEqual(opacity({ day: "2026-01-10" }), [1, 0.28]);
});

test("dimming a 10k-row grouped chart is linear, not a scan of the result per mark", () => {
  const regions = Array.from({ length: 10 }, (_, i) => `r${i}`);
  const rows = Array.from({ length: 10000 }, (_, i) => [regions[i % 10], `d${Math.floor(i / 10)}`, i]);
  const big = {
    columns: [
      { name: "region", type: "string" },
      { name: "day", type: "string" },
      { name: "n", type: "integer" },
    ],
    rows,
  };
  const option = {
    series: regions.map((name) => ({
      type: "bar",
      name,
      data: rows.filter((r) => r[0] === name).map((r) => [r[1], r[2]]),
    })),
  };
  const spec = { type: "bar", x: "day", y: ["n"], group_by: "region" };
  const single = crossFilterPlan({ cross_filter: { region: "region" } }, filters);
  const times = [];
  let series;
  for (let run = 0; run < 5; run++) {
    const fresh = { ...big, rows: [...rows] };
    const started = performance.now();
    series = dimUnpicked(option, spec, fresh, single, { region: "r3" });
    times.push(performance.now() - started);
  }
  assert.equal(series[3].data[5].itemStyle, undefined);
  assert.equal(series[4].data[5].itemStyle.opacity, 0.28);
  const took = Math.min(...times);
  assert.ok(took < 100, `dimming took ${Math.round(took)}ms at best`);
});

test("a null under a filter that is not narrowing anything does not unpick the row", () => {
  const active = activeValues(plan, { region: "eu", channel: "" }, offs);
  assert.equal(rowIsPicked(plan, ["eu", null, 1], result.columns, active), true);
  assert.equal(rowIsPicked(plan, ["us", null, 1], result.columns, active), false);
  assert.equal(rowIsPicked(plan, [null, "web", 1], result.columns, active), false);
});

test("a boolean click sets the option its filter bar renders", () => {
  const flags = crossFilterPlan({ cross_filter: { active: "active" } }, [
    { name: "active", type: "select", options: [true, false] },
  ]);
  const { values } = picked(flags, [true], [{ name: "active", type: "boolean" }]);
  assert.deepEqual(values, { active: "True" });
});
