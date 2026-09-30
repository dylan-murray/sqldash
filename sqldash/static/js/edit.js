import {
  connectEvents,
  replaceDashboard,
  initFilters,
  applyTileHues,
  dashboard,
  markDrillTiles,
  mutatingHeaders,
  dashboardName,
  disposeTile,
  renameTile,
  getEtag,
  runTiles,
  setEditing,
  setEtag,
  toast,
} from "/static/js/runner.js";
import { dashboardPath } from "/static/js/paths.js";

const grid = GridStack.init({
  column: dashboard.layout.columns,
  cellHeight: dashboard.layout.row_height,
  margin: 7,
  staticGrid: true,
  float: true,
  resizable: { handles: "se" },
  draggable: { cancel: "input,textarea,button,select,a,.cell-inspect,.cell-pop,.dd-menu" },
});

let editing = new URLSearchParams(location.search).get("edit") === "1";

/* ---------- save plumbing ---------- */

function flashSaved() {
  const el = document.getElementById("save-indicator");
  if (!el) return;
  el.classList.add("show");
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.remove("show"), 1600);
}

// Mutations share one If-Match etag. Two overlapping saves (resizestop + the
// trailing change, or delete's positions PATCH + the next click) both read
// the same etag and one 409s as "File changed on disk". Queue so each call
// sees the etag the previous response just wrote.
let saveQueue = Promise.resolve();

function save(method, path, body) {
  const queued = saveQueue.then(async () => {
    const res = await fetch(path, {
      method,
      headers: mutatingHeaders({ "Content-Type": "application/json", "If-Match": getEtag() }),
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (res.status === 409) {
      toast("File changed on disk — reload to keep editing", "error");
      return null;
    }
    if (!res.ok) throw new Error((await res.json()).detail ?? `HTTP ${res.status}`);
    const payload = await res.json();
    if (payload?.etag) setEtag(payload.etag);
    flashSaved();
    return payload;
  });
  saveQueue = queued.then(
    () => undefined,
    () => undefined,
  );
  return queued;
}

/* ---------- table tiles shrink to content (authored size = max) ---------- */

function fitTableTile(tileEl) {
  const item = tileEl.closest(".grid-stack-item");
  if (!item) return;
  if (!item.dataset.authorH) item.dataset.authorH = item.getAttribute("gs-h");
  const head = tileEl.querySelector(".tile-head");
  const table = tileEl.querySelector(".table-wrap table");
  if (!table) return;
  const note = tileEl.querySelector(".table-note");
  const contentPx =
    (head?.offsetHeight ?? 0) + table.offsetHeight + (note?.offsetHeight ?? 0) + 34;
  const cell = grid.getCellHeight();
  const needed = Math.max(2, Math.ceil(contentPx / (cell + 7)));
  const target = Math.min(Number(item.dataset.authorH), needed);
  if (Number(item.getAttribute("gs-h")) !== target) {
    grid.update(item, { h: target });
  }
}

document.addEventListener("sqldash:table-rendered", (e) => {
  if (editing) return;
  fitTableTile(e.target);
});

function restoreAuthoredSizes() {
  document.querySelectorAll(".grid-stack-item[data-author-h]").forEach((item) => {
    if (Number(item.getAttribute("gs-h")) !== Number(item.dataset.authorH)) {
      grid.update(item, { h: Number(item.dataset.authorH) });
    }
  });
}

/* ---------- edit mode ---------- */

function applyMode() {
  setEditing(editing);
  grid.setStatic(!editing);
  if (editing) restoreAuthoredSizes();
  document.body.classList.toggle("editing", editing);
  document.querySelectorAll(".edit-only").forEach((el) => (el.hidden = !editing));
  document.querySelectorAll(".view-only").forEach((el) => (el.hidden = editing));
  if (!editing && filterPanel) filterPanel.hidden = true;
  applyMetaEditable();
  const url = new URL(location);
  if (editing) url.searchParams.set("edit", "1");
  else url.searchParams.delete("edit");
  history.replaceState(null, "", url);
}

document.getElementById("edit-btn").addEventListener("click", () => {
  editing = true;
  applyMode();
});
document.getElementById("done-btn").addEventListener("click", () => {
  editing = false;
  applyMode();
});

// `grid.compact()` moves nodes, which emits `change` itself. Swallowing that
// re-entry keeps a second PATCH — carrying the etag the first just superseded —
// from reaching the server as a 409.
let compacting = false;

// Gravity, one tile at a time, within its own column span. `grid.compact()`
// re-packs the whole grid row-major from the top-left, so deleting a tile made
// its neighbour jump into the vacated column and the tile below slide across —
// the author removed one tile and the rest swapped places. A tile keeps its
// column here and only its row moves.
//
// Only tiles that overlap [x, x+w) and sit at or below y are packed, and they
// cannot rise above y. A whole-grid first-fit closed authored gaps above the
// resize (a heading, two empty rows, then a shrink of the tile under them
// rewrote the file with the gap gone). Growing still pushes tiles beneath
// the hole down instead of overlapping them.
//
// A tile that moves up leaves a hole in every column it spanned. One pass
// only packs tiles that overlapped the original hole, so a full-width tile
// rising under a half-width shrink left the other column empty. Cascade into
// each vacated rectangle; only an upward move opens one.
function settle(x, y, w) {
  const nodes = grid.engine.nodes.map((n) => ({
    el: n.el, x: n.x, y: n.y, w: n.w, h: n.h,
  }));
  const queue = [{ x, y, w }];
  let moved = false;
  let guard = nodes.length * nodes.length + 1;
  while (queue.length && guard--) {
    const hole = queue.shift();
    const frozen = [];
    const moving = [];
    for (const node of nodes.slice().sort((a, b) => a.y - b.y || a.x - b.x)) {
      const overlaps = node.x < hole.x + hole.w && hole.x < node.x + node.w;
      if (!overlaps || node.y < hole.y) frozen.push(node);
      else moving.push(node);
    }
    const placed = frozen.map((n) => ({ x: n.x, y: n.y, w: n.w, h: n.h }));
    for (const node of moving) {
      let ny = hole.y;
      while (
        placed.some(
          (p) =>
            ny < p.y + p.h && p.y < ny + node.h && node.x < p.x + p.w && p.x < node.x + node.w
        )
      ) {
        ny += 1;
      }
      if (ny < node.y) {
        queue.push({ x: node.x, y: node.y, w: node.w });
        node.y = ny;
        moved = true;
      } else if (ny > node.y) {
        node.y = ny;
        moved = true;
      }
      placed.push({ x: node.x, y: ny, w: node.w, h: node.h });
    }
  }
  if (!moved) return false;
  compacting = true;
  try {
    grid.batchUpdate();
    for (const n of nodes) {
      if (n.el.gridstackNode && n.el.gridstackNode.y !== n.y) {
        grid.update(n.el, { y: n.y });
      }
    }
    grid.batchUpdate(false);
  } finally {
    compacting = false;
  }
  return true;
}

async function savePositions() {
  const positions = {};
  for (const node of grid.engine.nodes) {
    positions[node.id] = { x: node.x, y: node.y, w: node.w, h: node.h };
  }
  try {
    const payload = await save(
      "PATCH", `/api/dashboards/${dashboardPath(dashboardName)}/positions`, { positions }
    );
    if (!payload) return;
    for (const [id, pos] of Object.entries(positions)) {
      const tile = dashboard.tiles.find((w) => w.id === id);
      if (tile) tile.position = pos;
    }
  } catch (err) {
    toast(`Layout save failed: ${err.message}`, "error");
  }
}

// Only a resize or a delete can open a hole, and each settles at its own site
// rather than setting a flag for whatever `change` happens to arrive next: a
// delete emits no `change` at all, so such a flag survived until the following
// drag and re-packed the tile the author had just dropped.
//
// `resizestop` runs before the resize's own `change`. settle() only swallows
// that change when it actually moves a tile (batchUpdate). Saving here only
// in that case: a no-hole resize (grow down, shrink the bottom tile) returns
// false, leaves the dirty node, and `change` is the one save. Saving on every
// resizestop doubled the PATCH with the same etag and 409'd the loser.
//
// Snapshot the node on resizestart: a no-op handle grab must not settle, and
// the hole is the union of the old and new rectangles so a grow still pushes
// what it landed on.
let resizeBefore = null;
grid.on("resizestart", (_event, el) => {
  const node = el?.gridstackNode;
  resizeBefore = node ? { x: node.x, y: node.y, w: node.w, h: node.h } : null;
});
grid.on("resizestop", async (_event, el) => {
  if (!editing) return;
  const node = el?.gridstackNode;
  const before = resizeBefore;
  resizeBefore = null;
  if (!node || !before) return;
  if (before.x === node.x && before.y === node.y && before.w === node.w && before.h === node.h) {
    return;
  }
  const x = Math.min(before.x, node.x);
  const y = Math.min(before.y, node.y);
  const w = Math.max(before.x + before.w, node.x + node.w) - x;
  if (settle(x, y, w)) await savePositions();
});

grid.on("change", async (event, items) => {
  if (!editing || !items?.length || compacting) return;
  await savePositions();
});

/* ---------- editable title / description ---------- */

const titleEl = document.getElementById("dash-title");
const descEl = document.getElementById("dash-desc");

function applyMetaEditable() {
  for (const el of [titleEl, descEl]) {
    if (!el) continue;
    el.contentEditable = editing ? "plaintext-only" : "false";
    el.classList.toggle("editable", editing);
  }
  if (descEl && editing && !descEl.textContent.trim()) {
    descEl.dataset.empty = "1";
  }
}

async function saveMeta() {
  const title = titleEl?.textContent.trim();
  const description = descEl ? descEl.textContent.trim() : null;
  if (!title) {
    toast("Title cannot be empty", "error");
    titleEl.textContent = dashboard.title;
    return;
  }
  if (title === dashboard.title && (description ?? "") === (dashboard.description ?? "")) return;
  try {
    const payload = await save("PATCH", `/api/dashboards/${dashboardPath(dashboardName)}/meta`, {
      title,
      description: description ?? "",
    });
    if (!payload) return;
    dashboard.title = title;
    dashboard.description = description;
    const topbarTitle = document.getElementById("topbar-title");
    if (topbarTitle) topbarTitle.textContent = title;
    const infoName = document.querySelector(".dash-info-name");
    if (infoName) infoName.textContent = title;
  } catch (err) {
    toast(`Save failed: ${err.message}`, "error");
  }
}

for (const el of [titleEl, descEl]) {
  if (!el) continue;
  el.addEventListener("blur", saveMeta);
  el.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      el.blur();
    }
  });
  el.addEventListener("input", () => delete el.dataset.empty);
}

/* ---------- filter editor ---------- */

const filterPanel = document.getElementById("filter-editor");

function currentFilterDefs() {
  return dashboard.filters.map((f) => {
    const def = { name: f.name, type: f.type };
    if (f.label) def.label = f.label;
    if (f.default !== null && f.default !== undefined) def.default = f.default;
    if (f.options?.length) def.options = f.options;
    if (f.type === "daterange" && f.bind) def.bind = f.bind;
    return def;
  });
}

async function putFilters(filters) {
  try {
    const payload = await save("PUT", `/api/dashboards/${dashboardPath(dashboardName)}/filters`, { filters });
    if (!payload) return;
    const url = new URL(location);
    url.searchParams.set("edit", "1");
    location.href = url;
  } catch (err) {
    toast(`Filter save failed: ${err.message}`, "error");
  }
}

document.getElementById("add-filter-btn")?.addEventListener("click", () => {
  filterPanel.hidden = !filterPanel.hidden;
});

document.getElementById("fe-type")?.addEventListener("change", (e) => {
  document.getElementById("fe-options-field").hidden = e.target.value !== "select";
});

document.getElementById("fe-save")?.addEventListener("click", () => {
  const name = document.getElementById("fe-name").value.trim();
  if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)) {
    toast("Filter name must be a valid identifier (letters, digits, _)", "error");
    return;
  }
  const type = document.getElementById("fe-type").value;
  const filter = { name, type };
  const label = document.getElementById("fe-label").value.trim();
  if (label) filter.label = label;
  const dflt = document.getElementById("fe-default").value.trim();
  if (dflt) filter.default = dflt;
  if (type === "select") {
    const options = document.getElementById("fe-options").value
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    if (options.length) filter.options = options;
  }
  const filters = currentFilterDefs().filter((f) => f.name !== name);
  filters.push(filter);
  putFilters(filters);
});

document.querySelectorAll("[data-remove-filter]").forEach((btn) => {
  btn.addEventListener("click", () => {
    const name = btn.dataset.removeFilter;
    putFilters(currentFilterDefs().filter((f) => f.name !== name));
  });
});

/* ---------- tile actions ---------- */

// Tile ids are derived from position and content, so deleting one renumbers
// `tile_N` and promotes a deduped `q_2` to `q`. Everything we hold keyed by id
// — the tiles array, the DOM, the chart and result maps — is stale the moment
// the delete returns, and a stale id still resolves server-side, so the next
// delete quietly removes the wrong tile. The delete response carries the ids
// that are now true; adopt them before anything else reads one.
function resyncTileIds(serverTiles) {
  if (!Array.isArray(serverTiles) || serverTiles.length !== dashboard.tiles.length) {
    // Never observed — the server derives from the same file the client just
    // filtered. But a silent return here restores the exact bug this function
    // exists to prevent, so it must not be the quiet path.
    console.error("tile resync skipped: server sent", serverTiles, "for", dashboard.tiles.length);
    toast("Reload the page — tile ids are out of sync", "error");
    return;
  }
  // Resolve every element up front: the new ids overlap the old ones, so
  // reassigning as we go would let one tile's new id collide with another's
  // current one and select the wrong element.
  const els = dashboard.tiles.map((w) =>
    document.querySelector(`.tile[data-tile-id="${CSS.escape(w.id)}"]`)
  );
  // Query names move only when the server says they moved. Inferring it from
  // `tile.query === tile.id` cannot tell a hoisted inline-sql name from an
  // authored one that happens to match, and renaming an authored query makes
  // the tile ask for a name the file never had — answered by another tile's
  // query, silently, or by a 404.
  const renamed = [];
  dashboard.tiles.forEach((tile, i) => {
    const next = serverTiles[i];
    if (tile.query && next.query && tile.query !== next.query) {
      renamed.push([tile.query, next.query, tile]);
    }
    if (tile.id === next.id) return;
    renameTile(tile.id, next.id);
    tile.id = next.id;
    const el = els[i];
    if (!el) return;
    el.dataset.tileId = next.id;
    // GridStack keys its nodes by gs-id, and the positions PATCH sends
    // grid.engine.nodes ids. Leaving those stale means the next drag saves
    // positions for ids the server no longer has — a 422 the user only sees
    // as "Layout save failed", after the tile has already moved on screen.
    const item = el.closest(".grid-stack-item");
    if (!item) return;
    item.setAttribute("gs-id", next.id);
    if (item.gridstackNode) item.gridstackNode.id = next.id;
  });
  // Lift every SQL out before writing any back: the new names overlap the old
  // ones, so renaming in place would let one tile's new key overwrite another's
  // still-live entry.
  const carried = renamed.map(([from]) => dashboard.queries[from]);
  renamed.forEach(([from]) => delete dashboard.queries[from]);
  renamed.forEach(([, to, tile], i) => {
    if (carried[i] !== undefined) dashboard.queries[to] = carried[i];
    tile.query = to;
  });
}

document.getElementById("grid").addEventListener("click", async (event) => {
  const btn = event.target.closest(".wa-btn");
  if (!btn || !btn.dataset.action || btn.dataset.action === "csv" || btn.dataset.action === "edit") return;
  const tileEl = btn.closest(".tile");
  const tileId = tileEl.dataset.tileId;
  const tile = dashboard.tiles.find((w) => w.id === tileId);
  if (!tile) return;

  if (btn.dataset.action === "delete") {
    if (!confirm(`Delete tile "${tile.title || tile.id}"?`)) return;
    try {
      const payload = await save(
        "DELETE", `/api/dashboards/${dashboardPath(dashboardName)}/tiles/${encodeURIComponent(tileId)}`
      );
      if (!payload) return;
      disposeTile(tileId);
      const item = tileEl.closest(".grid-stack-item");
      const hole = item?.gridstackNode;
      grid.removeWidget(item);
      // A removal emits `removed`, never `change`, so nothing else settles or
      // saves it — the vacated rows stayed empty and were never written. Only
      // write when something actually moved: the DELETE already recorded the
      // removal, and an extra PATCH queues ahead of whatever the author does
      // next, since writes run one at a time.
      const settled = hole ? settle(hole.x, hole.y, hole.w) : false;
      dashboard.tiles = dashboard.tiles.filter((w) => w.id !== tileId);
      resyncTileIds(payload.tiles);
      if (settled) await savePositions();
      if (tile.query && !dashboard.tiles.some((w) => w.query === tile.query)) {
        delete dashboard.queries[tile.query];
      }
    } catch (err) {
      toast(`Delete failed: ${err.message}`, "error");
    }
  }
});

/* ---------- external changes via SSE ---------- */

let refreshPending = null;
export async function refreshDashboard() {
  if (refreshPending) return refreshPending;
  refreshPending = (async () => {
    if (editing) throw new Error("Finish dashboard editing before refreshing Studio.");
    const response = await fetch(location.href, { cache: "no-store" });
    if (!response.ok) throw new Error("Dashboard could not refresh. Check the agent's changes.");
    const doc = new DOMParser().parseFromString(await response.text(), "text/html");
    const next = JSON.parse(doc.getElementById("dashboard-data").textContent);
    const items = [...doc.querySelectorAll(".grid-stack > .grid-stack-item")];
    replaceDashboard(next);
    grid.removeAll();
    grid.column(dashboard.layout.columns);
    grid.cellHeight(dashboard.layout.row_height);
    grid.batchUpdate();
    for (const item of items) {
      document.querySelector(".grid-stack").append(item);
      grid.makeWidget(item);
    }
    grid.batchUpdate(false);
    for (const selector of ["#dash-title", "#dash-desc", "#topbar-title", ".dash-info-name"]) {
      const target = document.querySelector(selector);
      const source = doc.querySelector(selector);
      if (target && source) target.textContent = source.textContent;
    }
    document.title = doc.title;
    const filters = document.querySelector(".filter-bar");
    filters.replaceChildren(...doc.querySelector(".filter-bar").childNodes);
    for (const id of ["dash-page", "dash-css"]) {
      document.getElementById(id)?.remove();
      const style = doc.getElementById(id);
      if (style) document.head.append(style);
    }
    initFilters();
    const { enhanceSelects } = await import("/static/js/dropdown.js");
    enhanceSelects(filters);
    applyTileHues();
    markDrillTiles();
    applyMode();
    runTiles(dashboard.tiles);
    window.dispatchEvent(new CustomEvent("sqldash:dashboard-refreshed"));
  })();
  try { await refreshPending; }
  finally { refreshPending = null; }
}

connectEvents((payload) => {
  if (document.body.dataset.studioActive === "true") {
    window.dispatchEvent(new CustomEvent("sqldash:studio-change"));
    return;
  }
  if (editing) {
    // The etag is left alone: adopting the new one would let a stale edit save
    // straight over the change instead of meeting the 409 and the reload it asks for.
    if (payload.repeat) return;
    toast("Dashboard file changed on disk — reload to see the latest", "error");
  } else {
    location.reload();
  }
});

/* ---------- boot ---------- */

applyMode();
runTiles(dashboard.tiles);
