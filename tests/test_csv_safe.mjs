import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { csvText, spreadsheetSafe } from "../sqldash/static/js/csv-safe.js";

const corpus = JSON.parse(
  readFileSync(new URL("./spreadsheet_safe.json", import.meta.url), "utf8")
);

test("every cell in the shared corpus is quoted the way the server export quotes it", () => {
  for (const c of corpus.cases) {
    assert.deepEqual(spreadsheetSafe(c.value), c.want, JSON.stringify(c.value));
  }
});

test("the sorted download quotes formula headers and cells and keeps numbers", () => {
  const text = csvText(["=head", "n"], [['=HYPERLINK("http://x","y")', -3.5], ["-2.25", null]]);
  assert.equal(
    text,
    '"\'=head","n"\r\n"\'=HYPERLINK(""http://x"",""y"")","-3.5"\r\n"-2.25",""'
  );
});

test("the sorted download writes a list or struct cell as JSON", () => {
  const text = csvText(["l", "s"], [[[1, null], { k: "v" }]]);
  assert.equal(text, '"l","s"\r\n"[1,null]","{""k"":""v""}"');
});
