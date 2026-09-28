import assert from "node:assert/strict";
import { test } from "node:test";

import { drillUrl, matchOption, rowForPoint } from "../sqldash/static/js/drill.js";

const result = {
  columns: [
    { name: "customer_id", type: "integer" },
    { name: "customer", type: "string" },
    { name: "region", type: "string" },
    { name: "signed_up", type: "timestamp" },
    { name: "revenue", type: "float" },
  ],
  rows: [
    [7, "Acme & Sons", "us", "2026-03-04T10:00:00", 120],
    [9, "Zed/Co", "eu", "2026-04-01T00:00:00", 80],
    [11, null, "us", null, 40],
  ],
};

const plan = (params, extra = {}) => ({
  target: "customer_detail",
  href: "/d/customer_detail",
  title: "Customer detail",
  params,
  errors: [],
  ...extra,
});

const context = { dashboardName: "overview", filters: { dates_start: "2026-01-01", dates_end: "" } };

test("a bar resolves to the row it was drawn from, so any column can fill a filter", () => {
  const row = rowForPoint({ type: "bar", x: "customer", y: ["revenue"] }, result, { dataIndex: 1 });
  assert.equal(row[0], 9);
});

test("a grouped series resolves inside its own group, not the whole result", () => {
  const spec = { type: "bar", x: "customer", y: ["revenue"], group_by: "region" };
  const row = rowForPoint(spec, result, { dataIndex: 1, seriesName: "us" });
  assert.equal(row[0], 11);
});

test("a slice and a big number resolve to their rows", () => {
  assert.equal(rowForPoint({ type: "pie", label: "customer" }, result, { dataIndex: 2 })[0], 11);
  assert.equal(rowForPoint({ type: "big_number", value: "revenue" }, result, {})[0], 7);
});

test("a chart type without a known data layout falls back to matching the x value", () => {
  const row = rowForPoint({ type: "funnel", x: "customer" }, result, { name: "Zed/Co" });
  assert.equal(row[0], 9);
});

test("column values are URL-encoded and current filters carry, empty ones left to the default", () => {
  const { href } = drillUrl(
    plan([
      { param: "customer", type: "select", column: "customer" },
      { param: "period_start", type: "date", current: "dates_start" },
      { param: "period_end", type: "date", current: "dates_end" },
    ]),
    result.rows[0],
    result.columns,
    context,
  );
  const url = new URL(href, "http://x");
  assert.equal(url.pathname, "/d/customer_detail");
  assert.equal(url.searchParams.get("f_customer"), "Acme & Sons");
  assert.equal(url.searchParams.get("f_period_start"), "2026-01-01");
  assert.equal(url.searchParams.has("f_period_end"), false);
  assert.equal(url.searchParams.get("from"), "overview");
  assert.ok(!href.includes(" & "), href);
});

test("dates are cut to the day and numbers checked before any link is built", () => {
  const params = [
    { param: "day", type: "date", column: "signed_up" },
    { param: "id", type: "number", column: "customer_id" },
  ];
  const { href } = drillUrl(plan(params), result.rows[1], result.columns, context);
  const url = new URL(href, "http://x");
  assert.equal(url.searchParams.get("f_day"), "2026-04-01");
  assert.equal(url.searchParams.get("f_id"), "9");
  const bad = drillUrl(
    plan([{ param: "id", type: "number", column: "customer" }]),
    result.rows[0],
    result.columns,
    context,
  );
  assert.match(bad.error, /not a number/);
});

test("a null, a missing column, or a value outside static options is refused, not dropped", () => {
  const one = (param, row = result.rows[2]) => drillUrl(plan([param]), row, result.columns, context);
  assert.match(one({ param: "c", type: "select", column: "customer" }).error, /empty/);
  assert.match(one({ param: "c", type: "text", column: "nope" }).error, /not in this tile's result/);
  const options = { param: "r", type: "select", column: "region", options: ["eu"] };
  assert.match(one(options, result.rows[0]).error, /not one of the options/);
  assert.ok(one(options, result.rows[1]).href);
});

test("a drill into the same dashboard keeps its other filters and adds no breadcrumb", () => {
  const self = plan([{ param: "region", type: "select", column: "region" }], {
    target: "overview",
    href: "/d/overview",
  });
  const { href } = drillUrl(self, result.rows[1], result.columns, {
    ...context,
    search: "?f_dates_start=2026-02-01&f_region=us&edit=1",
  });
  const url = new URL(href, "http://x");
  assert.equal(url.searchParams.get("f_region"), "eu");
  assert.equal(url.searchParams.get("f_dates_start"), "2026-02-01");
  assert.equal(url.searchParams.has("edit"), false);
  assert.equal(url.searchParams.has("from"), false);
});

test("the link is always a same-origin dashboard path", () => {
  const hostile = plan([{ param: "c", type: "text", column: "customer" }], {
    href: "/d/customer_detail",
  });
  const row = ["x", "javascript:alert(1)//", "us", null, 1];
  const { href } = drillUrl(hostile, row, [{ name: "id" }, { name: "customer" }], context);
  assert.ok(href.startsWith("/d/customer_detail?"), href);
  assert.equal(new URL(href, "http://x").searchParams.get("f_c"), "javascript:alert(1)//");
});

test("a carried filter value outside the destination's options is refused, not sent", () => {
  const carried = plan([{ param: "region", type: "select", current: "region", options: ["all", "eu"] }]);
  const at = (region) => drillUrl(carried, result.rows[0], result.columns, { ...context, filters: { region } });
  assert.match(at("us").error, /'us' is not one of the options of Customer detail's region filter/);
  assert.equal(new URL(at("eu").href, "http://x").searchParams.get("f_region"), "eu");
  assert.equal(new URL(at("").href, "http://x").searchParams.has("f_region"), false);
});

test("a number is sent the way a number input takes it, and a date must be on the calendar", () => {
  const one = (type, value) =>
    drillUrl(
      plan([{ param: "v", type, column: "customer" }]),
      [7, value, "us", null, 1],
      result.columns,
      context,
    );
  const sent = (type, value) => new URL(one(type, value).href, "http://x").searchParams.get("f_v");
  assert.equal(sent("number", "+7"), "7");
  assert.equal(sent("number", " 1. "), "1");
  assert.equal(sent("number", "-0.5e3"), "-0.5e3");
  assert.equal(sent("number", "12345678901234567890"), "12345678901234567890");
  assert.match(one("number", "7a").error, /not a number/);
  assert.equal(sent("date", "2024-02-29T00:00:00"), "2024-02-29");
  assert.match(one("date", "2026-02-31").error, /not a date/);
  assert.match(one("date", "2026-13-01").error, /not a date/);
});

test("a typed cell matches a typed option by value, and text only ever matches exactly", () => {
  const one = (options, kinds, value, type) =>
    drillUrl(
      plan([{ param: "v", type: "select", column: "v", options, option_kinds: kinds }]),
      [value],
      [{ name: "v", type }],
      context,
    );
  const sent = (...args) => new URL(one(...args).href, "http://x").searchParams.get("f_v");
  const bools = [["all", "true", "false"], ["string", "boolean", "boolean"]];
  const nums = [["1", "2"], ["number", "number"]];
  assert.equal(sent(...bools, true, "boolean"), "true");
  assert.equal(sent(...nums, 1.0, "float"), "1");
  assert.equal(sent(...nums, "1.0", "decimal"), "1");
  assert.equal(sent(["0.000001"], ["number"], 0.000001, "float"), "0.000001");
  assert.match(one(...nums, 3, "integer").error, /'3' is not one of the options/);
  assert.match(one(["all", "100"], ["string", "string"], "00100", "string").error, /'00100'/);
  assert.match(one(["100"], ["number"], "00100", "string").error, /'00100'/);
  assert.match(one(...bools, "True", "string").error, /'True'/);
  assert.equal(sent(["all", "100"], ["string", "string"], "100", "string"), "100");
  const text = plan([{ param: "active", type: "text", column: "active" }]);
  const href = drillUrl(text, [true], [{ name: "active", type: "boolean" }], context).href;
  assert.equal(new URL(href, "http://x").searchParams.get("f_active"), "true");
});

test("a carried filter matches options by the type of the filter it came from", () => {
  const carried = (kind, options, kinds) =>
    plan([{ param: "v", type: "select", current: "src", kind, options, option_kinds: kinds }]);
  const at = (p, src) => drillUrl(p, [], [], { ...context, filters: { src } });
  const num = carried("number", ["1", "2"], ["number", "number"]);
  assert.equal(new URL(at(num, "1.0").href, "http://x").searchParams.get("f_v"), "1");
  const text = carried("string", ["100"], ["string"]);
  assert.match(at(text, "00100").error, /'00100'/);
});

test("a URL value adopts the option's type but never loosens a text option", () => {
  const options = [
    { value: "all", kind: "string" },
    { value: "true", kind: "boolean" },
    { value: "2", kind: "number" },
    { value: "100", kind: "string" },
  ];
  assert.equal(matchOption(options, "True", null), "true");
  assert.equal(matchOption(options, "2.0", null), "2");
  assert.equal(matchOption(options, "00100", null), undefined);
});

test("a carried value takes its kind from the source option picked at click time", () => {
  const carried = plan([
    { param: "v", type: "select", current: "src", kind: "string", options: ["1", "2"], option_kinds: ["number", "number"] },
  ]);
  const at = (kinds) => drillUrl(carried, [], [], { ...context, filters: { src: "1.00" }, kinds });
  assert.equal(new URL(at({ src: "number" }).href, "http://x").searchParams.get("f_v"), "1");
  assert.match(at({}).error, /'1.00'/);
});
