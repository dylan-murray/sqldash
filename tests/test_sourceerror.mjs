import assert from "node:assert/strict";
import { test } from "node:test";

import { looksLikeSourceFailure } from "../sqldash/static/js/sourceerror.js";

test("a shared-relation compile error is not a source problem", () => {
  assert.equal(
    looksLikeSourceFailure(
      "000904 (42000): SQL compilation error: invalid identifier 'AQ.FOO'",
    ),
    false,
  );
  assert.equal(looksLikeSourceFailure("Catalog Error: Table with name no_such_table does not exist"), false);
  assert.equal(looksLikeSourceFailure("Binder Error: Referenced column \"x\" not found"), false);
});

test("connection and auth failures are source problems", () => {
  assert.equal(looksLikeSourceFailure("connection refused"), true);
  assert.equal(looksLikeSourceFailure("password authentication failed for user"), true);
  assert.equal(looksLikeSourceFailure("query exceeded Snowflake network_timeout"), true);
  assert.equal(looksLikeSourceFailure("No active warehouse selected in the current session"), true);
});
