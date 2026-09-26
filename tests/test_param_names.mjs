import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { paramNamesIn } from "../sqldash/static/js/params.js";

const corpus = JSON.parse(
  readFileSync(new URL("./param_corpus.json", import.meta.url), "utf8")
);

for (const c of corpus.cases) {
  test(`paramNamesIn matches extract_params: ${JSON.stringify(c.sql)}`, () => {
    assert.deepEqual(paramNamesIn(c.sql), c.names);
  });
}
