import { escapeHtml, inferSpec } from "./charts.js";

const NUMBER_TEXT = /^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/;
const INPUT_NUMBER = /^-?(\d+(\.\d+)?|\.\d+)([eE][+-]?\d+)?$/;
const ISO_DAY = /^(\d{4})-(\d{2})-(\d{2})/;

function numberText(text) {
  if (INPUT_NUMBER.test(text)) return text;
  return text.replace(/^\+/, "").replace(/\.(?=[eE]|$)/, "");
}

function calendarDay(text) {
  const match = ISO_DAY.exec(text);
  if (!match) return null;
  const [day, year, month, date] = match;
  const at = new Date(0);
  at.setUTCFullYear(Number(year), Number(month) - 1, Number(date));
  const same =
    at.getUTCFullYear() === Number(year) &&
    at.getUTCMonth() === Number(month) - 1 &&
    at.getUTCDate() === Number(date);
  return same ? day : null;
}

export function rowForPoint(spec, result, point) {
  const rows = result.rows ?? [];
  if (!rows.length) return null;
  const s = inferSpec({ ...spec, y: spec.y ? [...spec.y] : spec.y }, result);
  const index = (name) => result.columns.findIndex((c) => c.name === name);
  const i = point.dataIndex;
  if (s.type === "big_number") return rows[0];
  if (s.type === "pie") return rows[i] ?? null;
  if (["line", "bar", "area", "scatter"].includes(s.type)) {
    if (s.group_by && (s.y?.length ?? 0) === 1) {
      const gi = index(s.group_by);
      const members = rows.filter((row) => String(row[gi] ?? "∅") === String(point.seriesName));
      return members[i] ?? null;
    }
    return rows[i] ?? null;
  }
  const key = index(s.x ?? s.label);
  if (key < 0) return rows[i] ?? null;
  return rows.find((row) => String(row[key]) === String(point.name)) ?? null;
}

function valueText(value, type, column) {
  if (value === null || value === undefined || value === "") {
    return { error: `${column} is empty here, so there is nothing to drill with` };
  }
  const text = typeof value === "object" ? JSON.stringify(value) : String(value);
  if (type === "number" && !NUMBER_TEXT.test(text.trim())) {
    return { error: `${column} is '${text}', which is not a number` };
  }
  if (type === "date") {
    const day = calendarDay(text);
    if (!day) return { error: `${column} is '${text}', which is not a date` };
    return { text: day };
  }
  return { text: type === "number" ? numberText(text.trim()) : text };
}

const NUMERIC_COLUMNS = new Set(["integer", "float", "decimal"]);

export function valueKind(columnType) {
  if (NUMERIC_COLUMNS.has(columnType)) return "number";
  return columnType === "boolean" ? "boolean" : "string";
}

function typed(value, kind) {
  const text = String(value).trim();
  if (kind === "boolean") {
    const lower = text.toLowerCase();
    return lower === "true" || lower === "false" ? lower : null;
  }
  if (kind !== "number") return null;
  if (/^[+-]?\d+$/.test(text)) return BigInt(text).toString();
  return NUMBER_TEXT.test(text) ? String(Number(text)) : null;
}

export function matchOption(options, value, kind = "string") {
  const exact = options.find((option) => option.value === value);
  if (exact || kind === "string") return exact?.value;
  const typedOptions = options.filter((o) => o.kind === "number" || o.kind === "boolean");
  return typedOptions.find((option) => {
    if (kind !== null && option.kind !== kind) return false;
    const wanted = typed(value, option.kind);
    return wanted !== null && wanted === typed(option.value, option.kind);
  })?.value;
}

export function drillUrl(plan, row, columns, context) {
  if (!plan.href) return { error: "this drill has no dashboard to open" };
  const url = new URL(plan.href, "http://sqldash.invalid");
  const self = plan.target === context.dashboardName;
  if (self) {
    for (const [key, value] of new URLSearchParams(context.search ?? "")) {
      if (key.startsWith("f_")) url.searchParams.set(key, value);
    }
  }
  for (const param of plan.params) {
    let text;
    let kind;
    if (param.current !== undefined) {
      text = context.filters[param.current];
      kind = context.kinds?.[param.current] ?? param.kind ?? "string";
      if (text === undefined || text === "") continue;
    } else {
      const at = columns.findIndex((c) => c.name === param.column);
      if (at < 0) return { error: `column '${param.column}' is not in this tile's result` };
      const value = valueText(row?.[at], param.type, param.column);
      if (value.error) return { error: value.error };
      text = value.text;
      kind = valueKind(columns[at].type);
    }
    if (param.options) {
      const options = param.options.map((value, i) => ({
        value,
        kind: param.option_kinds?.[i] ?? "string",
      }));
      const option = matchOption(options, text, kind);
      if (option === undefined) {
        return { error: `'${text}' is not one of the options of ${plan.title}'s ${param.param} filter` };
      }
      text = option;
    }
    url.searchParams.set(`f_${param.param}`, text);
  }
  if (!self) url.searchParams.set("from", context.dashboardName);
  return { href: url.pathname + url.search };
}

export function followDrill(plan, href, newTab = false) {
  if (plan.new_tab || newTab) window.open(href, "_blank", "noopener");
  else location.assign(href);
}

const ARROW =
  '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true" fill="none" ' +
  'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">' +
  '<path d="M5 11 11 5M6 5h5v5"/></svg>';
const WARN =
  '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true" fill="none" ' +
  'stroke="currentColor" stroke-width="1.6" stroke-linecap="round">' +
  '<circle cx="8" cy="8" r="6"/><path d="M8 5v3.5M8 11h.01"/></svg>';

export function markDrillTile(el, plan) {
  const head = el.querySelector(".tile-head");
  if (!head) return;
  head.querySelector(".tile-drill")?.remove();
  if (!plan) return;
  const hint = document.createElement("span");
  hint.className = "tile-drill";
  const broken = plan.errors.length > 0;
  hint.classList.toggle("is-broken", broken);
  hint.innerHTML = `${broken ? WARN : ARROW}<span>${escapeHtml(broken ? "Drill unavailable" : plan.title ?? "")}</span>`;
  hint.title = broken ? plan.errors.join("\n") : `Click to open ${plan.title} filtered to what you picked`;
  head.querySelector(".tile-actions")?.before(hint);
}

export function tableDrillCells(plan, result, context, notify) {
  if (!plan || plan.errors.length) return undefined;
  refreshDrillLinks();
  const column = plan.column ?? result.columns[0]?.name;
  const at = result.columns.findIndex((c) => c.name === column);
  if (at < 0) {
    notify(`drill column '${column}' is not in this tile's result`);
    return undefined;
  }
  return (td, row, index) => {
    if (index !== at) return;
    const link = document.createElement("a");
    link.className = "cell-link";
    if (plan.new_tab) {
      link.target = "_blank";
      link.rel = "noopener";
    }
    const refresh = () => {
      const next = drillUrl(plan, row, result.columns, context());
      if (next.href) {
        link.href = next.href;
        link.removeAttribute("tabindex");
        link.removeAttribute("role");
      } else {
        link.removeAttribute("href");
        link.tabIndex = 0;
        link.setAttribute("role", "link");
      }
      td.title = next.href ? `Open ${plan.title}` : next.error;
      return next;
    };
    const guard = (e, newTab = false) => {
      e.stopPropagation();
      const linked = link.hasAttribute("href");
      const { href, error } = refresh();
      if (href && linked) return;
      e.preventDefault();
      if (href) followDrill(plan, href, newTab || e.metaKey || e.ctrlKey);
      else notify(error);
    };
    for (const type of ["pointerenter", "pointerdown", "focus"]) link.addEventListener(type, refresh);
    link.addEventListener("click", (e) => guard(e));
    link.addEventListener("auxclick", (e) => {
      if (e.button === 1) guard(e, true);
    });
    link.addEventListener("keydown", (e) => {
      e.stopPropagation();
      if (e.key === "Enter" && !link.hasAttribute("href")) guard(e);
    });
    liveLinks.add({ link, refresh });
    link.append(...td.childNodes);
    td.replaceChildren(link);
    td.classList.add("has-link");
    td.tabIndex = -1;
    refresh();
  };
}

const liveLinks = new Set();

export function refreshDrillLinks() {
  for (const entry of liveLinks) {
    if (entry.link.isConnected) entry.refresh();
    else liveLinks.delete(entry);
  }
}

function pointAt(chart, seriesIndex, dataIndex) {
  const series = chart.getOption().series?.[seriesIndex];
  const datum = series?.data?.[dataIndex];
  const name = Array.isArray(datum) ? datum[0] : datum?.name ?? datum;
  return { componentType: "series", seriesIndex, dataIndex, seriesName: series?.name, name };
}

export function chartKeys(mount, chart, label, activate) {
  mount.tabIndex = 0;
  mount.setAttribute("role", "application");
  mount.setAttribute("aria-label", label);
  const at = { series: 0, index: -1 };
  const lengthOf = (s) => chart.getOption().series?.[s]?.data?.length ?? 0;
  const show = () => {
    chart.dispatchAction({ type: "downplay" });
    chart.dispatchAction({ type: "highlight", seriesIndex: at.series, dataIndex: at.index });
    chart.dispatchAction({ type: "showTip", seriesIndex: at.series, dataIndex: at.index });
  };
  mount.onkeydown = (e) => {
    const seriesCount = chart.getOption().series?.length ?? 0;
    if (!seriesCount) return;
    const count = lengthOf(at.series);
    if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
      if (!count) return;
      const step = e.key === "ArrowRight" ? 1 : -1;
      at.index = at.index < 0 ? (step > 0 ? 0 : count - 1) : (at.index + step + count) % count;
    } else if (e.key === "ArrowUp" || e.key === "ArrowDown") {
      if (seriesCount < 2) return;
      at.series = (at.series + (e.key === "ArrowDown" ? 1 : -1) + seriesCount) % seriesCount;
      at.index = Math.min(Math.max(at.index, 0), Math.max(lengthOf(at.series) - 1, 0));
    } else if ((e.key === "Enter" || e.key === " ") && at.index >= 0) {
      e.preventDefault();
      activate(pointAt(chart, at.series, at.index), e);
      return;
    } else if (e.key === "Escape") {
      at.index = -1;
      chart.dispatchAction({ type: "downplay" });
      chart.dispatchAction({ type: "hideTip" });
      return;
    } else {
      return;
    }
    e.preventDefault();
    show();
  };
  mount.onblur = () => {
    chart.dispatchAction({ type: "downplay" });
    chart.dispatchAction({ type: "hideTip" });
  };
}

export function initDrillCrumb() {
  const back = document.getElementById("drill-back");
  if (!back) return;
  let previous = null;
  try {
    previous = document.referrer ? new URL(document.referrer) : null;
  } catch {
    previous = null;
  }
  const fromOrigin = previous && previous.origin === location.origin;
  if (fromOrigin && previous.pathname === new URL(back.href).pathname) {
    back.href = previous.pathname + previous.search;
  }
  back.addEventListener("click", (e) => {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return;
    if (fromOrigin && history.length > 1 && back.href === previous.href) {
      e.preventDefault();
      history.back();
    }
  });
}
