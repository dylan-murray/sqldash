import assert from "node:assert/strict";
import { test } from "node:test";

import { inspectText } from "../sqldash/static/js/charts.js";

test("objects are pretty-printed for inspect and one-lined in the cell", () => {
  const value = { error: "timeout", sql: "SELECT 1" };
  const shown = inspectText(value);
  assert.equal(shown.display, JSON.stringify(value));
  assert.equal(shown.inspect, JSON.stringify(value, null, 2));
  assert.equal(shown.pretty, true);
});

test("a JSON string is pretty-printed on inspect", () => {
  const shown = inspectText('{"a":1}');
  assert.equal(shown.display, '{"a":1}');
  assert.equal(shown.inspect, '{\n  "a": 1\n}');
  assert.equal(shown.pretty, true);
});

test("a plain string is unchanged", () => {
  const shown = inspectText("hello");
  assert.equal(shown.display, "hello");
  assert.equal(shown.inspect, "hello");
  assert.equal(shown.pretty, false);
});

test("null is labeled, not stringified", () => {
  const shown = inspectText(null);
  assert.equal(shown.display, "null");
  assert.equal(shown.inspect, "null");
});

test("a Date is not stringified to empty braces", () => {
  const shown = inspectText(new Date("2026-01-15T00:00:00Z"));
  assert.notEqual(shown.display, "{}");
  assert.match(shown.display, /2026/);
  assert.equal(shown.pretty, false);
});
