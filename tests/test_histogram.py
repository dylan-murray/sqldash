"""Histogram tiles: the `chart:` keys that define bins, how lint keeps them on
histograms, how an edit writes them back, and what the browser draws from a
real DuckDB result."""

import time

import pytest
from pydantic import ValidationError

from sqldash.lint import lint_project
from sqldash.models.chart import ChartSpec
from sqldash.project.store import DashboardStore
from sqldash.semantics import SemanticLayer
from sqldash.server import create_app
from sqldash.snapshot import _start_server

UNBINNABLE = "These values span too wide or too narrow a range to bin"


def lint_errors(root):
    store = DashboardStore(root)
    return [f.message for f in lint_project(store, SemanticLayer(store)) if f.level == "error"]


def test_bin_definitions_parse():
    spec = ChartSpec.model_validate(
        {"type": "histogram", "x": "amount", "bin_width": 25, "bin_start": 5, "measure": "percent"}
    )
    assert (spec.bin_width, spec.bin_start, spec.measure) == (25, 5, "percent")
    assert ChartSpec.model_validate({"type": "histogram", "bins": 12}).bins == 12


@pytest.mark.parametrize(
    ("chart", "message"),
    [
        ({"bins": 10, "bin_width": 5}, "not both"),
        ({"bin_start": 3}, "needs a bin_width"),
        ({"bins": 0}, "greater than or equal to 1"),
        ({"bins": 201}, "less than or equal to 200"),
        ({"bins": 2.5}, "valid integer"),
        ({"bins": True}, "valid integer"),
        ({"bin_width": 0}, "greater than 0"),
        ({"bin_width": float("inf")}, "finite"),
        ({"measure": "density"}, "count"),
    ],
)
def test_bin_definitions_that_cannot_mean_anything_are_refused(chart, message):
    with pytest.raises(ValidationError, match=message):
        ChartSpec.model_validate({"type": "histogram", **chart})


def test_lint_keeps_histogram_keys_on_histograms(tmp_path):
    (tmp_path / "d.yaml").write_text(
        "title: D\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  - {title: A, chart: {type: bar, bins: 10}, sql: 'SELECT 1 AS n'}\n"
        "  - {title: B, chart: {type: line, measure: percent}, sql: 'SELECT 1 AS n'}\n"
        "  - {title: C, chart: {type: histogram, x: n, y: [n]}, sql: 'SELECT 1 AS n'}\n"
        "  - {title: E, chart: {type: histogram, x: n, bin_width: 5}, sql: 'SELECT 1 AS n'}\n"
    )
    errors = lint_errors(tmp_path)
    assert any("bins is only valid on histogram charts, not bar" in m for m in errors), errors
    assert any("measure is only valid on histogram charts, not line" in m for m in errors), errors
    assert any("tile 'c'" in m and "takes no y" in m for m in errors), errors
    assert not any("tile 'e'" in m for m in errors), errors


def test_a_saved_bin_definition_reloads_and_edits_one_key(tmp_path):
    doc = (
        "title: D\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  - title: Order values\n"
        "    sql: SELECT 1 AS amount\n"
        "    chart: {type: histogram, x: amount, bin_width: 25, bin_start: 5}\n"
    )
    (tmp_path / "d.yaml").write_text(doc)
    store = DashboardStore(tmp_path)
    dashboard, _, etag = store.load("d")
    chart = dashboard.tiles[0].chart
    assert (chart.type, chart.x, chart.bin_width, chart.bin_start) == ("histogram", "amount", 25, 5)
    payload = {
        "id": "order_values",
        "title": "Order values",
        "query": "order_values",
        "chart": {
            "type": "histogram",
            "x": "amount",
            "bins": None,
            "bin_width": 10,
            "bin_start": 5,
            "measure": "percent",
        },
    }
    store.upsert_tile("d", payload, "SELECT 1 AS amount", etag)
    text = (tmp_path / "d.yaml").read_text()
    assert text == doc.replace(
        "bin_width: 25, bin_start: 5}", "bin_width: 10, bin_start: 5, measure: percent}"
    ), text
    reloaded, _, _ = store.load("d")
    assert reloaded.tiles[0].chart.bin_width == 10
    assert reloaded.tiles[0].chart.measure == "percent"


def _browser_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as p:
            p.chromium.launch().close()
        return True
    except Exception:
        return False


DISTRIBUTIONS = """\
title: Distributions
source: {type: duckdb, database: ':memory:'}
tiles:
  - title: Mixed
    chart: {type: histogram, x: v, bin_width: 10}
    sql: |
      SELECT * FROM (VALUES (-15.0), (-10.0), (-0.5), (0.0), (9.99), (10.0), (NULL), (NULL),
        (30.0)) t(v)
  - title: Constant
    chart: {type: histogram, x: v}
    sql: SELECT 7 AS v FROM range(0, 12)
  - title: Skewed
    chart: {type: histogram, x: v, bins: 20, measure: percent}
    sql: SELECT exp(i / 40.0) AS v FROM range(0, 400) t(i)
  - title: Truncated
    chart: {type: histogram, x: v}
    sql: SELECT i AS v FROM range(0, 5000) t(i)
  - title: Too wide
    chart: {type: histogram, x: v}
    sql: SELECT * FROM (VALUES (-1e308), (1e308)) t(v)
  - title: Shifted
    chart: {type: histogram, x: v, bin_width: 1, bin_start: 0.001}
    sql: SELECT * FROM (VALUES (0.001), (1.001), (2.001)) t(v)
"""


@pytest.mark.skipif(not _browser_available(), reason="playwright browser not installed")
def test_histograms_render_real_duckdb_distributions(tmp_path):
    from playwright.sync_api import sync_playwright

    (tmp_path / "d.yaml").write_text(DISTRIBUTIONS)
    app = create_app(tmp_path, allowed_hosts=["127.0.0.1", "localhost"], row_limit=1000)
    server, thread, port = _start_server(app)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1300, "height": 1000})
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{port}/d/d", wait_until="load")
            deadline = time.monotonic() + 30
            while page.evaluate("() => document.querySelectorAll('.tile-status .skeleton').length"):
                assert time.monotonic() < deadline, "tiles never finished loading"
                time.sleep(0.2)
            page.wait_for_timeout(500)
            state = page.evaluate("""() => Object.fromEntries(
                [...document.querySelectorAll('.tile')].map(tile => {
                    const mount = tile.querySelector('.chart-mount');
                    const option = echarts.getInstanceByDom(mount).getOption();
                    return [tile.dataset.tileId, {
                        bins: option.series[0].data,
                        scope: option.series[1].data[0][0],
                        ticks: option.xAxis[0].axisLabel.customValues ?? null,
                        note: tile.querySelector('.truncated-note')?.textContent ?? null,
                        empty: tile.querySelector('.chart-empty')?.textContent ?? null,
                        texts: echarts.getInstanceByDom(mount).getZr().storage.getDisplayList()
                            .filter(e => e.type === 'tspan').map(e => e.style.text),
                    }];
                }))""")
            page.set_viewport_size({"width": 390, "height": 1000})
            page.wait_for_timeout(800)
            narrow = page.evaluate("""() => {
                const mount = document.querySelector('[data-tile-id="mixed"] .chart-mount');
                const chart = echarts.getInstanceByDom(mount);
                const scope = chart.getZr().storage.getDisplayList()
                    .filter(e => (e.style?.text ?? '').startsWith('7 value'));
                return {
                    width: chart.getWidth(),
                    texts: scope.map(e => [e.style.text, e.x + e.getBoundingRect().width]),
                };
            }""")
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    assert not errors, errors
    assert state["mixed"]["bins"] == [
        [-20, -10, 1],
        [-10, 0, 2],
        [0, 10, 2],
        [10, 20, 1],
        [20, 30, 1],
    ], state["mixed"]
    assert state["mixed"]["scope"] == "7 values · 2 nulls excluded"
    assert state["constant"]["bins"] == [[7, 7, 12]]
    skewed = state["skewed"]["bins"]
    assert len(skewed) == 20
    assert sum(b[2] for b in skewed) == pytest.approx(1)
    assert skewed[0][2] > skewed[-1][2]
    ticks = state["skewed"]["ticks"]
    step = ticks[1] - ticks[0]
    assert step in (1000, 2000, 2500, 5000), ticks
    assert all(t % step == 0 for t in ticks), ticks
    truncated = state["truncated"]
    assert sum(b[2] for b in truncated["bins"]) == 1000
    assert truncated["scope"] == "1,000 values"
    assert truncated["note"] == "showing first 1,000 rows (truncated)"
    assert state["too_wide"]["bins"] == []
    assert state["too_wide"]["empty"] == UNBINNABLE
    assert {"0.001", "1.001", "2.001"} <= set(state["shifted"]["texts"]), state["shifted"]["texts"]
    assert narrow["texts"], narrow
    assert all(right <= narrow["width"] - 8 for _, right in narrow["texts"]), narrow


@pytest.mark.skipif(not _browser_available(), reason="playwright browser not installed")
@pytest.mark.parametrize("bins", ["bin_width: 10, bin_start: 5", "bin_width: 10"])
def test_clearing_the_width_in_the_builder_drops_bin_start_too(tmp_path, bins):
    from playwright.sync_api import sync_playwright

    doc = (
        "title: H\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  - title: Spread\n"
        f"    chart: {{type: histogram, x: v, {bins}}}\n"
        "    sql: SELECT i::DOUBLE AS v FROM range(0, 100) t(i)\n"
    )
    (tmp_path / "h.yaml").write_text(doc)
    app = create_app(tmp_path, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    saves = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1300, "height": 1000})
            page.on(
                "response",
                lambda r: saves.append(r.status) if r.request.method == "PUT" else None,
            )
            page.goto(f"http://127.0.0.1:{port}/d/h/query?tile=spread", wait_until="load")
            page.wait_for_selector("#qb-type .seg-btn.active")
            page.locator("#run-btn").click()
            page.wait_for_function(
                "() => document.querySelector('#qb-preview .chart-mount')", timeout=20000
            )
            width = page.locator('#qb-encoding input[data-spec="bin_width"]')
            width.fill("")
            width.dispatch_event("change")
            mode = page.evaluate("() => document.querySelector('[data-bin-mode]').value")
            start = page.locator('#qb-encoding input[data-spec="bin_start"]')
            start_editable = start.count() > 0 and start.is_visible() and start.is_enabled()
            page.locator("#qb-add").click()
            deadline = time.monotonic() + 10
            while not saves:
                assert time.monotonic() < deadline, "the tile was never saved"
                page.wait_for_timeout(100)
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    assert mode == "auto"
    assert not start_editable
    assert saves == [200], saves
    assert "    chart: {type: histogram, x: v}\n" in (tmp_path / "h.yaml").read_text()
