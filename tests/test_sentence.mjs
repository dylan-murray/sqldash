import assert from "node:assert/strict";
import { test } from "node:test";

import { withoutPeriod } from "../sqldash/static/js/sentence.js";

test("a message ending in a period gets one period when a sentence follows", () => {
  const messages = [
    "Object does not exist.",
    "Object does not exist",
    "Object does not exist. ",
    "Object does not exist...",
  ];
  for (const message of messages) {
    assert.equal(`${withoutPeriod(message)}. Next.`, "Object does not exist. Next.", message);
  }
});

test("only trailing periods and spaces go, not the rest of the message", () => {
  assert.equal(withoutPeriod("v1.2 failed?"), "v1.2 failed?");
  assert.equal(withoutPeriod("see db.schema.table."), "see db.schema.table");
  assert.equal(withoutPeriod(undefined), "");
  assert.equal(withoutPeriod(42), "42");
});
