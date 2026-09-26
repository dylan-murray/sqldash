export function shiftIso(iso, days, years) {
  const d = new Date(`${iso}T00:00:00Z`);
  if (years) {
    const month = d.getUTCMonth();
    d.setUTCFullYear(d.getUTCFullYear() - years);
    if (d.getUTCMonth() !== month) d.setUTCDate(0);
  }
  if (days) d.setUTCDate(d.getUTCDate() - days);
  return d.toISOString().slice(0, 10);
}

const PRESET_ALIASES = { mtd: "month_to_date", ytd: "year_to_date" };
const RELATIVE_DAYS = { d: 1, w: 7, m: 30, y: 365 };

function daysBefore(today, days) {
  const d = new Date(`${today}T00:00:00Z`);
  d.setUTCDate(d.getUTCDate() - days);
  return d.getUTCFullYear() >= 1 ? d.toISOString().slice(0, 10) : null;
}

/** Resolve a daterange preset against a given day, mirroring
`params.resolve_daterange_preset`. `today` is the *server's* date, stamped into the
page payload: the browser's own clock answers a different question (its UTC date is
tomorrow for an evening in the Americas), and the preset has to mean one window on
every surface. `tests/daterange_presets.json` pins this against the Python side. */
export function presetRange(preset, today) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(today ?? "")) return null;
  const token = String(preset ?? "").trim();
  const canonical = PRESET_ALIASES[token] ?? token;
  const relative = /^-(\d+)([dwmy])$/.exec(canonical);
  if (relative) {
    const days = Number(relative[1]) * RELATIVE_DAYS[relative[2]];
    const start = daysBefore(today, days);
    return start && { preset: `last_${days}_days`, start, end: today };
  }
  const last = /^last_(\d+)_days$/.exec(canonical);
  if (last) {
    const start = daysBefore(today, Number(last[1]));
    return start && { preset: canonical, start, end: today };
  }
  if (canonical === "month_to_date") {
    return { preset: canonical, start: `${today.slice(0, 7)}-01`, end: today };
  }
  if (canonical === "year_to_date") {
    return { preset: canonical, start: `${today.slice(0, 4)}-01-01`, end: today };
  }
  return null;
}

export function compareWindow(mode, start, end) {
  if (!start || !end) return null;
  if (mode === "yoy") {
    return { start: shiftIso(start, 0, 1), end: shiftIso(end, 0, 1), label: "last year" };
  }
  const days =
    Math.round((new Date(`${end}T00:00:00Z`) - new Date(`${start}T00:00:00Z`)) / 86400000) + 1;
  return {
    start: shiftIso(start, days, 0),
    end: shiftIso(end, days, 0),
    label: "previous period",
  };
}

/** Settle a tile's run and its compare run, the current run's error first. Both
are refused for an inverted range, but the compare body carries the prior window
runner.js derived, which the user never typed; `Promise.all` rejected with
whichever 422 landed first, and the compare request is sent first. */
export async function currentThenPrevious(current, previous) {
  const [now, before] = await Promise.allSettled([current, previous]);
  if (now.status === "rejected") throw now.reason;
  if (before.status === "rejected") throw before.reason;
  return [now.value, before.value];
}
