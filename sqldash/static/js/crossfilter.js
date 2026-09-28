import { escapeHtml } from "./charts.js";
import { clickValue, clickedRow } from "./drill.js";

const FUNNEL =
  '<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true" fill="none" ' +
  'stroke="currentColor" stroke-width="1.6" stroke-linejoin="round">' +
  '<path d="M2.5 3.5h11L9.2 8.6v3.9l-2.4 1V8.6z"/></svg>';

export function crossFilterPlan(tile, filters) {
  const mapping = tile.cross_filter;
  if (!mapping) return null;
  const entries = [];
  const errors = [];
  for (const [name, column] of Object.entries(mapping)) {
    const def = filters.find((f) => f.name === name);
    if (!def) errors.push(`cross_filter '${name}' is not a filter on this dashboard`);
    else if (def.type === "daterange") errors.push(`cross_filter '${name}' is a date range`);
    else entries.push({ name, column, def });
  }
  return { entries, errors };
}

export function offValue(def, options = []) {
  if (def.type === "select" && options.includes("all")) return "all";
  const fallback = def.resolved_default;
  return fallback === null || fallback === undefined ? "" : String(fallback);
}

export function picked(plan, row, columns) {
  const values = {};
  for (const { name, column, def } of plan.entries) {
    const at = columns.findIndex((c) => c.name === column);
    if (at < 0) return { error: `column '${column}' is not in this tile's result` };
    const { text, error } = clickValue(row?.[at], def.type, column);
    if (error) return { error };
    values[name] = text;
  }
  return { values };
}

export function activeValues(plan, current, offs) {
  const values = {};
  let on = false;
  for (const { name, def } of plan.entries) {
    const value = current[name];
    if (value === undefined || value === "") continue;
    if (value !== offs[name]) on = true;
    if (!(def.type === "select" && value === "all")) values[name] = value;
  }
  return on ? values : null;
}

export function toggled(plan, values, current, offs) {
  const same = plan.entries.every(({ name }) => current[name] === values[name]);
  const next = {};
  for (const { name } of plan.entries) next[name] = same ? offs[name] : values[name];
  return next;
}

export function rowIsPicked(plan, row, columns, active) {
  const { values } = picked(plan, row, columns);
  return (
    Boolean(values) &&
    plan.entries.every(({ name }) => !(name in active) || values[name] === active[name])
  );
}

export function dimUnpicked(option, spec, result, plan, active, opacity = 0.28) {
  return (option.series ?? []).map((series) => {
    const line = series.type === "line";
    let missed = false;
    const data = (series.data ?? []).map((datum, dataIndex) => {
      const name = Array.isArray(datum) ? datum[0] : (datum?.name ?? datum);
      const row = clickedRow(spec, result, { dataIndex, seriesName: series.name, name });
      const item = datum !== null && typeof datum === "object" && !Array.isArray(datum)
        ? { ...datum }
        : { value: datum };
      if (row && rowIsPicked(plan, row, result.columns, active)) return item;
      missed = true;
      if (line && !series.showSymbol) {
        return {
          ...item,
          itemStyle: { ...(item.itemStyle ?? {}), opacity: 0 },
          emphasis: { itemStyle: { opacity: 1 } },
        };
      }
      return { ...item, itemStyle: { ...(item.itemStyle ?? {}), opacity } };
    });
    if (!line || !missed) return { data };
    const fade = (style) => ({ opacity: (style?.opacity ?? 1) * opacity });
    const faded = { data, showSymbol: true, lineStyle: fade(series.lineStyle) };
    if (series.areaStyle) faded.areaStyle = fade(series.areaStyle);
    return faded;
  });
}

export function crossFilterChip(plan, active) {
  const labels = plan.entries.map(({ def }) => def.label || def.name).join(" and ");
  if (plan.errors.length) {
    const chip = document.createElement("span");
    chip.className = "tile-xf is-broken";
    chip.innerHTML = `${FUNNEL}<span>Cross-filter unavailable</span>`;
    chip.title = plan.errors.join("\n");
    return chip;
  }
  if (!active) {
    const chip = document.createElement("span");
    chip.className = "tile-xf";
    chip.innerHTML = `${FUNNEL}<span>${escapeHtml(labels)}</span>`;
    chip.title = `Click to filter the dashboard by ${labels}`;
    return chip;
  }
  const chip = document.createElement("button");
  chip.type = "button";
  chip.className = "tile-xf is-active";
  const shown = plan.entries
    .filter(({ name }) => name in active)
    .map(({ name }) => active[name])
    .join(" · ");
  chip.innerHTML =
    `${FUNNEL}<span>${escapeHtml(shown)}</span>` +
    '<svg class="xf-clear" viewBox="0 0 16 16" width="10" height="10" aria-hidden="true" ' +
    'fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round">' +
    '<path d="m4 4 8 8M12 4l-8 8"/></svg>';
  chip.title = `Clear ${labels}`;
  chip.setAttribute("aria-label", `Clear ${labels} (${shown})`);
  return chip;
}
