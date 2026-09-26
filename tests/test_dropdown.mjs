import assert from "node:assert/strict";
import { test } from "node:test";

import { menuBox, menuShift } from "../sqldash/static/js/dropdown.js";

test("a centered menu with room does not move", () => {
  const { up, dx } = menuShift({
    wrap: { left: 400, width: 80, top: 10, bottom: 40 },
    menuW: 200,
    menuH: 100,
    vw: 800,
    vh: 600,
  });
  assert.equal(up, false);
  assert.equal(dx, 0);
});

test("a menu near the left edge shifts right", () => {
  const { dx } = menuShift({
    wrap: { left: 8, width: 80, top: 10, bottom: 40 },
    menuW: 360,
    menuH: 100,
    vw: 800,
    vh: 600,
  });
  assert.equal(dx, 140);
});

test("a menu near the right edge shifts left", () => {
  const { dx } = menuShift({
    wrap: { left: 720, width: 80, top: 10, bottom: 40 },
    menuW: 360,
    menuH: 100,
    vw: 800,
    vh: 600,
  });
  assert.equal(dx, -148);
});

test("a menu that would hang off the bottom flips up", () => {
  const { up } = menuShift({
    wrap: { left: 400, width: 80, top: 520, bottom: 550 },
    menuW: 200,
    menuH: 200,
    vw: 800,
    vh: 600,
  });
  assert.equal(up, true);
});

test("a full-width menu against the left edge still lands on the pad", () => {
  const { dx } = menuShift({
    wrap: { left: 8, width: 80, top: 10, bottom: 40 },
    menuW: 784,
    menuH: 100,
    vw: 800,
    vh: 600,
  });
  assert.equal(8 + 40 - 392 + dx, 8);
});

test("an open menu is placed from the wrap's viewport rect, not percent of the wrap", () => {
  const box = menuBox({
    wrap: { left: 400, width: 80, top: 10, bottom: 40 },
    menuW: 200,
    menuH: 100,
    vw: 800,
    vh: 600,
  });
  assert.equal(box.up, false);
  assert.equal(box.left, 440);
  assert.equal(box.top, 46);
  assert.equal(box.bottom, "auto");
  assert.equal(box.translate, "-50% 0");
});

test("a menu that would hang off the bottom is pinned above the wrap", () => {
  const box = menuBox({
    wrap: { left: 400, width: 80, top: 520, bottom: 550 },
    menuW: 200,
    menuH: 200,
    vw: 800,
    vh: 600,
  });
  assert.equal(box.up, true);
  assert.equal(box.top, "auto");
  assert.equal(box.bottom, 86);
});
