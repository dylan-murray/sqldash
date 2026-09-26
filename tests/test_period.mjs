import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { compareWindow, currentThenPrevious, presetRange } from "../sqldash/static/js/period.js";

test("yoy on a leap-day start keeps the prior Feb 28", () => {
  const w = compareWindow("yoy", "2024-02-29", "2024-03-31");
  assert.equal(w.start, "2023-02-28");
  assert.equal(w.end, "2023-03-31");
});

test("yoy on a non-leap start is a straight year back", () => {
  const w = compareWindow("yoy", "2024-03-01", "2024-03-31");
  assert.equal(w.start, "2023-03-01");
  assert.equal(w.end, "2023-03-31");
});

test("previous_period still uses the day count", () => {
  const w = compareWindow("previous_period", "2024-02-29", "2024-03-31");
  assert.equal(w.start, "2024-01-28");
  assert.equal(w.end, "2024-02-28");
});

const corpus = JSON.parse(
  readFileSync(new URL("./daterange_presets.json", import.meta.url), "utf8")
);

test("every preset in the shared corpus resolves the way Python does", () => {
  for (const c of corpus.cases) {
    const got = presetRange(c.token, c.today);
    const want =
      c.preset === null ? null : { preset: c.preset, start: c.start, end: c.end };
    assert.deepEqual(got, want, `${c.token} on ${c.today}`);
  }
});

test("a preset without the server's day resolves to nothing", () => {
  assert.equal(presetRange("last_30_days", null), null);
  assert.equal(presetRange("last_30_days", ""), null);
  assert.equal(presetRange("last_30_days", "not-a-date"), null);
});

const later = (ms, fn) => new Promise((resolve, reject) => setTimeout(() => fn(resolve, reject), ms));

test("the current run's error wins even when the compare run fails first", async () => {
  const current = later(20, (_, reject) => reject(new Error("entered range")));
  const previous = later(0, (_, reject) => reject(new Error("derived range")));
  await assert.rejects(currentThenPrevious(current, previous), /entered range/);
});

test("a compare failure still surfaces when the current run succeeds", async () => {
  const current = later(0, (resolve) => resolve("rows"));
  const previous = later(10, (_, reject) => reject(new Error("prior window exploded")));
  await assert.rejects(currentThenPrevious(current, previous), /prior window exploded/);
});

test("both runs resolve into the pair, with no compare run as null", async () => {
  assert.deepEqual(await currentThenPrevious(Promise.resolve(1), Promise.resolve(2)), [1, 2]);
  assert.deepEqual(await currentThenPrevious(Promise.resolve(1), null), [1, null]);
});

