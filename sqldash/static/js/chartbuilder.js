import {
  inferSpec,
  markEmptyChart,
  markTruncated,
  pruneSpecForType,
  renderBigNumber,
  RESULT_PAGE,
  renderTable,
  translate,
} from "/static/js/charts.js";
import { enhanceSelects } from "/static/js/dropdown.js";

const CHART_TYPES = ["line", "bar", "area", "scatter", "pie", "big_number", "table"];
const TYPE_LABELS = {
  line: "Line", bar: "Bar", area: "Area", scatter: "Scatter",
  pie: "Pie", big_number: "Number", table: "Table",
};
const NUMERIC = new Set(["integer", "float", "decimal"]);
// Column encodings: filled in from the result for the preview, but only
// written to the file when the author picked them. A pinned inference is a
// column name the file did not ask for, and it goes stale the moment the
// query's aliases change (#357).
const ENCODING_KEYS = ["x", "y", "group_by", "label", "value"];

function isSet(value) {
  return value != null && !(Array.isArray(value) && !value.length);
}

function checkbox(checked, key) {
  const box = document.createElement("input");
  box.type = "checkbox";
  box.defaultChecked = Boolean(checked);
  if (key) box.dataset.spec = key;
  return box;
}

export class ChartBuilder {
  constructor({ typeEl, encodingEl, previewEl, onChange }) {
    this.typeEl = typeEl;
    this.encodingEl = encodingEl;
    this.previewEl = previewEl;
    this.onChange = onChange ?? (() => {});
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
    return spec;
  }

  setSpec(spec) {
    this._spec = { format: {}, ...(spec ?? { type: "table" }) };
    this._authored = new Set(ENCODING_KEYS.filter((key) => isSet(this._spec[key])));
    this.renderAll();
  }

  setResult(result, { infer = true } = {}) {
    this.result = result;
    if (infer && result) this._spec = { ...inferSpec(this.spec, result) };
    this.renderAll();
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
        this._spec = pruneSpecForType(this._spec, type);
        if (this.result) this._spec = { ...inferSpec(this.spec, this.result) };
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
    } else if (type === "pie") {
      fields.push(["Label", this.columnSelect("label", spec.label)]);
      fields.push(["Value", this.columnSelect("value", spec.value)]);
    } else if (type === "big_number") {
      fields.push(["Value", this.columnSelect("value", spec.value)]);
    }

    this.encodingEl.replaceChildren(
      ...fields.map(([text, ...controls]) => {
        const field = document.createElement("div");
        field.className = "field field-inline";
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
        else this._spec[key] = input.value || null;
        this.renderPreview();
        this.onChange();
      });
    });
    this.encodingEl.querySelectorAll("[data-spec-y]").forEach((box) => {
      box.addEventListener("change", () => {
        this._authored.add("y");
        this._spec.y = [...this.encodingEl.querySelectorAll("[data-spec-y]:checked")].map(
          (b) => b.value
        );
        this.renderPreview();
        this.onChange();
      });
    });
    enhanceSelects(this.encodingEl);
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
    const option = translate(spec, this.result, undefined, this._chart.getDom().clientHeight);
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
