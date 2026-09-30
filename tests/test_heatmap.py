"""Heatmap tiles: the `chart:` keys for cell aggregation and color, how lint
keeps them on heatmaps, how an edit writes them back, and what the browser
draws from real DuckDB results."""

import json
import time

import pytest
from pydantic import ValidationError

from sqldash.api.helpers import client_payload
from sqldash.lint import lint_project
from sqldash.models.chart import ChartSpec
from sqldash.project.store import DashboardStore
from sqldash.semantics import SemanticLayer
from sqldash.server import create_app
from sqldash.snapshot import _start_server


def lint_errors(root):
    store = DashboardStore(root)
    return [f.message for f in lint_project(store, SemanticLayer(store)) if f.level == "error"]


def test_heatmap_keys_parse():
    spec = ChartSpec.model_validate(
        {
            "type": "heatmap",
            "x": "hour",
            "y": "weekday",
            "value": "orders",
            "aggregate": "sum",
            "palette": "diverging",
            "midpoint": 0,
            "x_order": [9, 10, 11],
            "y_order": ["Mon", "Tue"],
        }
    )
    assert spec.y == ["weekday"]
    assert spec.x_order == [9, 10, 11]
    assert (spec.aggregate, spec.palette, spec.midpoint) == ("sum", "diverging", 0)


@pytest.mark.parametrize(
    ("chart", "message"),
    [
        ({"midpoint": 0}, "needs palette: diverging"),
        ({"palette": "sequential", "midpoint": 0}, "needs palette: diverging"),
        ({"aggregate": "median"}, "sum"),
        ({"palette": "rainbow"}, "sequential"),
        ({"palette": "diverging", "midpoint": float("nan")}, "finite"),
        ({"x_order": [float("nan")]}, "finite"),
        ({"y_order": ["Mon", float("inf")]}, "finite"),
    ],
)
def test_heatmap_keys_that_cannot_mean_anything_are_refused(chart, message):
    with pytest.raises(ValidationError, match=message):
        ChartSpec.model_validate({"type": "heatmap", **chart})


def test_lint_keeps_heatmap_keys_on_heatmaps(tmp_path):
    (tmp_path / "d.yaml").write_text(
        "title: D\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  - {title: A, chart: {type: bar, aggregate: sum}, sql: 'SELECT 1 AS n'}\n"
        "  - {title: B, chart: {type: line, y_order: [a]}, sql: 'SELECT 1 AS n'}\n"
        "  - {title: C, chart: {type: heatmap, y: [a, b]}, sql: 'SELECT 1 AS a, 2 AS b'}\n"
        "  - {title: E, chart: {type: heatmap, x: a, y: b, aggregate: count}, "
        "sql: 'SELECT 1 AS a, 2 AS b'}\n"
    )
    errors = lint_errors(tmp_path)
    assert any("aggregate is only valid on heatmap charts, not bar" in m for m in errors), errors
    assert any("y_order is only valid on heatmap charts, not line" in m for m in errors), errors
    assert any("tile 'c'" in m and "one y column, not 2" in m for m in errors), errors
    assert not any("tile 'e'" in m for m in errors), errors


def test_a_saved_heatmap_reloads_and_edits_one_key(tmp_path):
    doc = (
        "title: D\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  - title: Orders\n"
        "    sql: SELECT 'Mon' AS weekday, 9 AS hour, 3 AS orders\n"
        "    chart:\n"
        "      type: heatmap\n"
        "      x: hour\n"
        "      y: weekday\n"
        "      value: orders\n"
        "      y_order: [Mon, Tue, Wed]\n"
    )
    (tmp_path / "d.yaml").write_text(doc)
    store = DashboardStore(tmp_path)
    dashboard, _, etag = store.load("d")
    assert dashboard.tiles[0].chart.y == ["weekday"]
    payload = {
        "id": "orders",
        "title": "Orders",
        "query": "orders",
        "chart": {
            "type": "heatmap",
            "x": "hour",
            "y": "weekday",
            "value": "orders",
            "aggregate": "sum",
            "palette": None,
            "midpoint": None,
            "y_order": ["Mon", "Tue", "Wed"],
        },
    }
    store.upsert_tile("d", payload, "SELECT 'Mon' AS weekday, 9 AS hour, 3 AS orders", etag)
    text = (tmp_path / "d.yaml").read_text()
    assert text == doc + "      aggregate: sum\n", text
    reloaded, _, _ = store.load("d")
    assert reloaded.tiles[0].chart.aggregate == "sum"
    assert reloaded.tiles[0].chart.y_order == ["Mon", "Tue", "Wed"]


def _edit_chart(tmp_path, chart_yaml, change):
    doc = (
        "title: D\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  - title: T\n"
        "    sql: SELECT 1 AS k, 'a' AS r, 1 AS v\n"
        "    chart:\n" + chart_yaml
    )
    (tmp_path / "d.yaml").write_text(doc)
    store = DashboardStore(tmp_path)
    dashboard, _, etag = store.load("d")
    chart = json.loads(json.dumps(client_payload("d", dashboard, etag)))["dashboard"]["tiles"][0][
        "chart"
    ]
    chart.update(change)
    payload = {"id": "t", "title": "T", "query": "t", "chart": chart}
    store.upsert_tile("d", payload, "SELECT 1 AS k, 'a' AS r, 1 AS v", etag)
    return (tmp_path / "d.yaml").read_text().split("    chart:\n", 1)[1]


def test_order_integers_past_the_browser_limit_must_be_quoted(tmp_path):
    with pytest.raises(ValidationError, match="quote it as a string: '9007199254740993'"):
        ChartSpec.model_validate({"type": "heatmap", "x_order": [9007199254740993]})
    ChartSpec.model_validate({"type": "heatmap", "x_order": [9007199254740991, "9007199254740993"]})
    (tmp_path / "d.yaml").write_text(
        "title: D\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  - title: T\n"
        "    sql: SELECT 1 AS k, 'a' AS r, 1 AS v\n"
        "    chart: {type: heatmap, x: k, y: r, value: v, y_order: [-9_007_199_254_740_993]}\n"
    )
    assert any("quote it as a string" in e for e in lint_errors(tmp_path))


def test_changing_an_order_entry_from_number_to_text_is_saved(tmp_path):
    written = _edit_chart(
        tmp_path,
        "      type: heatmap\n      x: k\n      y: r\n      value: v\n      x_order: [1]\n",
        {"x_order": ["1"]},
    )
    assert "x_order: ['1']" in written, written


@pytest.mark.parametrize(
    ("before", "after", "expected"), [("[1]", [True], "[true]"), ("[true]", [1], "[1]")]
)
def test_order_edits_between_true_and_1_are_saved(tmp_path, before, after, expected):
    chart = "      type: heatmap\n      x: k\n      y: r\n      value: v\n"
    chart += f"      x_order: {before}\n"
    written = _edit_chart(tmp_path, chart, {"x_order": after})
    assert f"x_order: {expected}" in written, written


def test_styled_order_integers_and_either_y_spelling_keep_their_comments(tmp_path):
    chart = (
        "      type: heatmap\n"
        "      x: k\n"
        "      y: r  # rows\n"
        "      value: v\n"
        "      x_order:\n"
        "        - 0x1F  # hex\n"
        "        - 1_000  # grouped\n"
    )
    written = _edit_chart(tmp_path, chart, {"aggregate": "sum"})
    assert written == chart + "      aggregate: sum\n", written
    listed = chart.replace("      y: r  # rows\n", "      y:\n        - r  # rows\n")
    written = _edit_chart(tmp_path, listed, {"aggregate": "sum"})
    assert written == listed + "      aggregate: sum\n", written


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


LONG = "a region name long enough to need truncating on any axis"

HEATMAPS = f"""\
title: Heatmaps
source: {{type: duckdb, database: ':memory:'}}
tiles:
  - title: Weekday hour
    chart:
      type: heatmap
      x: hour
      y: weekday
      value: orders
      y_order: [Mon, Tue, Wed]
    sql: |
      SELECT * FROM (VALUES ('Tue', 9, 5), ('Mon', 9, 0), ('Mon', 10, -3), ('Wed', 10, 1000),
        ('Tue', 11, NULL)) t(weekday, hour, orders)
  - title: Region category
    chart: {{type: heatmap, x: category, y: region, value: amount, aggregate: sum,
      palette: diverging, midpoint: 10}}
    sql: |
      SELECT * FROM (VALUES ('us', 'toys', 4), ('us', 'toys', 6), ('eu', 'home', 30),
        ('{LONG}', 'toys', -2)) t(region, category, amount)
  - title: Duplicates
    chart: {{type: heatmap, x: category, y: region, value: amount}}
    sql: SELECT * FROM (VALUES ('us', 'toys', 4), ('us', 'toys', 6)) t(region, category, amount)
  - title: Many
    chart: {{type: heatmap, x: sku, y: store, aggregate: count}}
    sql: SELECT 'sku' || (i % 120) AS sku, 'store' || (i % 3) AS store FROM range(0, 1200) t(i)
  - title: Wide legend
    format: currency
    chart: {{type: heatmap, x: c, y: r, value: v}}
    sql: SELECT * FROM (VALUES ('a', 'x', 12700.5), ('b', 'x', 137800.25)) t(r, c, v)
  - title: Overflow
    chart: {{type: heatmap, x: c, y: r, value: v, aggregate: sum}}
    sql: SELECT * FROM (VALUES ('a', 'x', 1e308), ('a', 'x', 1e308)) t(r, c, v)
"""

READ_TEXT = """() => Object.fromEntries([...document.querySelectorAll('.tile')].map(tile => {
    const chart = echarts.getInstanceByDom(tile.querySelector('.chart-mount'));
    const o = chart.getOption();
    const shown = chart.getZr().storage.getDisplayList()
        .filter(e => e.type === 'tspan' && e.style?.text)
        .map(e => e.style.text);
    return [tile.dataset.tileId, {
        width: chart.getWidth(),
        yLabel: o.yAxis[0].axisLabel.width,
        legend: o.visualMap?.[0]?.itemHeight ?? null,
        shown,
    }];
}))"""

READ_TILES = """() => Object.fromEntries([...document.querySelectorAll('.tile')].map(tile => {
    const chart = echarts.getInstanceByDom(tile.querySelector('.chart-mount'));
    const o = chart.getOption();
    const cells = (series) => (series?.data ?? []).map(d => [
        o.xAxis[0].data[d.value[0]], o.yAxis[0].data[d.value[1]], d.value[2]]);
    const mount = tile.querySelector('.chart-mount').getBoundingClientRect();
    return [tile.dataset.tileId, {
        x: o.xAxis[0].data, y: o.yAxis[0].data,
        filled: cells(o.series[0]), missing: cells(o.series[1]),
        range: o.visualMap?.[0] && [o.visualMap[0].min, o.visualMap[0].max],
        scope: o.graphic?.[0]?.elements?.[0]?.style?.text ?? null,
        empty: tile.querySelector('.chart-empty')?.textContent ?? null,
        width: chart.getWidth(), mountWidth: Math.round(mount.width),
        scroll: document.documentElement.scrollWidth > window.innerWidth,
    }];
}))"""


@pytest.mark.skipif(not _browser_available(), reason="playwright browser not installed")
def test_heatmaps_render_real_duckdb_cells_in_both_themes_and_on_resize(tmp_path):
    from playwright.sync_api import sync_playwright

    (tmp_path / "d.yaml").write_text(HEATMAPS)
    app = create_app(tmp_path, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    states = {}
    texts = {}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            for theme in ("light", "dark"):
                context = browser.new_context(viewport={"width": 1300, "height": 1000})
                context.add_init_script(f"localStorage.setItem('sqldash-theme', '{theme}')")
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda e, errors=errors: errors.append(str(e)))
                page.goto(f"http://127.0.0.1:{port}/d/d", wait_until="load")
                deadline = time.monotonic() + 30
                while page.evaluate(
                    "() => document.querySelectorAll('.tile-status .skeleton').length"
                ):
                    assert time.monotonic() < deadline, "tiles never finished loading"
                    time.sleep(0.2)
                page.wait_for_timeout(500)
                states[theme] = page.evaluate(READ_TILES)
                page.set_viewport_size({"width": 700, "height": 1000})
                page.wait_for_timeout(800)
                states[f"{theme}-narrow"] = page.evaluate(READ_TILES)
                page.set_viewport_size({"width": 1300, "height": 1000})
                page.wait_for_timeout(800)
                texts[theme] = page.evaluate(READ_TEXT)
                page.set_viewport_size({"width": 390, "height": 1000})
                page.wait_for_timeout(800)
                texts[f"{theme}-390"] = page.evaluate(READ_TEXT)
                assert not errors, errors
                context.close()
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)

    for key, state in states.items():
        weekday = state["weekday_hour"]
        assert weekday["y"] == ["Mon", "Tue", "Wed"], key
        assert weekday["x"] == ["9", "10", "11"], key
        assert sorted(weekday["filled"]) == sorted(
            [["9", "Tue", 5], ["9", "Mon", 0], ["10", "Mon", -3], ["10", "Wed", 1000]]
        ), key
        assert sorted(c[:2] for c in weekday["missing"]) == sorted(
            [["11", "Mon"], ["11", "Tue"], ["11", "Wed"], ["9", "Wed"], ["10", "Tue"]]
        ), key
        assert weekday["range"] == [-3, 1000], key

        region = state["region_category"]
        assert sorted(region["filled"]) == sorted(
            [["toys", "us", 10], ["home", "eu", 30], ["toys", LONG, -2]]
        ), key
        assert region["range"] == [-10, 30], key

        assert state["duplicates"]["filled"] == [], key
        assert state["overflow"]["filled"] == [], key
        assert state["overflow"]["empty"] == "Some cells add up to more than a number can hold", key
        assert state["duplicates"]["empty"].startswith("1 cell has more than one row"), key

        many = state["many"]
        assert len(many["x"]) == 60, key
        assert many["scope"] == "60 of 120 sku values", key
        assert all(c[2] == 10 for c in many["filled"]), key
        assert many["width"] == many["mountWidth"], key

    for key, tiles in texts.items():
        region = tiles["region_category"]
        long = [t for t in region["shown"] if t.startswith("a region")]
        assert len(long) == 1, (key, long)
        assert long[0].endswith("…"), (key, long)
        if key.endswith("390"):
            assert region["width"] < 260, (key, region["width"])
            assert region["yLabel"] <= 64, (key, region)
            assert region["legend"] <= 40, (key, region)
        else:
            assert region["yLabel"] == 180, (key, region)
            assert len(long[0]) > 25, (key, region)


@pytest.mark.skipif(not _browser_available(), reason="playwright browser not installed")
def test_switching_to_heatmap_before_running_saves_one_y(tmp_path):
    from playwright.sync_api import sync_playwright

    (tmp_path / "d.yaml").write_text(
        "title: D\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  - title: Lines\n"
        "    chart: {type: line, x: day, y: [hour, orders]}\n"
        "    sql: SELECT 'mon' AS day, 9 AS hour, 3 AS orders\n"
    )
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
            page.goto(f"http://127.0.0.1:{port}/d/d/query?tile=lines", wait_until="load")
            page.wait_for_selector("#qb-type .seg-btn.active")
            page.locator("#qb-type .seg-btn", has_text="Heatmap").click()
            page.locator("#qb-add").click()
            deadline = time.monotonic() + 10
            while not saves:
                assert time.monotonic() < deadline, "the tile was never saved"
                page.wait_for_timeout(100)
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    assert saves == [200], saves
    text = (tmp_path / "d.yaml").read_text()
    assert "type: heatmap" in text, text
    assert "hour, orders" not in text, text
    assert not [e for e in lint_errors(tmp_path) if "one y column" in e]


@pytest.mark.skipif(not _browser_available(), reason="playwright browser not installed")
def test_clicking_a_heatmap_cell_changes_no_filter(tmp_path):
    from playwright.sync_api import sync_playwright

    (tmp_path / "d.yaml").write_text(
        "title: D\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "filters:\n"
        "  - {name: x, type: select, options: [a, 'a × b']}\n"
        "tiles:\n"
        "  - title: Cells\n"
        "    chart: {type: heatmap, x: x, y: y, value: v}\n"
        "    sql: SELECT * FROM (VALUES ('a', 'b', 10), ('a × b', 'c', 20)) t(x, y, v)\n"
    )
    app = create_app(tmp_path, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1300, "height": 1000})
            page.goto(f"http://127.0.0.1:{port}/d/d", wait_until="load")
            deadline = time.monotonic() + 30
            while page.evaluate("() => document.querySelectorAll('.tile-status .skeleton').length"):
                assert time.monotonic() < deadline, "tiles never finished loading"
                time.sleep(0.2)
            page.wait_for_timeout(500)
            select = "() => document.querySelector('.filter-bar select[data-filter=\"x\"]').value"
            before = page.evaluate(select)
            point = page.evaluate("""() => {
                const mount = document.querySelector('.tile .chart-mount');
                const chart = echarts.getInstanceByDom(mount);
                const [x, y] = chart.convertToPixel({seriesIndex: 0}, [0, 0]);
                const box = mount.getBoundingClientRect();
                return [box.left + x, box.top + y];
            }""")
            page.mouse.click(*point)
            page.wait_for_timeout(500)
            after = page.evaluate(select)
            url = page.url
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    assert before == after, (before, after)
    assert "x=" not in url, url
