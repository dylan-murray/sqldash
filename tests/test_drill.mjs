import assert from "node:assert/strict";
import { test } from "node:test";

import { drillUrl, rowForPoint } from "../sqldash/static/js/drill.js";

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
