import assert from "node:assert/strict";
import { test } from "node:test";

import {
  chartNumber,
  compareNumbers,
  formatValue,
  setFormatConfig,
} from "../sqldash/static/js/charts.js";

setFormatConfig({ locale: "en-US" });

test("an integer past 2^53 sent as text keeps every digit", () => {
  assert.equal(formatValue("12345678901234567890", "number"), "12,345,678,901,234,567,890");
  assert.equal(formatValue("9007199254740993", "number"), "9,007,199,254,740,993");
  assert.equal(
    formatValue("-10000000000000000000000000000000000001", "number"),
    "-10,000,000,000,000,000,000,000,000,000,000,000,001"
  );
});

test("a high-scale decimal keeps its digits and trims only trailing zeros", () => {
  assert.equal(
    formatValue("12345678901234567890.0123456789", "number"),
    "12,345,678,901,234,567,890.0123456789"
  );
  assert.equal(formatValue("221474.9300000000", "number"), "221,474.93");
  assert.equal(formatValue("1.5000000000", "number"), "1.5");
});

test("a tiny decimal in exponent form is not rounded to -0", () => {
  assert.equal(formatValue("-1E-10", "number"), "-0.0000000001");
  assert.equal(formatValue("1.5E+3", "number"), "1,500");
  assert.equal(formatValue("-0.0000000000", "number"), "0");
});

test("numbers the browser already holds exactly format as before", () => {
  assert.equal(formatValue(42, "number"), "42");
  assert.equal(formatValue(3.14159, "number"), "3.14");
  assert.equal(formatValue(1234.5, "currency"), "$1,235");
  assert.equal(formatValue(0.1234, "percent"), "12.3%");
});

test("named formats round the exact text, not a double", () => {
  assert.equal(formatValue("9007199254740993", "USD"), "$9007T");
  assert.equal(formatValue("123.456", "currency"), "$123.46");
  assert.equal(formatValue("0.12345", "percent"), "12.3%");
});

test("sorting orders exact text without collapsing neighbours", () => {
  const values = ["9007199254740993", 42, "-1E-10", "9007199254740992", "12345678901234567890", 0];
  const sorted = [...values].sort(compareNumbers);
  assert.deepEqual(sorted, [
    "-1E-10",
    0,
    42,
    "9007199254740992",
    "9007199254740993",
    "12345678901234567890",
  ]);
  assert.ok(compareNumbers("-2", "-10") > 0);
  assert.equal(compareNumbers("1.50", "1.5"), 0);
});

test("charts plot a number, not the text", () => {
  assert.equal(chartNumber("221474.9300000000"), 221474.93);
  assert.equal(chartNumber(7), 7);
  assert.equal(chartNumber("north"), "north");
  assert.equal(chartNumber(null), null);
});

test("a float the warehouse returned as NaN or Infinity shows as that text", () => {
  assert.equal(formatValue("NaN", "number"), "NaN");
  assert.equal(formatValue("Infinity", "number"), "Infinity");
  assert.equal(formatValue("-Infinity", "currency"), "-Infinity");
});

test("NaN and Infinity leave a gap in a chart instead of plotting text", () => {
  assert.equal(chartNumber("NaN"), null);
  assert.equal(chartNumber("Infinity"), null);
  assert.equal(chartNumber("-Infinity"), null);
  assert.equal(chartNumber("1.5"), 1.5);
});

test("sorting puts -Infinity first, then numbers, Infinity, and NaN last", () => {
  const values = ["NaN", 3, "Infinity", "-1E-10", "-Infinity", "12345678901234567890"];
  assert.deepEqual([...values].sort(compareNumbers), [
    "-Infinity",
    "-1E-10",
    3,
    "12345678901234567890",
    "Infinity",
    "NaN",
  ]);
});
