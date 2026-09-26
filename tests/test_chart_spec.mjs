import assert from "node:assert/strict";
import { test } from "node:test";

import { escapeHtml, pieTooltip, pruneSpecForType } from "../sqldash/static/js/charts.js";

test("switching off bar drops orientation so lint stays clean", () => {
  const next = pruneSpecForType(
    { type: "bar", orientation: "horizontal", stacked: true, x: "region", y: ["n"] },
    "pie",
  );
  assert.equal(next.type, "pie");
  assert.equal(next.orientation, undefined);
  assert.equal(next.stacked, undefined);
  assert.equal(next.x, undefined);
});

test("switching bar to area keeps stacked and drops orientation", () => {
  const next = pruneSpecForType(
    { type: "bar", orientation: "horizontal", stacked: true, x: "region" },
    "area",
  );
  assert.equal(next.stacked, true);
  assert.equal(next.x, "region");
  assert.equal(next.orientation, undefined);
});

test("pie tooltip escapes a result label instead of rendering it", () => {
  const html = pieTooltip("number")({
    marker: '<span class="m"></span>',
    name: '<img src=x onerror="window.pwned=1"> & co',
    value: 5,
    percent: 62.5,
  });
  assert.ok(!html.includes("<img"));
  assert.ok(html.includes("&#60;img src=x onerror=&#34;window.pwned=1&#34;&#62; &#38; co"));
  assert.ok(html.startsWith('<span class="m"></span> '));
  assert.ok(html.includes("62.5%"));
});

test("escapeHtml covers every character that can open markup or an attribute", () => {
  assert.equal(escapeHtml(`<a href='x' title="y">&`), "&#60;a href=&#39;x&#39; title=&#34;y&#34;&#62;&#38;");
  assert.equal(escapeHtml(null), "null");
});
