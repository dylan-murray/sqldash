import {
  cleanCombo,
  cleanReference,
  inferSpec,
  markEmptyChart,
  markTruncated,
  own,
  pruneSpecForType,
  referenceKind,
  renderBigNumber,
  RESULT_PAGE,
  renderTable,
  translate,
} from "/static/js/charts.js";
import { enhanceSelects } from "/static/js/dropdown.js";
import { binValues, MAX_BINS } from "/static/js/histogram.js";

const CHART_TYPES = [
  "line", "bar", "area", "scatter", "pie", "histogram", "heatmap", "big_number", "table",
];
const TYPE_LABELS = {
  line: "Line", bar: "Bar", area: "Area", scatter: "Scatter", pie: "Pie",
  histogram: "Histogram", heatmap: "Heatmap", big_number: "Number", table: "Table",
};
const NUMERIC = new Set(["integer", "float", "decimal"]);
// Column encodings: filled in from the result for the preview, but only
// written to the file when the author picked them. A pinned inference is a
// column name the file did not ask for, and it goes stale the moment the
// query's aliases change (#357).
const ENCODING_KEYS = ["x", "y", "group_by", "label", "value"];
const XY_TYPES = ["line", "bar", "area", "scatter"];
const REFERENCE_KINDS = [
  ["line", "Line at a value"],
  ["band", "Band between values"],
  ["marker", "Marker on the x axis"],
  ["span", "Span on the x axis"],
  ["metric", "Metric value"],
];
const REFERENCE_COLORS = [
  ["ink", "Ink"],
  ["muted", "Muted"],
  ["accent", "Accent"],
  ["good", "Good"],
  ["bad", "Bad"],
  ...[1, 2, 3, 4, 5, 6, 7, 8].map((n) => [`series-${n}`, `Series ${n}`]),
];
const REFERENCE_COLOR_TOKENS = {
  ink: "--ink-2",
  muted: "--ink-muted",
  accent: "--accent",
  good: "--good-text",
  bad: "--danger",
};
const REFERENCE_STYLES = [
  ["dashed", "Dashed"],
  ["solid", "Solid"],
  ["dotted", "Dotted"],
];

function blankReference(kind) {
  if (kind === "band") return { y: ["", ""] };
  if (kind === "marker") return { x: "" };
  if (kind === "span") return { x: ["", ""] };
  if (kind === "metric") return { metric: "" };
  return { y: "" };
}
const COMBO_TYPES = ["line", "bar", "area"];
const MARK_LABELS = { line: "Line", bar: "Bar", area: "Area" };
const FORMAT_OPTIONS = [
  ["", "Default format"],
  ["number", "Number"],
  ["currency", "Currency"],
  ["percent", "Percent"],
  ["compact", "Compact"],
];

function isSet(value) {
  return value != null && !(Array.isArray(value) && !value.length);
}

function numberInput(value, key, { min, max, step = "any", placeholder = "" } = {}) {
  const input = document.createElement("input");
  input.type = "number";
  input.dataset.spec = key;
  input.step = step;
  if (min != null) input.min = min;
  if (max != null) input.max = max;
  input.placeholder = placeholder;
  input.value = value ?? "";
  return input;
}

function optionSelect(options, selected, key) {
  const select = document.createElement("select");
  if (key) select.dataset.spec = key;
  for (const [value, text] of options) {
    const picked = (selected ?? "") === value;
    select.appendChild(new Option(text, value, picked, picked));
  }
  return select;
}

const AGGREGATES = [
  ["", "One row each"],
  ["sum", "Sum"],
  ["avg", "Average"],
  ["count", "Count rows"],
  ["min", "Min"],
  ["max", "Max"],
];

function binMode(spec) {
  if (spec.bin_width != null) return "width";
  if (spec.bins != null) return "count";
  return "auto";
}

function checkbox(checked, key) {
  const box = document.createElement("input");
  box.type = "checkbox";
  box.defaultChecked = Boolean(checked);
  if (key) box.dataset.spec = key;
  return box;
}

export class ChartBuilder {
  constructor({ typeEl, encodingEl, previewEl, onChange, metricNames }) {
    this.typeEl = typeEl;
    this.encodingEl = encodingEl;
    this.previewEl = previewEl;
    this.onChange = onChange ?? (() => {});
    this.metricNames = metricNames ?? (() => []);
    this._parkedReferences = null;
    this.result = null;
    this._spec = { type: "table", format: {} };
    this._chart = null;
    this._authored = new Set();
    window.addEventListener("sqldash:themechange", () => this.renderPreview());
  }

  get spec() {
    const spec = { ...this._spec };
    for (const key of ENCODING_KEYS) {
      if (!this._authored.has(key)) delete spec[key];
    }
    if (spec.references) {
      const references = spec.references.map(cleanReference).filter(Boolean);
      if (references.length) spec.references = references;
      else delete spec.references;
    }
    delete spec.series;
    delete spec.axes;
    Object.assign(spec, cleanCombo(this._spec));
    return spec;
  }

  setSpec(spec) {
    this._parkedReferences = null;
    this._spec = { format: {}, ...(spec ?? { type: "table" }) };
    if (Array.isArray(this._spec.references)) {
      this._spec.references = this._spec.references.map((ref) => cleanReference(ref) ?? ref);
    }
    this._authored = new Set(ENCODING_KEYS.filter((key) => isSet(this._spec[key])));
    this.renderAll();
  }

  setResult(result, { infer = true } = {}) {
    this.result = result;
    if (infer && result) this.inferFrom(result);
    this.renderAll();
  }

  inferFrom(result) {
    const references = this._spec.references;
    this._spec = { ...inferSpec(this.spec, result) };
    if (references) this._spec.references = references;
  }

  renderAll() {
    this.renderTypes();
    this.renderEncodings();
    this.renderPreview();
  }

  renderTypes() {
    this.typeEl.innerHTML = "";
    for (const type of CHART_TYPES) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "seg-btn" + (this._spec.type === type ? " active" : "");
      btn.textContent = TYPE_LABELS[type];
      btn.addEventListener("click", () => {
        const refs = this._spec.references?.length ? this._spec.references : null;
        this._spec = pruneSpecForType(this._spec, type);
        if (XY_TYPES.includes(type)) {
          if (!this._spec.references && this._parkedReferences) {
            this._spec.references = this._parkedReferences;
          }
          this._parkedReferences = null;
        } else if (refs) {
          this._parkedReferences = refs;
        }
        if (this.result) this.inferFrom(this.result);
        this.renderAll();
        this.onChange();
      });
      this.typeEl.appendChild(btn);
    }
  }

  columnSelect(key, selected, allowNone = true) {
    const columns = this.result?.columns ?? [];
    const select = document.createElement("select");
    select.dataset.spec = key;
    const add = (value, text = value) => {
      const picked = Boolean(selected) && value === selected;
      select.appendChild(new Option(text, value, picked, picked));
    };
    if (allowNone) add("", "—");
    for (const col of columns) add(col.name);
    if (selected && !columns.some((c) => c.name === selected)) add(selected);
    return select;
  }

  multiYControl() {
    const columns = this.result?.columns ?? [];
    const numeric = columns.filter((c) => NUMERIC.has(c.type));
    const selected = new Set(this._spec.y ?? []);
    const names = !numeric.length && selected.size ? [...selected] : numeric.map((c) => c.name);
    if (!names.length) {
      const hint = document.createElement("span");
      hint.className = "hint";
      hint.textContent = "run the query first";
      return [hint];
    }
    return names.map((name) => {
      const label = document.createElement("label");
      label.className = "check";
      const box = checkbox(selected.has(name));
      box.dataset.specY = "";
      box.value = name;
      label.append(box, ` ${name}`);
      return label;
    });
  }

  renderEncodings() {
    const spec = this._spec;
    const type = spec.type;
    const fields = [];
    if (["line", "bar", "area", "scatter"].includes(type)) {
      fields.push(["X axis", this.columnSelect("x", spec.x)]);
      fields.push(["Y axis", ...this.multiYControl()]);
      fields.push(["Group by", this.columnSelect("group_by", spec.group_by)]);
      if (type === "bar" || type === "area") {
        fields.push(["Stacked", checkbox(spec.stacked, "stacked")]);
      }
      if (type === "bar") {
        fields.push(["Horizontal", checkbox(spec.orientation === "horizontal", "orientation")]);
      }
      if (COMBO_TYPES.includes(type) && (spec.y?.length ?? 0) >= 2) {
        fields.push(["Series", this.seriesControl()]);
      }
      fields.push(["References", this.referencesControl()]);
    } else if (type === "pie") {
      fields.push(["Label", this.columnSelect("label", spec.label)]);
      fields.push(["Value", this.columnSelect("value", spec.value)]);
    } else if (type === "heatmap") {
      fields.push(["X axis", this.columnSelect("x", spec.x)]);
      fields.push(["Y axis", this.columnSelect("y", Array.isArray(spec.y) ? spec.y[0] : spec.y)]);
      fields.push(["Value", this.columnSelect("value", spec.value)]);
      fields.push(["Cells", optionSelect(AGGREGATES, spec.aggregate, "aggregate")]);
      fields.push([
        "Palette",
        optionSelect([["", "Sequential"], ["diverging", "Diverging"]], spec.palette, "palette"),
      ]);
      if (spec.palette === "diverging") {
        fields.push(["Midpoint", numberInput(spec.midpoint, "midpoint", { placeholder: "0" })]);
      }
    } else if (type === "histogram") {
      fields.push(["Column", this.columnSelect("x", spec.x)]);
      const mode = binMode(spec);
      const modeSelect = optionSelect(
        [["auto", "Auto"], ["count", "Bin count"], ["width", "Bin width"]],
        mode
      );
      modeSelect.dataset.binMode = "";
      fields.push(["Bins", modeSelect]);
      if (mode === "count") {
        fields.push(["Count", numberInput(spec.bins, "bins", { min: 1, max: MAX_BINS, step: 1 })]);
      } else if (mode === "width") {
        fields.push(["Width", numberInput(spec.bin_width, "bin_width", { min: 0 })]);
        const start = numberInput(spec.bin_start, "bin_start", { placeholder: "0" });
        start.disabled = spec.bin_width == null;
        fields.push(["Start at", start]);
      }
      fields.push([
        "Show",
        optionSelect([["", "Count"], ["percent", "Percent"]], spec.measure, "measure"),
      ]);
    } else if (type === "big_number") {
      fields.push(["Value", this.columnSelect("value", spec.value)]);
    }

    if (!XY_TYPES.includes(type) && this._parkedReferences?.length) {
      const hint = document.createElement("span");
      hint.className = "hint ref-parked";
      const n = this._parkedReferences.length;
      hint.textContent = `${n} reference${n === 1 ? "" : "s"} set aside: references draw on line, bar, area and scatter charts, and come back if you switch to one`;
      fields.push(["References", hint]);
    }

    this.encodingEl.replaceChildren(
      ...fields.map(([text, ...controls]) => {
        const field = document.createElement("div");
        field.className =
          text === "References"
            ? "field field-refs"
            : text === "Series"
              ? "field field-series"
              : "field field-inline";
        const label = document.createElement("label");
        label.textContent = text;
        field.append(label, ...controls);
        return field;
      })
    );

    this.encodingEl.querySelectorAll("[data-spec]").forEach((input) => {
      input.addEventListener("change", () => {
        const key = input.dataset.spec;
        this._authored.add(key);
        if (key === "orientation") this._spec.orientation = input.checked ? "horizontal" : null;
        else if (input.type === "checkbox") this._spec[key] = input.checked;
        else if (input.type === "number") this._spec[key] = this.numberSetting(key, input.value);
        else this._spec[key] = input.value || null;
        if (key === "palette" && this._spec.palette !== "diverging") this._spec.midpoint = null;
        if (key === "palette" || key === "group_by" || key === "orientation") {
          this.renderEncodings();
        }
        if (this._spec.bin_start != null && this._spec.bin_width == null) this._spec.bin_start = null;
        if (key === "bin_width" && this._spec.bin_width == null) this.renderEncodings();
        this.renderPreview();
        this.onChange();
      });
    });
    this.encodingEl.querySelector("[data-bin-mode]")?.addEventListener("change", (e) => {
      this.setBinMode(e.target.value);
      this.renderEncodings();
      this.renderPreview();
      this.onChange();
    });
    this.encodingEl.querySelectorAll("[data-spec-y]").forEach((box) => {
      box.addEventListener("change", () => {
        this._authored.add("y");
        this._spec.y = [...this.encodingEl.querySelectorAll("[data-spec-y]:checked")].map(
          (b) => b.value
        );
        this.renderEncodings();
        this.renderPreview();
        this.onChange();
      });
    });
    enhanceSelects(this.encodingEl);
  }

  xValues() {
    const columns = this.result?.columns ?? [];
    const xi = columns.findIndex((c) => c.name === this._spec.x);
    if (xi < 0) return [];
    return [...new Set(this.result.rows.map((row) => row[xi]).filter((v) => v != null).map(String))].slice(0, 200);
  }

  referencesChanged({ rerender = false } = {}) {
    if (rerender) this.renderEncodings();
    this.renderPreview();
    this.onChange();
  }

  referencesControl() {
    const wrap = document.createElement("div");
    wrap.className = "ref-list";
    const refs = this._spec.references ?? [];
    const listId = `ref-x-values-${Math.random().toString(36).slice(2, 8)}`;
    const datalist = document.createElement("datalist");
    datalist.id = listId;
    for (const value of this.xValues()) datalist.appendChild(new Option(value));
    wrap.appendChild(datalist);

    refs.forEach((ref, index) => {
      const kind = referenceKind(ref);
      const row = document.createElement("div");
      row.className = "ref-row";
      const token = REFERENCE_COLOR_TOKENS[ref.color ?? "ink"] ?? `--${ref.color}`;
      row.style.setProperty("--ref-color", `var(${token})`);
      const update = (patch, opts) => {
        const next = { ...this._spec.references[index], ...patch };
        for (const [key, value] of Object.entries(patch)) if (value === null) delete next[key];
        this._spec.references[index] = next;
        this.referencesChanged(opts);
      };
      const select = (options, value, onPick, label) => {
        const el = document.createElement("select");
        el.setAttribute("aria-label", label);
        for (const [v, text] of options) el.appendChild(new Option(text, v, v === value, v === value));
        el.addEventListener("change", () => onPick(el.value));
        return el;
      };
      const input = (value, { type = "text", placeholder, label, list, onInput }) => {
        const el = document.createElement("input");
        el.type = type;
        if (type === "number") el.step = "any";
        el.value = value ?? "";
        el.placeholder = placeholder;
        el.setAttribute("aria-label", label);
        if (list) el.setAttribute("list", list);
        el.addEventListener("input", () => onInput(el.value));
        return el;
      };

      const head = document.createElement("div");
      head.className = "ref-head";
      head.append(
        select(REFERENCE_KINDS, kind, (next) => {
          const { label, color, style, format } = this._spec.references[index];
          this._spec.references[index] = { ...blankReference(next), label, color, style, format };
          this.referencesChanged({ rerender: true });
        }, "Reference kind"),
        select(REFERENCE_COLORS, ref.color ?? "ink", (next) => {
          update({ color: next === "ink" ? null : next });
          row.style.setProperty("--ref-color", `var(${REFERENCE_COLOR_TOKENS[next] ?? `--${next}`})`);
        }, "Reference color"),
        select(REFERENCE_STYLES, ref.style ?? "dashed", (next) => update({ style: next === "dashed" ? null : next }), "Line style")
      );
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "btn btn-ghost ref-remove";
      remove.setAttribute("aria-label", "Remove reference");
      remove.title = "Remove reference";
      remove.textContent = "×";
      remove.addEventListener("click", () => {
        this._spec.references.splice(index, 1);
        if (!this._spec.references.length) delete this._spec.references;
        this.referencesChanged({ rerender: true });
      });
      head.appendChild(remove);

      const body = document.createElement("div");
      body.className = "ref-body";
      if (kind === "line") {
        body.appendChild(input(ref.y, { type: "number", placeholder: "Value", label: "Reference value", onInput: (v) => update({ y: v }) }));
      } else if (kind === "band") {
        const pair = ref.y ?? ["", ""];
        body.append(
          input(pair[0], { type: "number", placeholder: "From", label: "Band from", onInput: (v) => update({ y: [v, this._spec.references[index].y?.[1] ?? ""] }) }),
          input(pair[1], { type: "number", placeholder: "To", label: "Band to", onInput: (v) => update({ y: [this._spec.references[index].y?.[0] ?? "", v] }) })
        );
      } else if (kind === "marker") {
        body.appendChild(input(ref.x, { placeholder: "Date or category", label: "Marker position", list: listId, onInput: (v) => update({ x: v }) }));
      } else if (kind === "span") {
        const pair = ref.x ?? ["", ""];
        body.append(
          input(pair[0], { placeholder: "From", label: "Span from", list: listId, onInput: (v) => update({ x: [v, this._spec.references[index].x?.[1] ?? ""] }) }),
          input(pair[1], { placeholder: "To", label: "Span to", list: listId, onInput: (v) => update({ x: [this._spec.references[index].x?.[0] ?? "", v] }) })
        );
      } else {
        const names = this.metricNames();
        if (names.length) {
          const options = [["", "Choose a metric…"], ...names.map((n) => [n, n])];
          if (ref.metric && !names.includes(ref.metric)) options.push([ref.metric, ref.metric]);
          body.appendChild(select(options, ref.metric ?? "", (v) => update({ metric: v }), "Reference metric"));
        } else {
          body.appendChild(input(ref.metric, { placeholder: "Metric name", label: "Reference metric", onInput: (v) => update({ metric: v }) }));
        }
      }
      body.appendChild(input(ref.label, { placeholder: "Label", label: "Reference label", onInput: (v) => update({ label: v || null }) }));
      row.append(head, body);
      if (kind === "metric") {
        const note = document.createElement("span");
        note.className = "hint";
        note.textContent = "Runs with the dashboard's filters, so it shows on the dashboard rather than in this preview";
        row.appendChild(note);
      }
      wrap.appendChild(row);
    });

    const add = document.createElement("button");
    add.type = "button";
    add.className = "btn btn-ghost ref-add";
    add.textContent = "+ Add reference";
    add.addEventListener("click", () => {
      this._spec.references = [...(this._spec.references ?? []), blankReference("line")];
      this.referencesChanged({ rerender: true });
      this.encodingEl.querySelector(".ref-row:last-of-type input")?.focus();
    });
    wrap.appendChild(add);
    return wrap;
  }

  numberSetting(key, text) {
    if (text === "") return null;
    const n = Number(text);
    if (!Number.isFinite(n)) return null;
    if (key === "bins") return Math.min(MAX_BINS, Math.max(1, Math.round(n)));
    if (key === "bin_width") return n > 0 ? n : null;
    return n;
  }

  setBinMode(mode) {
    const spec = this._spec;
    const column = this.result?.columns.findIndex((c) => c.name === spec.x) ?? -1;
    const values = column >= 0 ? this.result.rows.map((row) => row[column]) : [];
    const auto = binValues(values, {});
    if (mode === "count") {
      spec.bins = spec.bins ?? (auto.bins.length || 10);
      spec.bin_width = null;
      spec.bin_start = null;
    } else if (mode === "width") {
      spec.bin_width = spec.bin_width ?? (auto.width ? Number(auto.width.toPrecision(12)) : 1);
      spec.bins = null;
    } else {
      spec.bins = null;
      spec.bin_width = null;
      spec.bin_start = null;
    }
  }

  seriesChanged({ rerender = false } = {}) {
    if (rerender) this.renderEncodings();
    this.renderPreview();
    this.onChange();
  }

  columnFormat(column) {
    const format = this._spec.format;
    if (typeof format === "string") return format;
    return own(format, column) ?? "";
  }

  setColumnFormat(column, value) {
    let format = this._spec.format;
    const shared = typeof format === "string" ? format : null;
    format = Object.assign(Object.create(null), shared ? {} : format);
    if (shared) for (const name of this._spec.y ?? []) format[name] = shared;
    if (value) format[column] = value;
    else delete format[column];
    this._spec.format = format;
  }

  seriesControl() {
    const wrap = document.createElement("div");
    wrap.className = "series-list";
    const spec = this._spec;
    if (spec.group_by || spec.orientation === "horizontal") {
      const hint = document.createElement("span");
      hint.className = "hint series-hint";
      hint.textContent = spec.group_by
        ? "Group by splits one column into a series per value, so marks and a second axis are off while it is set"
        : "A horizontal bar has one value axis, so marks and a second axis are off while it is set";
      wrap.appendChild(hint);
      return wrap;
    }
    const select = (options, value, onPick, label) => {
      const el = document.createElement("select");
      el.setAttribute("aria-label", label);
      for (const [v, text] of options) el.appendChild(new Option(text, v, v === value, v === value));
      el.addEventListener("change", () => onPick(el.value));
      return el;
    };
    const text = (value, placeholder, label, onInput) => {
      const el = document.createElement("input");
      el.type = "text";
      el.value = value ?? "";
      el.placeholder = placeholder;
      el.setAttribute("aria-label", label);
      el.addEventListener("input", () => onInput(el.value));
      return el;
    };
    const entry = (column) => own(spec.series, column) ?? {};
    const patch = (column, change, opts) => {
      spec.series = Object.assign(Object.create(null), spec.series);
      spec.series[column] = { ...entry(column), ...change };
      this.seriesChanged(opts);
    };
    spec.y.forEach((column, index) => {
      const row = document.createElement("div");
      row.className = "series-row";
      row.dataset.column = column;
      row.style.setProperty("--series-color", `var(--series-${(index % 8) + 1})`);
      const name = document.createElement("span");
      name.className = "series-name";
      name.textContent = column;
      name.title = column;
      const marks = COMBO_TYPES.map((t) => [t, t === spec.type ? `${MARK_LABELS[t]} (chart)` : MARK_LABELS[t]]);
      row.append(
        name,
        select(marks, entry(column).type || spec.type, (v) => patch(column, { type: v }), `Mark for ${column}`),
        select(
          [["left", "Left axis"], ["right", "Right axis"]],
          entry(column).axis || "left",
          (v) => patch(column, { axis: v }, { rerender: true }),
          `Axis for ${column}`
        ),
        select(FORMAT_OPTIONS, this.columnFormat(column), (v) => {
          this.setColumnFormat(column, v);
          this.seriesChanged();
        }, `Format for ${column}`),
        text(entry(column).label, "Legend name", `Legend name for ${column}`, (v) => patch(column, { label: v }))
      );
      wrap.appendChild(row);
    });
    const onRight = spec.y.some((column) => entry(column).axis === "right");
    const titles = document.createElement("div");
    titles.className = "series-axes";
    const axisTitle = (side, label) =>
      text(spec.axes?.[side]?.title, `${label} axis title`, `${label} axis title`, (v) => {
        spec.axes = { ...(spec.axes ?? {}), [side]: { ...(spec.axes?.[side] ?? {}), title: v } };
        this.seriesChanged();
      });
    titles.appendChild(axisTitle("left", "Left"));
    if (onRight) titles.appendChild(axisTitle("right", "Right"));
    wrap.appendChild(titles);
    return wrap;
  }

  renderPreview() {
    const preview = this.previewEl;
    this._chart?.dispose();
    this._chart = null;
    if (!this.result) {
      preview.innerHTML = `<div class="placeholder">Run the query to preview</div>`;
      return;
    }
    const spec = this.spec;
    preview.innerHTML = "";
    if (spec.type === "big_number") {
      renderBigNumber(preview, spec, this.result);
      return markTruncated(preview, this.result);
    }
    // renderTable brings its own note, inside the scrolling wrap.
    if (spec.type === "table") return renderTable(preview, spec, this.result, { page: RESULT_PAGE });
    const mount = document.createElement("div");
    mount.className = "chart-mount";
    preview.appendChild(mount);
    this._chart = echarts.init(mount);
    const option = translate(
      spec,
      this.result,
      undefined,
      this._chart.getDom().clientHeight,
      this._chart.getDom().clientWidth
    );
    this._chart.setOption(option, { notMerge: true });
    markEmptyChart(preview, option);
    new ResizeObserver(() => this._chart?.resize()).observe(mount);
    // The preview is where an author decides whether a chart says what they
    // meant. A series cut short by the row cap, with nothing to say so, is the
    // same misreading the tiles were fixed for.
    markTruncated(preview, this.result);
  }

  dispose() {
    this._chart?.dispose();
    this._chart = null;
  }
}

export function slugify(text, fallback) {
  const slug = (text || "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
  return slug || fallback;
}
