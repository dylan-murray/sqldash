import {
  markEmptyChart,
  markTruncated,
  renderBigNumber,
  setFormatConfig,
  translate,
} from "/static/js/charts.js";
import { apiToken as readApiToken } from "/static/js/token.js";

const metric = JSON.parse(document.getElementById("metric-data").textContent);
const token = readApiToken();
const mount = document.getElementById("metric-previews");

setFormatConfig({});

async function runMetric(body) {
  const response = await fetch("/api/run", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Sqldash-Token": token },
    body: JSON.stringify({ metric: metric.name, ...body }),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail ?? `run failed (${response.status})`);
  for (;;) {
    const poll = await fetch(`/api/executions/${payload.id}`);
    const execution = await poll.json();
    if (execution.status === "done") return execution.result;
    if (execution.status === "error") throw new Error(execution.error ?? "query failed");
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
}

function card(title) {
  const el = document.createElement("div");
  el.className = "tile metric-preview";
  el.innerHTML = `
    <div class="tile-head"><h3></h3></div>
    <div class="tile-body"><div class="tile-status"><div class="loading">running…</div></div></div>`;
  el.querySelector("h3").textContent = title;
  mount.appendChild(el);
  return el.querySelector(".tile-body");
}

function fail(body, err) {
  body.innerHTML = "";
  const box = document.createElement("div");
  box.className = "err";
  box.textContent = err.message;
  body.appendChild(box);
}

function chartInto(body, spec, result) {
  body.innerHTML = "";
  const chartMount = document.createElement("div");
  chartMount.className = "chart-mount";
  body.appendChild(chartMount);
  const chart = echarts.init(chartMount, null, { renderer: "canvas" });
  const option = translate(spec, result, undefined, chartMount.clientHeight);
  chart.setOption(option, { notMerge: true });
  markEmptyChart(body, option);
  new ResizeObserver(() => chart.resize()).observe(chartMount);
  markTruncated(body, result);
  window.addEventListener("sqldash:themechange", () => {
    chart.setOption(translate(spec, result, undefined, chartMount.clientHeight), { notMerge: true });
  });
}

const format = metric.format ? { format: metric.format } : {};

const totalBody = card("Total");
runMetric({})
  .then((result) => {
    totalBody.innerHTML = "";
    renderBigNumber(totalBody, { type: "big_number", ...format }, result);
    markTruncated(totalBody, result);
  })
  .catch((err) => fail(totalBody, err));

if (metric.time_dimension) {
  const grain = metric.time_dimension.grain;
  const body = card(`By ${grain}`);
  runMetric({ grain })
    .then((result) => chartInto(body, { type: "area", ...format }, result))
    .catch((err) => fail(body, err));
}

if (metric.dimensions.length) {
  const dim = metric.dimensions[0].name;
  const body = card(`By ${dim}`);
  runMetric({ dimensions: [dim] })
    .then((result) => chartInto(body, { type: "bar", ...format }, result))
    .catch((err) => fail(body, err));
}
