"""Browser smoke tests: the UI behaviors unit tests cannot see — chart
rendering, cross-filter clicks, table sorting/shrinking, the settings modal.
Every scenario here regressed at least once during development. Skips cleanly
when no chromium is installed; CI installs one."""

import asyncio
import json
import re
import subprocess
import sys
import threading
import time
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import duckdb
import pytest

from sqldash.api import routes_studio
from sqldash.period import compare_window
from sqldash.project.store import DashboardStore
from sqldash.scaffold import create_demo
from sqldash.server import create_app
from sqldash.snapshot import _start_server
from sqldash.studio.entrypoints import AgentEntrypoint, save_entrypoint


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


pytestmark = pytest.mark.skipif(not _browser_available(), reason="playwright browser not installed")


def _stop_server(server, thread, page=None):
    try:
        if page is not None and not page.is_closed():
            page.goto("about:blank")
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive(), "embedded server did not stop after browser disconnected"


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    root = tmp_path_factory.mktemp("smoke")
    create_demo(root)
    with open(root / ".sqldash" / "demo.yaml", "a") as f:
        f.write(
            "\n  - title: Tiny table\n"
            "    chart: table\n"
            "    size: 12x6\n"
            '    sql: "SELECT c AS segment, v AS total'
            " FROM (VALUES ('north', 10), ('south', 20)) t(c, v)\"\n"
        )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    yield f"http://127.0.0.1:{port}"
    _stop_server(server, thread)


@pytest.fixture(scope="module")
def page(served):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(timeout=30_000)
        context = browser.new_context(viewport={"width": 1400, "height": 1000}, color_scheme="dark")
        page = context.new_page()
        page.set_default_timeout(30_000)
        page.set_default_navigation_timeout(30_000)
        page.on("console", lambda m: _events.append(f"console[{m.type}] {m.text}"))
        page.on("pageerror", lambda e: _events.append(f"pageerror {e}"))
        page.on(
            "requestfailed",
            lambda r: _events.append(f"requestfailed {r.url} {r.failure}"),
        )
        page.on(
            "response",
            lambda r: (
                _events.append(f"http {r.status} {r.url}")
                if r.status >= 300 or "/static/" not in r.url
                else None
            ),
        )
        yield page
        browser.close()


_events: list[str] = []


def _dump_page(page, label):
    tail = "\n".join(_events[-30:])
    body = page.content()[:2000]
    pytest.fail(f"{label}\nurl={page.url}\nrecent events:\n{tail}\npage content head:\n{body}")


def _wait_tiles(page, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pending = page.evaluate("() => document.querySelectorAll('.tile-status .skeleton').length")
        if pending == 0:
            return
        time.sleep(0.2)
    pytest.fail("tiles never finished loading")


def _ensure_dashboard(page, served):
    if "/d/demo" not in page.url:
        page.goto(f"{served}/d/demo", wait_until="load")
        _wait_tiles(page)


def test_dashboard_renders_everything(page, served):
    page.goto(f"{served}/d/demo", wait_until="load")
    _wait_tiles(page)
    state = page.evaluate("""() => ({
        errors: document.querySelectorAll('.tile-status .err').length,
        canvases: document.querySelectorAll('.chart-mount canvas').length,
        big: document.querySelector('.big-number .value')?.textContent ?? '',
        hues: [...document.querySelectorAll('.tile[data-tile-id]')]
            .filter(el => el.style.getPropertyValue('--wcolor')).length,
    })""")
    assert state["errors"] == 0
    assert state["canvases"] >= 3
    assert state["big"].startswith("$")
    assert state["hues"] >= 5


def test_light_mode_does_not_borrow_dark_elevation(page, served):
    """Light used glow, glass blur, and the same shadow geometry as dark —
    which reads as grime on paper. Technique is now per-theme.

    Also: zero-elevation tokens must be composable. `--shadow-*: none` inside
    `box-shadow: ring, var(--shadow-3)` is invalid CSS and drops the ring —
    dragging a tile in light then had no accent outline at all."""
    _ensure_dashboard(page, served)
    page.evaluate(
        """() => {
          document.documentElement.dataset.theme = "light";
          window.dispatchEvent(new CustomEvent("sqldash:themechange"));
        }"""
    )
    try:
        measured = page.evaluate(
            """() => {
              const tile = document.querySelector(".tile:not(.tile-text)");
              const topbar = document.querySelector(".topbar");
              const body = getComputedStyle(document.body);
              const ts = getComputedStyle(tile);
              const tb = getComputedStyle(topbar);
              // Force the drag state without a real mouse drag: the ring is
              // `0 0 0 1.5px var(--accent), var(--shadow-3)`. If --shadow-3
              // is the keyword none, the whole declaration is invalid.
              // Kill the 150ms box-shadow transition so getComputedStyle is
              // the end state, not an interpolated transparent mid-frame.
              tile.style.transition = "none";
              document.body.classList.add("editing");
              const item = tile.closest(".grid-stack-item");
              item.classList.add("ui-draggable-dragging");
              void tile.offsetHeight;
              const dragShadow = getComputedStyle(tile).boxShadow;
              item.classList.remove("ui-draggable-dragging");
              document.body.classList.remove("editing");
              tile.style.transition = "";
              const primary = document.querySelector(".btn-primary");
              const primaryShadow = primary
                ? getComputedStyle(primary).boxShadow
                : null;
              return {
                pageGlow: body.backgroundImage,
                tileBlur: ts.backdropFilter || ts.webkitBackdropFilter,
                topbarBlur: tb.backdropFilter || tb.webkitBackdropFilter,
                dragShadow,
                primaryShadow,
              };
            }"""
        )
        assert measured["pageGlow"] in ("none", ""), measured
        assert "blur" not in (measured["tileBlur"] or "none").lower(), measured
        assert "blur" not in (measured["topbarBlur"] or "none").lower(), measured
        assert measured["dragShadow"] not in (None, "", "none"), measured
        # accent ring must survive composition with the zero-elevation token
        assert "1.5px" in measured["dragShadow"], measured
        if measured["primaryShadow"] is not None:
            assert measured["primaryShadow"] not in ("", "none"), measured
            assert "inset" in measured["primaryShadow"], measured
    finally:
        page.evaluate(
            """() => {
              document.documentElement.dataset.theme = "dark";
              window.dispatchEvent(new CustomEvent("sqldash:themechange"));
            }"""
        )


def test_dark_tiles_keep_tinted_chrome(page, served):
    """--tile-wash/rim/border that reference --wcolor must resolve on .tile,
    not :root — otherwise dark cards go transparent with currentColor borders.
    Light rim must also resolve per tile (not always accent)."""
    _ensure_dashboard(page, served)
    page.evaluate(
        """() => {
          document.documentElement.dataset.theme = "dark";
          window.dispatchEvent(new CustomEvent("sqldash:themechange"));
        }"""
    )
    try:
        measured = page.evaluate(
            """() => {
              const tile = document.querySelector(".tile:not(.tile-text)");
              const ts = getComputedStyle(tile);
              const bg = ts.backgroundColor;
              const border = ts.borderTopColor;
              const wash = ts.getPropertyValue("--tile-wash").trim();
              const ink = getComputedStyle(document.documentElement)
                .getPropertyValue("--ink-1").trim();
              return {bg, border, wash, ink};
            }"""
        )
        assert measured["wash"], measured  # non-empty — token computed valid
        assert measured["bg"] not in ("", "rgba(0, 0, 0, 0)", "transparent"), measured
        # border must be the tinted mix, not currentColor fallback (= --ink-1)
        assert measured["border"] != measured["ink"], measured
        assert "rgb" in measured["border"] or "color" in measured["border"], measured

        # light: two tiles with different --wcolor must not share one accent rim
        rims = page.evaluate(
            """() => {
              document.documentElement.dataset.theme = "light";
              window.dispatchEvent(new CustomEvent("sqldash:themechange"));
              const tiles = [...document.querySelectorAll(".tile:not(.tile-text)")].slice(0, 2);
              const hues = ["#4a3aa7", "#1baf7a"];
              return tiles.map((t, i) => {
                t.style.setProperty("--wcolor", hues[i]);
                return getComputedStyle(t).getPropertyValue("--tile-rim").trim();
              });
            }"""
        )
        assert len(rims) == 2, rims
        assert rims[0] != rims[1], rims
    finally:
        page.evaluate(
            """() => {
              document.documentElement.dataset.theme = "dark";
              window.dispatchEvent(new CustomEvent("sqldash:themechange"));
              for (const t of document.querySelectorAll(".tile")) {
                t.style.removeProperty("--wcolor");
              }
            }"""
        )


def test_compare_delta_renders(page, served):
    _ensure_dashboard(page, served)
    delta = page.locator(".bn-delta")
    assert delta.count() == 1
    assert "vs previous period" in delta.first.text_content()


def test_compare_without_time_dimension_does_not_draw_a_zero_delta(page, tmp_path_factory):
    """Lint errors; the tile used to still show ▲ 0.0% vs previous period. #280."""
    root = tmp_path_factory.mktemp("notime")
    (root / ".sqldash").mkdir()
    (root / ".sqldash" / "data").mkdir()
    (root / ".sqldash" / "data" / "orders.csv").write_text(
        "order_date,region,amount\n2026-08-01,us,10\n2026-08-02,us,20\n"
    )
    (root / ".sqldash" / "metrics.yaml").write_text(
        "source: {type: duckdb, attach_files: true}\n"
        "relations:\n  orders: {table: orders}\n"
        "metrics:\n  n_orders: {title: Orders, relation: orders, expr: COUNT(*), format: compact}\n"
    )
    (root / ".sqldash" / "cmp.yaml").write_text(
        "title: CMP\n"
        "source: {type: duckdb, attach_files: true}\n"
        "filters:\n"
        "  - {name: dates, type: daterange, label: Date range, default: last_60_days}\n"
        "tiles:\n"
        "  - title: Orders with compare\n"
        "    metric: n_orders\n"
        "    compare: previous_period\n"
        "    size: 6x2\n"
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/cmp", wait_until="load")
        page.wait_for_selector(".big-number .value", timeout=30_000)
        assert page.locator(".bn-delta").count() == 0
    finally:
        _stop_server(server, thread, page)


def _compare_dashboard(root, monkeypatch, filters=""):
    create_demo(root)
    (root / ".sqldash" / "cmp.yaml").write_text(
        "title: CMP\n"
        "source: {type: duckdb, attach_files: true}\n"
        f"{filters}"
        "tiles:\n"
        "  - title: Revenue compare\n"
        "    metric: {name: revenue, compare: previous_period}\n"
        "    size: 6x2\n"
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    monkeypatch.setattr(app.state.watcher, "start", lambda _loop: None)
    return _start_server(app)


def test_compare_without_a_daterange_filter_is_a_tile_error(page, tmp_path_factory, monkeypatch):
    """The tile used to show a confident number with no delta and no hint the
    compare was dropped, while lint and every headless surface errored. #516."""
    server, thread, port = _compare_dashboard(tmp_path_factory.mktemp("nodaterange"), monkeypatch)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/cmp", wait_until="load")
        err = page.locator(".tile .tile-status .err")
        err.wait_for(timeout=30_000)
        assert err.text_content() == (
            "compare 'previous_period' needs a time range but the dashboard has no "
            "daterange filter — add one, or omit compare"
        )
        assert page.locator(".big-number .value").count() == 0
        assert page.locator(".bn-delta").count() == 0
    finally:
        _stop_server(server, thread, page)


def test_compare_with_no_range_picked_errors_until_one_is(page, tmp_path_factory, monkeypatch):
    server, thread, port = _compare_dashboard(
        tmp_path_factory.mktemp("nodefault"),
        monkeypatch,
        "filters:\n  - {name: dates, type: daterange, label: Date range}\n",
    )
    try:
        page.goto(f"http://127.0.0.1:{port}/d/cmp", wait_until="load")
        err = page.locator(".tile .tile-status .err")
        err.wait_for(timeout=30_000)
        assert "pick a start and end date" in err.text_content()
        start_input, end_input = page.locator(".dr-date").all()
        today = date.today()
        start_input.fill((today - timedelta(days=59)).isoformat())
        end_input.fill(today.isoformat())
        delta = page.locator(".bn-delta")
        delta.wait_for(timeout=30_000)
        assert "vs previous period" in delta.text_content()
        assert page.locator(".tile .tile-status .err").count() == 0
        end_input.fill("")
        err.wait_for(timeout=30_000)
        assert "pick a start and end date" in err.text_content()
        assert page.locator(".big-number .value").count() == 0
        assert page.locator(".bn-delta").count() == 0
    finally:
        _stop_server(server, thread, page)


def test_a_failed_prior_window_run_is_a_tile_error(page, tmp_path_factory, monkeypatch):
    """A failing prior-window run used to be swallowed into "no delta"."""
    server, thread, port = _compare_dashboard(
        tmp_path_factory.mktemp("prevfail"),
        monkeypatch,
        "filters:\n  - {name: dates, type: daterange, default: last_60_days}\n",
    )
    seen = []

    def fail_prior_window(route):
        body = json.loads(route.request.post_data or "{}")
        if body.get("metric") and not seen:
            seen.append(body)
            route.fulfill(
                status=422,
                content_type="application/json",
                body=json.dumps({"detail": "prior window exploded"}),
            )
        else:
            route.continue_()

    page.route("**/api/run", fail_prior_window)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/cmp", wait_until="load")
        err = page.locator(".tile .tile-status .err")
        err.wait_for(timeout=30_000)
        assert err.text_content() == "prior window exploded"
        assert len(seen) == 1
        assert seen[0]["params"]["dates_end"] < date.today().isoformat()
        assert page.locator(".bn-delta").count() == 0
    finally:
        page.unroute("**/api/run", fail_prior_window)
        _stop_server(server, thread, page)


def test_an_inverted_range_on_a_compare_tile_names_the_dates_the_user_entered(
    page, tmp_path_factory, monkeypatch
):
    """Both windows are refused, and the compare run's 422 used to be the one shown:
    it quotes the previous-period window runner.js derived (2026-10-01..2026-08-31),
    which appears nowhere in the UI, while sibling tiles quote what was typed."""
    server, thread, port = _compare_dashboard(
        tmp_path_factory.mktemp("inverted"),
        monkeypatch,
        "filters:\n  - {name: dates, type: daterange, default: last_60_days}\n",
    )
    try:
        query = "f_dates_start=2026-09-01&f_dates_end=2026-08-01"
        page.goto(f"http://127.0.0.1:{port}/d/cmp?{query}", wait_until="load")
        err = page.locator(".tile .tile-status .err")
        err.wait_for(timeout=30_000)
        assert "dates_start '2026-09-01' is after dates_end '2026-08-01'" in err.text_content()
        assert "2026-10-01" not in err.text_content()
        assert page.locator(".bn-delta").count() == 0
    finally:
        _stop_server(server, thread, page)


def test_cross_filter_click_sets_and_clears(page, served):
    _ensure_dashboard(page, served)
    tile = page.locator('.tile[data-tile-id="revenue_share_by_region"]')
    tile.scroll_into_view_if_needed()
    page.wait_for_function(
        """() => {
            const mount = document.querySelector(
                '.tile[data-tile-id="revenue_share_by_region"] .chart-mount');
            return Boolean(mount && echarts.getInstanceByDom(mount));
        }""",
        timeout=15_000,
    )
    sel = '.filter-bar select[data-filter="region"]'

    def slice_point():
        return page.evaluate("""() => {
            const mount = document.querySelector(
                '.tile[data-tile-id="revenue_share_by_region"] .chart-mount');
            const chart = echarts.getInstanceByDom(mount);
            if (!chart) return null;
            for (let fx = 0.1; fx < 0.95; fx += 0.05)
                for (let fy = 0.1; fy < 0.95; fy += 0.05) {
                    const x = chart.getWidth() * fx, y = chart.getHeight() * fy;
                    if (chart.containPixel({seriesIndex: 0}, [x, y])) {
                        const r = mount.getBoundingClientRect();
                        return {x: r.x + x, y: r.y + y};
                    }
                }
            return null;
        }""")

    point = slice_point()
    assert point, "no clickable slice found"
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        page.mouse.click(point["x"], point["y"])
        try:
            page.wait_for_function(
                f"() => document.querySelector('{sel}').value !== 'all'",
                timeout=2_000,
            )
            break
        except Exception:
            point = slice_point() or point
    else:
        pytest.fail("pie click never set the region filter")
    value = page.eval_on_selector(sel, "el => el.value")
    assert value in ("us", "eu", "apac")
    page.wait_for_function(f"() => location.search.includes('f_region={value}')", timeout=5000)
    _wait_tiles(page)
    page.mouse.click(point["x"], point["y"])
    page.wait_for_function(f"() => document.querySelector('{sel}').value === 'all'", timeout=5000)
    _wait_tiles(page)
    deadline = time.monotonic() + 15
    previous = None
    while time.monotonic() < deadline:
        snapshot = page.evaluate(
            """() => document
                .querySelector('.tile[data-tile-id="recent_orders"] tbody')
                ?.rows.length ?? -1"""
        )
        if snapshot == previous and snapshot > 0:
            break
        previous = snapshot
        time.sleep(0.4)


def test_options_sql_populates_select(page, served):
    _ensure_dashboard(page, served)
    options = page.eval_on_selector(
        '.filter-bar select[data-filter="region"]',
        "el => [...el.options].map(o => o.value)",
    )
    assert options == ["all", "apac", "eu", "us"]


def test_if_only_filter_re_runs_the_tile(page, tmp_path_factory):
    """#217: a param used only in `{% if %}` did not mark the tile dirty, so the
    browser kept the off-state rows while CLI/API honored the toggle."""
    root = tmp_path_factory.mktemp("ifonly")
    (root / "d.yaml").write_text(
        "title: F\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "filters:\n"
        "  - {name: region, type: select, default: all, options: [us, eu, apac]}\n"
        "queries:\n"
        '  q: "SELECT 1 AS n {% if region %}WHERE 1 = 2{% endif %}"\n'
        "tiles:\n"
        "  - {query: q, chart: table}\n"
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    tab = page.context.new_page()
    tab.set_default_timeout(30_000)
    tab.set_default_navigation_timeout(30_000)
    try:
        tab.goto(f"http://127.0.0.1:{port}/d/d", wait_until="load")
        tab.wait_for_selector(".tile", timeout=30_000)
        tab.wait_for_selector("table.results tbody tr", timeout=30_000)
        assert tab.locator("table.results tbody tr").count() == 1
        with tab.expect_response(
            lambda r: "/api/run" in r.url and r.request.method == "POST",
            timeout=10_000,
        ) as run:
            tab.locator('select[data-filter="region"]').select_option("us")
        assert run.value.status < 400, run.value.status
        tab.wait_for_function(
            "() => document.querySelectorAll('table.results tbody tr').length === 0",
            timeout=15_000,
        )
    finally:
        tab.close()
        _stop_server(server, thread, page)


def test_elif_only_filter_re_runs_the_tile(page, tmp_path_factory):
    """#671: `paramNamesIn` walked `{% if %}` but not `{% elif %}`, so an elif-only
    filter never re-ran its tile and was never sent — the bar showed the selection
    while the server bound the default."""
    root = tmp_path_factory.mktemp("elifonly")
    (root / "d.yaml").write_text(
        "title: F\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "filters:\n"
        "  - {name: region, type: select, default: all, options: [us, eu]}\n"
        "  - {name: vip_only, type: select, default: all, options: ['yes']}\n"
        "queries:\n"
        '  q: "SELECT 1 AS n {% if region %}WHERE 1 = 2'
        '{% elif vip_only %}WHERE 1 = 3{% endif %}"\n'
        "tiles:\n"
        "  - {query: q, chart: table}\n"
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    tab = page.context.new_page()
    tab.set_default_timeout(30_000)
    tab.set_default_navigation_timeout(30_000)
    try:
        tab.goto(f"http://127.0.0.1:{port}/d/d", wait_until="load")
        tab.wait_for_selector("table.results tbody tr", timeout=30_000)
        assert tab.locator("table.results tbody tr").count() == 1
        with tab.expect_request(
            lambda r: "/api/run" in r.url and r.method == "POST",
            timeout=10_000,
        ) as run:
            tab.locator('select[data-filter="vip_only"]').select_option("yes")
        assert json.loads(run.value.post_data)["params"].get("vip_only") == "yes"
        tab.wait_for_function(
            "() => document.querySelectorAll('table.results tbody tr').length === 0",
            timeout=15_000,
        )
    finally:
        tab.close()
        _stop_server(server, thread, page)


def test_filter_named_only_in_a_comment_neither_re_runs_nor_posts(page, tmp_path_factory):
    """#683: `extract_params` reads comment-blanked SQL and `paramNamesIn` did not, so
    a filter named only in a `--` comment re-ran its tile and posted a value the
    server then ignored."""
    root = tmp_path_factory.mktemp("commentonly")
    (root / "d.yaml").write_text(
        "title: F\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "filters:\n"
        "  - {name: region, type: select, default: all, options: [us, eu]}\n"
        "  - {name: ghost, type: select, default: all, options: ['yes']}\n"
        "queries:\n"
        '  q: "SELECT 1 AS n -- {{ ghost }}\\n{% if region %}WHERE 1 = 2{% endif %}"\n'
        "tiles:\n"
        "  - {query: q, chart: table}\n"
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    tab = page.context.new_page()
    tab.set_default_timeout(30_000)
    tab.set_default_navigation_timeout(30_000)
    runs: list[dict] = []
    try:
        tab.goto(f"http://127.0.0.1:{port}/d/d", wait_until="load")
        tab.wait_for_selector("table.results tbody tr", timeout=30_000)
        tab.on(
            "request",
            lambda r: (
                runs.append(json.loads(r.post_data))
                if "/api/run" in r.url and r.method == "POST"
                else None
            ),
        )
        tab.locator('select[data-filter="ghost"]').select_option("yes")
        with tab.expect_request(
            lambda r: "/api/run" in r.url and r.method == "POST",
            timeout=10_000,
        ):
            tab.locator('select[data-filter="region"]').select_option("us")
        tab.wait_for_function(
            "() => document.querySelectorAll('table.results tbody tr').length === 0",
            timeout=15_000,
        )
        assert [run["params"] for run in runs] == [{"region": "us"}]
    finally:
        tab.close()
        _stop_server(server, thread, page)


def _click_header_until(page, table, aria_state):
    for _ in range(5):
        try:
            table.locator("th", has_text="Amount").click(timeout=3000)
            page.wait_for_function(
                "() => document.querySelector("
                f'\'.tile[data-tile-id="recent_orders"] th[aria-sort="{aria_state}"]\''
                ") !== null",
                timeout=2000,
            )
            return True
        except Exception:
            time.sleep(0.5)
    return False


def test_table_sorts_on_header_click(page, served):
    _ensure_dashboard(page, served)
    table = page.locator('.tile[data-tile-id="recent_orders"] table.results')
    assert _click_header_until(page, table, "ascending"), "never sorted asc (re-render race?)"
    first_asc = float(table.locator("tbody tr").first.locator("td").last.inner_text())
    assert _click_header_until(page, table, "descending"), "never sorted desc (re-render race?)"
    first_desc = float(table.locator("tbody tr").first.locator("td").last.inner_text())
    assert first_desc >= first_asc


def test_small_table_tile_shrinks(page, served):
    _ensure_dashboard(page, served)
    state = page.evaluate("""() => {
        const item = document.querySelector('.tile[data-tile-id="tiny_table"]')
            .closest('.grid-stack-item');
        return {authored: item.dataset.authorH, now: Number(item.getAttribute('gs-h'))};
    }""")
    assert state["authored"] == "6"
    assert state["now"] < 6


def test_settings_modal_opens_and_fits(page, served):
    _ensure_dashboard(page, served)
    page.click("#settings-open")
    try:
        page.wait_for_selector("#settings-content .metric-panel", timeout=20000)
    except Exception:
        probe = page.evaluate("""() => ({
            inner: document.getElementById('settings-content')?.innerHTML.slice(0, 300),
            modalHidden: document.getElementById('settings-modal')?.hidden,
            settingsJs: performance.getEntriesByType('resource')
                .filter(e => e.name.includes('settings')).map(e => e.name),
            scripts: [...document.querySelectorAll('script[src]')].map(s => s.src),
        })""")
        page.keyboard.press("Escape")
        _dump_page(page, f"settings panel never rendered; probe: {probe!r}")
    metrics = page.evaluate("""() => {
        const m = document.getElementById('settings-content');
        return {overflow: m.scrollWidth > m.clientWidth,
                panels: m.querySelectorAll('.metric-panel').length};
    }""")
    assert metrics["overflow"] is False
    assert metrics["panels"] == 2
    page.keyboard.press("Escape")


def test_landing_search_filters_rows(page, served):
    page.keyboard.press("Escape")
    response = page.goto(f"{served}/", wait_until="domcontentloaded")
    try:
        page.wait_for_selector("#browser-filter", state="visible", timeout=15000)
    except Exception:
        nav = None if response is None else f"{response.status} {response.url}"
        _dump_page(page, f"landing search box never became visible; goto -> {nav}")
    total = page.locator("#dash-browser .drow").count()
    page.fill("#browser-filter", "zzz-no-match")
    page.wait_for_function(
        "() => [...document.querySelectorAll('#dash-browser .drow')].every(r => r.hidden)"
    )
    page.fill("#browser-filter", "order")
    page.wait_for_function(
        "() => [...document.querySelectorAll('#dash-browser .drow')].some(r => !r.hidden)"
    )
    assert total >= 1


def test_a_long_ambiguity_label_does_not_paint_over_the_source_column(page, tmp_path_factory):
    """`.row-tiles` was sized for "inline · name" and only had nowrap. A
    repo-qualified ambiguity is three times longer, so it ran out of its grid
    cell and painted across the source column. The full list stays in the
    title attribute, so clipping loses nothing.
    """
    root = tmp_path_factory.mktemp("ws103")
    (root / "repo1").mkdir()
    for n in ("dash_a", "dash_b"):
        (root / "repo1" / f"{n}.yaml").write_text(
            f"title: Dash {n}\n"
            "source: {type: duckdb, database: ':memory:'}\n"
            "metrics:\n  clash_total: {sql: 'SELECT 1 AS v', expr: 'SUM(v)'}\n"
            "tiles: []\n"
        )
    app = create_app(None, workspace=[("repo1", root / "repo1")], allowed_hosts=["127.0.0.1"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
        page.wait_for_selector(".row-warn", timeout=30_000)
        box = page.evaluate("""() => {
            const warn = document.querySelector('.row-warn');
            const cell = warn.closest('.row-tiles');
            const source = cell.parentElement.querySelector('.row-source');
            const clipped = getComputedStyle(cell).overflow === 'hidden';
            const warnRight = warn.getBoundingClientRect().right;
            const cellRight = cell.getBoundingClientRect().right;
            return {
                painted: clipped ? Math.min(warnRight, cellRight) : warnRight,
                sourceLeft: source.getBoundingClientRect().left,
                overflows: cell.scrollWidth > cell.clientWidth,
                title: warn.getAttribute('title'),
            };
        }""")
        assert box["overflows"], "fixture is too short to exercise the overflow"
        assert box["painted"] <= box["sourceLeft"], box
        assert "dash_a" in box["title"], box
        assert "dash_b" in box["title"], box
    finally:
        _stop_server(server, thread, page)


def test_the_ambiguity_survives_the_narrow_breakpoint(page, tmp_path_factory):
    """The marker lives in `.row-tiles`, which the 900px rule hides. Without a
    second signal a narrow screen showed an ordinary-looking row that silently
    did nothing when tapped — no hint, and a dead affordance.
    """
    root = tmp_path_factory.mktemp("narrow")
    for n in ("amb_a", "amb_b"):
        (root / f"{n}.yaml").write_text(
            f"title: Dash {n}\n"
            "source: {type: duckdb, database: ':memory:'}\n"
            "metrics:\n  clash_total: {sql: 'SELECT 1 AS v', expr: 'SUM(v)'}\n"
            "tiles: []\n"
        )
    (root / "solo.yaml").write_text(
        "title: Solo\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "metrics:\n  only_here: {sql: 'SELECT 1 AS v', expr: 'SUM(v)'}\n"
        "tiles: []\n"
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.set_viewport_size({"width": 700, "height": 900})
        page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
        page.wait_for_selector(".drow-inert", timeout=30_000)
        state = page.evaluate("""() => {
            const inert = document.querySelector('.drow-inert');
            const normal = document.querySelector('.drow:not(.drow-inert)');
            const warn = inert.querySelector('.row-warn');
            return {
                markerHidden: warn.getBoundingClientRect().height === 0,
                inertTitle: getComputedStyle(inert.querySelector('.row-title')).color,
                normalTitle: getComputedStyle(normal.querySelector('.row-title')).color,
            };
        }""")
        assert state["markerHidden"], "fixture no longer exercises the breakpoint"
        assert state["inertTitle"] != state["normalTitle"], state
    finally:
        page.set_viewport_size({"width": 1400, "height": 1000})
        _stop_server(server, thread, page)


@pytest.fixture
def deletes_served(tmp_path_factory, page):
    """Its own dashboard and server: this test deletes tiles, and the module
    `served` fixture's file is shared with every other test here."""
    root = tmp_path_factory.mktemp("deletes")
    (root / "d.yaml").write_text(
        "title: Deletes\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        # Pinned geometry: the drag below is expressed in pixels, so it needs
        # tiles that are narrower than the grid and tall enough to grab. A text
        # tile's default is a full-width one-row band — nowhere to drag to.
        "  - {markdown: FIRST, size: 6x4}\n"
        "  - {markdown: SECOND, size: 6x4}\n"
        "  - {markdown: THIRD, size: 6x4}\n"
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    yield f"http://127.0.0.1:{port}", root
    _stop_server(server, thread, page)


def test_two_deletes_in_a_row_remove_the_tiles_the_user_clicked(page, deletes_served):
    """#93. Derived ids are positional, so deleting a tile renumbers the rest.
    The browser kept the old ids in `dashboard.tiles` and in `data-tile-id`, and
    a stale id still resolves server-side — so the second delete succeeded on the
    wrong tile, destroying data the user never asked to remove.
    """
    base, root = deletes_served
    page.on("dialog", lambda d: d.accept())
    page.goto(f"{base}/d/d?edit=1")
    page.wait_for_selector(".tile", timeout=30_000)

    for label in ("FIRST", "SECOND"):
        tile = page.locator(".tile", has_text=label)
        tile.hover()
        # Wait on the request and the element, not on a word leaving innerText.
        # The text check could not tell a slow render from a click that missed —
        # and since a delete settles the survivors upward, a hovered button can
        # move out from under the cursor. This fails at the click if that
        # happens, instead of fifteen seconds later somewhere else.
        with page.expect_response(
            lambda r: "/tiles/" in r.url and r.request.method == "DELETE",
            timeout=15_000,
        ) as deletion:
            tile.locator('.wa-btn[data-action="delete"]').click()
        assert deletion.value.status < 400, deletion.value.status
        tile.wait_for(state="detached", timeout=15_000)

    # DOM updates before the trailing positions PATCH finishes. Wait for the
    # file, not the toast: a mid-write read used to see '' and fail on CI.
    remaining = ""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        remaining = (root / "d.yaml").read_text()
        if "THIRD" in remaining and "FIRST" not in remaining and "SECOND" not in remaining:
            break
        time.sleep(0.05)
    assert "THIRD" in remaining, remaining
    assert "FIRST" not in remaining, remaining
    assert "SECOND" not in remaining, remaining


def test_dragging_after_a_delete_saves_the_layout(page, deletes_served):
    """#93, second half. GridStack keys nodes by `gs-id` and the positions PATCH
    sends `grid.engine.nodes` ids, so a resync that fixes only the tiles array
    and `data-tile-id` still leaves the grid holding pre-delete ids. The drag
    then 422s on ids the server no longer has — and the tile has already moved
    on screen, so nothing looks wrong until reload.
    """
    base, root = deletes_served
    position_calls = []
    page.on("dialog", lambda d: d.accept())
    page.on(
        "response",
        lambda r: position_calls.append((r.status, r.url)) if "/positions" in r.url else None,
    )
    page.goto(f"{base}/d/d?edit=1")
    page.wait_for_selector(".tile", timeout=30_000)

    tile = page.locator(".tile", has_text="FIRST")
    tile.hover()
    tile.locator('.wa-btn[data-action="delete"]').click()
    page.wait_for_function("() => !document.body.innerText.includes('FIRST')", timeout=15_000)

    # Every id-keyed surface must agree before anything reads one.
    ids = page.evaluate("""() => ({
        dom: [...document.querySelectorAll('.grid-stack-item')].map(e => e.getAttribute('gs-id')),
        tiles: [...document.querySelectorAll('.tile')].map(e => e.dataset.tileId),
        nodes: (document.querySelector('.grid-stack').gridstack?.engine.nodes ?? []).map(n => n.id),
    })""")
    assert ids["dom"] == ids["tiles"], ids
    assert ids["nodes"], "could not reach the GridStack instance — the check below is worthless"
    assert sorted(ids["nodes"]) == sorted(ids["tiles"]), ids

    # Now actually drag, which is what sends those ids to the server.
    target = page.locator(".tile", has_text="THIRD")
    box = target.bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + 12)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] / 2 + 260, box["y"] + 12, steps=12)
    page.mouse.up()
    page.wait_for_timeout(1200)

    assert position_calls, "no /positions request fired — the drag did not happen"
    assert all(status < 400 for status, _ in position_calls), position_calls
    assert "position:" in (root / "d.yaml").read_text()


def test_an_inline_sql_tile_still_runs_after_a_delete(page, tmp_path_factory):
    """A tile with inline `sql:` has no authored query name — the server hoists
    the SQL under the tile's derived id, so `tile.query` is a derived identity
    that goes stale on a delete exactly like the ids around it. The next run
    then posts a query name the server no longer has and the tile 404s. Same
    root cause as the wrong-tile delete, one surface over.
    """
    root = tmp_path_factory.mktemp("hoisted")
    (root / "d.yaml").write_text(
        "title: Q\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "filters: [{name: cat, type: text, label: Cat, default: a}]\n"
        "tiles:\n"
        "  - {sql: 'SELECT 1 AS n, {{ cat }} AS c'}\n"
        "  - {sql: 'SELECT 2 AS n, {{ cat }} AS c'}\n"
        "  - {markdown: THIRD}\n"
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    failures = []
    try:
        page.on("dialog", lambda d: d.accept())
        page.on(
            "response",
            lambda r: (
                failures.append(f"{r.status} {r.url}")
                if r.status >= 400 and "/api/" in r.url
                else None
            ),
        )
        page.goto(f"http://127.0.0.1:{port}/d/d?edit=1")
        page.wait_for_selector(".tile", timeout=30_000)
        tile = page.locator(".tile").first
        tile.hover()
        tile.locator('.wa-btn[data-action="delete"]').click()
        page.wait_for_timeout(1500)
        # Changing a filter re-runs the survivors, which is where a stale
        # hoisted name surfaces.
        page.fill('[data-filter="cat"]', "b")
        page.keyboard.press("Enter")
        # Wait for the re-run to actually land rather than sleeping a fixed
        # interval, so a slow machine cannot sample before the request does.
        page.wait_for_function(
            "() => document.querySelectorAll('.tile-status .skeleton').length === 0",
            timeout=20_000,
        )
        page.wait_for_timeout(300)
        assert not failures, failures
    finally:
        _stop_server(server, thread, page)


def test_the_source_banner_clears_after_the_broken_tiles_are_deleted(page, tmp_path_factory):
    """disposeTile cleared three of the six id-keyed maps. The shared-source
    banner is removed only when `runErrors` is empty, so entries left behind by
    deleted tiles kept it on screen forever — and a survivor renumbered into a
    deleted tile's id inherits whatever else was left under it.
    """
    root = tmp_path_factory.mktemp("banner")
    # A dead source, not a missing table: since #145 only connection-class
    # errors raise the banner at all. This test is about the banner being
    # cleared once the failing tiles are gone, not about what earns one.
    (root / "d.yaml").write_text(
        "title: B\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "sources:\n"
        "  dead: {type: postgres, host: 127.0.0.1, port: 1, database: x, "
        "username: y, password: z}\n"
        "filters: [{name: cat, type: text, label: Cat, default: a}]\n"
        "tiles:\n"
        "  - {title: Broken one, source: dead, sql: 'SELECT 1'}\n"
        "  - {title: Broken two, source: dead, sql: 'SELECT 1'}\n"
        "  - {title: Fine, sql: 'SELECT 1 AS n, {{ cat }} AS c'}\n"
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.on("dialog", lambda d: d.accept())
        page.goto(f"http://127.0.0.1:{port}/d/d?edit=1")
        # Tiles exist in the HTML before JS starts runs. Waiting for
        # "3 tiles and no skeletons" is true on first paint. Wait until
        # every chart tile has a result or an error instead.
        page.wait_for_function(
            """() => {
              const tiles = [...document.querySelectorAll('.tile:not(.tile-text)')];
              if (tiles.length !== 3) return false;
              if (document.querySelector('.tile-status .skeleton')) return false;
              return tiles.every((t) =>
                t.querySelector('.tile-status, canvas, table, svg'));
            }""",
            timeout=60_000,
        )
        page.locator("#source-banner").wait_for(state="visible")
        assert page.locator("#source-banner").count() == 1

        for title in ("Broken one", "Broken two"):
            tile = page.locator(".tile", has_text=title)
            tile.hover()
            with page.expect_response(
                lambda r: "/tiles/" in r.url and r.request.method == "DELETE",
                timeout=15_000,
            ) as deletion:
                tile.locator('.wa-btn[data-action="delete"]').click()
            assert deletion.value.status < 400, deletion.value.status
            tile.wait_for(state="detached", timeout=15_000)

        # A filter change re-runs what is left; the surviving tile succeeds, and
        # that is what re-evaluates the banner.
        page.fill('[data-filter="cat"]', "b")
        page.keyboard.press("Enter")
        page.wait_for_function(
            "() => !document.getElementById('source-banner')"
            " && document.querySelectorAll('.tile').length === 1",
            timeout=20_000,
        )
    finally:
        _stop_server(server, thread, page)


def test_a_renumbered_tile_keeps_asking_for_its_own_query(page, tmp_path_factory):
    """The client used to decide a query name was hoisted by checking
    `tile.query === tile.id`, which is also true of an authored `query:` whose
    name happens to match its derived id. Renaming that one made the tile ask
    for a name the file never gave it — answered by another tile's query,
    silently, which is worse than the 404 the same mistake causes elsewhere.
    """
    root = tmp_path_factory.mktemp("authored")
    (root / "d.yaml").write_text(
        "title: D\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "filters: [{name: cat, type: text, label: Cat, default: a}]\n"
        "queries:\n"
        "  q: \"SELECT '1' || {{ cat }} AS v\"\n"
        "  q_2: \"SELECT '2' || {{ cat }} AS v\"\n"
        "tiles:\n"
        "  - {query: q}\n"
        "  - {title: q, query: q_2}\n"
        "  - {query: q}\n"
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    failures = []
    try:
        page.on("dialog", lambda d: d.accept())
        page.on(
            "response",
            lambda r: (
                failures.append(f"{r.status} {r.url}")
                if r.status >= 400 and "/api/" in r.url
                else None
            ),
        )
        page.goto(f"http://127.0.0.1:{port}/d/d?edit=1")
        page.wait_for_selector(".tile", timeout=30_000)
        page.wait_for_timeout(1500)

        cells = (
            "() => [...document.querySelectorAll('.tile')]"
            ".map(e => (e.innerText.match(/[12][ab]/) || [''])[0])"
        )
        assert page.evaluate(cells) == ["1a", "2a", "1a"], page.evaluate(cells)

        first = page.locator(".tile").first
        first.hover()
        first.locator('.wa-btn[data-action="delete"]').click()
        page.wait_for_timeout(1500)
        page.fill('[data-filter="cat"]', "b")
        page.keyboard.press("Enter")
        # Wait for both survivors to show a `b` value rather than sleeping: the
        # assertion below is about which value, not about how fast it arrives.
        page.wait_for_function(
            "() => [...document.querySelectorAll('.tile')].every(e => /[12]b/.test(e.innerText))",
            timeout=20_000,
        )

        # The survivor that was `query: q_2` must still be showing q_2's value.
        assert page.evaluate(cells) == ["2b", "1b"], page.evaluate(cells)
        assert not failures, failures
    finally:
        _stop_server(server, thread, page)


def test_dashboard_details_is_an_overlay(page, served):
    """#213: ⓘ next to the title opens a named card. Grid must not jump."""
    _ensure_dashboard(page, served)
    assert page.locator("details.dash-info").count() == 0
    before = page.evaluate(
        "() => document.querySelector('.grid-stack').getBoundingClientRect().top"
    )
    page.click("#dash-info-dd .dd-btn")
    page.wait_for_selector(".dash-info:not([hidden])")
    page.wait_for_timeout(220)
    after = page.evaluate(
        """() => {
          const menu = document.querySelector(".dash-info");
          const grid = document.querySelector(".grid-stack");
          const r = menu.getBoundingClientRect();
          return {
            gridY: grid.getBoundingClientRect().top,
            tag: menu.tagName,
            left: r.left,
            right: r.right,
            vw: innerWidth,
            hasMetrics: /revenue/i.test(menu.textContent),
            named: (menu.querySelector(".dash-info-name")?.textContent || "").trim(),
            inTopbar: Boolean(document.querySelector(".topbar #dash-info-dd")),
            nextToTitle: Boolean(document.querySelector(".dash-title-row #dash-info-dd")),
          };
        }"""
    )
    assert after["tag"] == "DIV", after
    assert after["gridY"] == before, after
    assert after["left"] >= 7.5, after
    assert after["right"] <= after["vw"] - 7.5, after
    assert after["hasMetrics"], after
    assert after["named"] == "Order Analytics", after
    assert after["nextToTitle"], after
    assert not after["inTopbar"], after
    page.locator(".dash-info-row").first.hover()
    hover_bg = page.evaluate(
        """() => getComputedStyle(document.querySelector(".dash-info-row")).backgroundColor"""
    )
    assert hover_bg not in ("", "rgba(0, 0, 0, 0)", "transparent"), hover_bg
    hit = page.evaluate(
        """() => {
          const menu = document.querySelector(".dash-info");
          const r = menu.getBoundingClientRect();
          const el = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
          return {
            onBody: menu.parentElement === document.body,
            hitInMenu: menu.contains(el),
          };
        }"""
    )
    assert hit["onBody"], hit
    assert hit["hitInMenu"], hit
    page.keyboard.press("Escape")
    page.wait_for_function("() => document.querySelector('.dash-info').hidden")


def test_switcher_menu_stays_inside_the_viewport(page, tmp_path_factory):
    """#212: a long title used to size `.dd-menu` past the left edge."""
    long = (
        "Quarterly revenue by customer segment and billing region "
        "with year-over-year comparison for the board"
    )
    root = tmp_path_factory.mktemp("switcher")
    create_demo(root)
    (root / ".sqldash" / "long.yaml").write_text(
        f"title: {long}\nsource: {{type: duckdb, attach_files: true}}\ntiles: []\n"
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.set_viewport_size({"width": 900, "height": 800})
        page.goto(f"http://127.0.0.1:{port}/d/demo", wait_until="load")
        page.click("#dash-switcher-dd .dd-btn")
        page.wait_for_selector(".dd-menu.dd-open")
        page.wait_for_timeout(220)
        box = page.evaluate(
            """() => {
              const menu = document.querySelector(".dd-menu.dd-open");
              const item = [...menu.querySelectorAll(".dd-item")]
                .find((a) => (a.getAttribute("title") || "").includes("Quarterly"));
              const r = menu.getBoundingClientRect();
              return {
                left: r.left,
                right: r.right,
                width: r.width,
                vw: innerWidth,
                maxWidth: getComputedStyle(menu).maxWidth,
                itemTitle: item?.getAttribute("title") ?? "",
                itemWidth: item ? item.getBoundingClientRect().width : 0,
                onBody: menu.parentElement === document.body,
              };
            }"""
        )
        assert box["left"] >= 7.5, box
        assert box["right"] <= box["vw"] - 7.5, box
        assert box["width"] <= 360.5, box
        assert box["itemTitle"] == long, box
        assert box["itemWidth"] <= box["width"] + 1, box
        assert box["onBody"], box
    finally:
        page.set_viewport_size({"width": 1400, "height": 1000})
        _stop_server(server, thread, page)


def test_open_menus_are_hit_above_a_long_title(page, tmp_path_factory, monkeypatch):
    """A long h1 used to paint over the switcher and ⓘ menus."""
    long = (
        "Fulfillment Health, Carrier Delays and Returns Drill-down for the weekly "
        "ops review of every late shipment"
    )
    root = tmp_path_factory.mktemp("portal")
    create_demo(root)
    (root / ".sqldash" / "long.yaml").write_text(
        f"title: {long}\nsource: {{type: duckdb, attach_files: true}}\ntiles: []\n"
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    monkeypatch.setattr(app.state.watcher, "start", lambda _loop: None)
    server, thread, port = _start_server(app)
    try:
        page.set_viewport_size({"width": 900, "height": 800})
        page.goto(f"http://127.0.0.1:{port}/d/long", wait_until="load")
        page.click("#dash-switcher-dd .dd-btn")
        page.wait_for_selector(".dd-menu.dd-open")
        page.wait_for_timeout(220)
        switcher = page.evaluate(
            """() => {
              const menu = document.querySelector(".dd-menu.dd-open");
              const h1 = document.querySelector("#dash-title");
              const mr = menu.getBoundingClientRect();
              const hr = h1.getBoundingClientRect();
              const overlap = mr.bottom > hr.top && mr.top < hr.bottom
                && mr.right > hr.left && mr.left < hr.right;
              const x = overlap
                ? (Math.min(mr.right, hr.right) + Math.max(mr.left, hr.left)) / 2
                : mr.left + mr.width / 2;
              const y = overlap
                ? (Math.min(mr.bottom, hr.bottom) + Math.max(mr.top, hr.top)) / 2
                : mr.top + mr.height / 2;
              const el = document.elementFromPoint(x, y);
              return {
                overlap,
                onBody: menu.parentElement === document.body,
                hitInMenu: menu.contains(el),
                hit: el && (el.className || el.tagName),
              };
            }"""
        )
        assert switcher["overlap"], switcher
        assert switcher["onBody"], switcher
        assert switcher["hitInMenu"], switcher
        page.keyboard.press("Escape")
        page.wait_for_function("() => !document.querySelector('.dd-menu.dd-open')")
        restored = page.evaluate(
            "() => Boolean(document.querySelector('#dash-switcher-dd > .dd-menu'))"
        )
        assert restored

        page.click("#dash-info-dd .dd-btn")
        page.wait_for_selector(".dash-info.dd-open")
        page.wait_for_timeout(220)
        info = page.evaluate(
            """() => {
              const menu = document.querySelector(".dash-info.dd-open");
              const r = menu.getBoundingClientRect();
              const el = document.elementFromPoint(
                r.left + r.width / 2, r.top + r.height / 2
              );
              return {
                onBody: menu.parentElement === document.body,
                hitInMenu: menu.contains(el),
              };
            }"""
        )
        assert info["onBody"], info
        assert info["hitInMenu"], info
        page.keyboard.press("Escape")
        page.wait_for_function("() => !document.querySelector('.dd-menu.dd-open')")
        page.click("#dash-switcher-dd .dd-btn")
        page.wait_for_selector(".dd-menu.dd-open")
        page.locator(".dd-menu.dd-open .dd-item", has_text="Order Analytics").click()
        page.wait_for_url("**/d/demo")
    finally:
        page.set_viewport_size({"width": 1400, "height": 1000})
        _stop_server(server, thread, page)


def test_a_shared_sql_error_is_not_blamed_on_the_source(page, tmp_path_factory):
    """Two tiles failing the same missing-table (or a shared relation) used to
    raise 'try sqldash source test'. The warehouse parsed the SQL — it is a
    query problem."""
    root = tmp_path_factory.mktemp("banner2")
    (root / "d.yaml").write_text(
        "title: B\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  - {title: Broken one, sql: 'SELECT * FROM no_such_table'}\n"
        "  - {title: Broken two, sql: 'SELECT * FROM no_such_table'}\n"
        "  - {title: Fine, sql: 'SELECT 1 AS n'}\n"
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/d", wait_until="load")
        page.wait_for_function(
            "() => document.querySelectorAll('.tile-status .skeleton').length === 0",
            timeout=30_000,
        )
        assert page.locator("#source-banner").count() == 0
        status = page.locator('.tile[data-tile-id="broken_two"] .tile-status').inner_text()
        assert "no_such_table" in status, status
    finally:
        _stop_server(server, thread, page)


def test_source_banner_dismisses_when_errors_stop_looking_like_source_failures(page, served):
    """Banner was gated only at raise: connection-class → query-class re-runs
    left a stale 'source problem' claim. Dismiss recomputes like delete does.
    clearRunError must also recompute — one of two recovering tiles used to
    leave 'every affected tile' up with only one still broken."""
    _ensure_dashboard(page, served)
    state = page.evaluate(
        """async () => {
          const m = await import('/static/js/runner.js');
          const ids = [...document.querySelectorAll('.tile[data-tile-id]')]
            .slice(0, 2)
            .map(el => el.dataset.tileId);
          if (ids.length < 2) return {ok: false, reason: 'need two tiles'};
          // Plant tile bodies with .err so quieting/restore has somewhere to land.
          for (const id of ids) {
            const body = document.querySelector(`.tile[data-tile-id="${id}"] .tile-body`);
            body.innerHTML = '<div class="tile-status"><div class="err">seed</div></div>';
          }
          m.noteRunError(ids[0], 'connection refused');
          m.noteRunError(ids[1], 'connection refused');
          const raised = !!document.getElementById('source-banner');
          const raisedMsg = document.querySelector('#source-banner .sb-message')?.textContent;
          m.noteRunError(ids[0], 'Catalog Error: Table with name no_such_table does not exist');
          m.noteRunError(ids[1], 'Catalog Error: Table with name no_such_table does not exist');
          const afterQuery = !!document.getElementById('source-banner');
          // Re-raise source failure, then clear one tile — banner must drop.
          m.noteRunError(ids[0], 'connection refused');
          m.noteRunError(ids[1], 'connection refused');
          const raisedAgain = !!document.getElementById('source-banner');
          m.clearRunError(ids[0]);
          const afterOneClear = !!document.getElementById('source-banner');
          m.clearRunError(ids[1]);
          return {
            ok: true,
            raised,
            raisedMsg,
            afterQuery,
            raisedAgain,
            afterOneClear,
          };
        }"""
    )
    assert state.get("ok"), state
    assert state["raised"], state
    assert "connection refused" in (state["raisedMsg"] or ""), state
    assert state["afterQuery"] is False, state
    assert state["raisedAgain"], state
    assert state["afterOneClear"] is False, state


def test_one_dead_source_raises_the_banner_even_when_the_driver_words_it_differently(page, served):
    """libpq words a refused connect two ways depending on which syscall saw the
    RST, so two tiles on the same dead postgres got different messages and the
    exact-text grouping never raised the banner (about 1 run in 30)."""
    _ensure_dashboard(page, served)
    state = page.evaluate(
        """async () => {
          const m = await import('/static/js/runner.js');
          const ids = [...document.querySelectorAll('.tile[data-tile-id]')]
            .slice(0, 2)
            .map(el => el.dataset.tileId);
          if (ids.length < 2) return {ok: false, reason: 'need two tiles'};
          const refused = 'connection failed: connection to server at "127.0.0.1", port 1 '
            + 'failed: Connection refused\\n\\tIs the server running on that host?';
          const recv = 'connection failed: connection to server at "127.0.0.1", port 1 '
            + 'failed: could not receive data from server: Connection refused';
          const banner = () => !!document.getElementById('source-banner');
          const sameSource = [m.noteRunError(ids[0], refused, 'dead'),
                              m.noteRunError(ids[1], recv, 'dead'), banner()];
          m.clearRunError(ids[0]);
          m.clearRunError(ids[1]);
          const otherSources = [m.noteRunError(ids[0], refused, 'one'),
                                m.noteRunError(ids[1], recv, 'two'), banner()];
          m.clearRunError(ids[0]);
          m.clearRunError(ids[1]);
          return {ok: true, sameSource, otherSources, after: banner()};
        }"""
    )
    assert state.get("ok"), state
    assert state["sameSource"] == [False, True, True], state
    assert state["otherSources"] == [False, False, False], state
    assert state["after"] is False, state


def test_a_truncated_chart_says_so_like_a_truncated_table_does(page, tmp_path_factory):
    """The row cap is applied to every tile, but only renderTable said so. A
    line that stops early reads as the data ending, not as the cap — the same
    result presented honestly in one tile and misleadingly in the next.
    """
    root = tmp_path_factory.mktemp("trunc")
    (root / "d.yaml").write_text(
        "title: T\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  - {title: As line, chart: line, "
        "sql: 'SELECT i AS x, i*2 AS y FROM range(1, 40) t(i)'}\n"
        "  - {title: As big number, chart: big_number, "
        "sql: 'SELECT i AS x FROM range(1, 40) t(i)'}\n"
        "  - {title: As table, chart: table, "
        "sql: 'SELECT i AS x, i*2 AS y FROM range(1, 40) t(i)'}\n"
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"], row_limit=5)
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/d", wait_until="load")
        page.wait_for_function(
            "() => document.querySelectorAll('.tile-status .skeleton').length === 0",
            timeout=30_000,
        )
        page.wait_for_selector(".truncated-note", timeout=15_000)
        # Presence is not the claim — the first version of this fix appended a
        # note to the big-number tile that landed 26px *below* the tile and was
        # clipped, while this assertion passed. So: inside the tile, topmost at
        # its own text, and no scrollbar introduced.
        state = page.evaluate("""() => [...document.querySelectorAll('.tile')].map(e => {
            const note = e.querySelector('.truncated-note');
            if (!note) return {note: false};
            const r = note.getBoundingClientRect();
            const tile = e.getBoundingClientRect();
            const item = e.closest('.grid-stack-item-content') || e;
            const topmost = [0.1, 0.3, 0.5].map(f => {
                const hit = document.elementFromPoint(r.left + r.width * f, r.top + r.height / 2);
                return hit === note || note.contains(hit);
            });
            // elementFromPoint cannot see *into* a canvas — a DOM note always
            // reports topmost over one it overlaps. Compare the boxes instead,
            // which is what catches a reservation that silently did not apply.
            const mount = e.querySelector('.chart-mount');
            const m = mount && mount.getBoundingClientRect();
            return {
                note: true,
                inside: r.bottom <= tile.bottom + 1,
                covered: topmost.includes(false),
                overlapsChart: m ? r.top < m.bottom - 0.5 : false,
                scrollbar: item.scrollHeight > item.clientHeight,
            };
        })""")
        assert len(state) == 3, state
        for tile in state:
            assert tile["note"], state
            assert tile["inside"], state
            assert not tile["covered"], state
            assert not tile["overlapsChart"], state
            assert not tile["scrollbar"], state
        # The download button sits beside that note and used to promise a
        # complete file: same result, so it says partial too (#362).
        buttons = page.evaluate("""() => [...document.querySelectorAll('[data-action="csv"]')]
            .map(b => ({title: b.title, aria: b.getAttribute('aria-label'), href: b.href}))""")
        assert len(buttons) == 3, buttons
        for button in buttons:
            assert button["title"] == "Download CSV (partial: first 5 rows)", buttons
            assert button["aria"] == button["title"], buttons
            assert "/csv?name=" in button["href"], buttons
    finally:
        _stop_server(server, thread, page)


def test_the_metric_page_marks_truncation_too(page, tmp_path_factory):
    """`/m/<name>` renders through its own path, so it stayed silent while the
    dashboard learned to say it. Same row cap, same misleading picture — the
    surface-drift half of #116."""
    root = tmp_path_factory.mktemp("metricpage")
    create_demo(root)
    app = create_app(root / ".sqldash", allowed_hosts=["127.0.0.1", "localhost"], row_limit=3)
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/m/revenue", wait_until="load")
        page.wait_for_selector(".truncated-note", timeout=30_000)
        visible = page.evaluate("""() => {
            const note = document.querySelector('.truncated-note');
            const r = note.getBoundingClientRect();
            const hit = document.elementFromPoint(r.left + r.width * 0.2, r.top + r.height / 2);
            // The metric page sets .chart-mount { height: 170px }, and an
            // absolutely positioned box with both height and bottom keeps its
            // height — so the reserved strip silently did not apply here and the
            // note sat over the x-axis labels. A DOM note reads as topmost over
            // a canvas either way, so the boxes are what tell the truth.
            const mount = note.parentElement.querySelector('.chart-mount');
            const m = mount && mount.getBoundingClientRect();
            return {
                painted: r.height > 0,
                topmost: hit === note || note.contains(hit),
                overlapsChart: m ? r.top < m.bottom - 0.5 : false,
            };
        }""")
        assert visible["painted"], visible
        assert visible["topmost"], visible
        assert not visible["overlapsChart"], visible
    finally:
        _stop_server(server, thread, page)


def test_the_chart_builder_preview_marks_truncation(page, tmp_path_factory):
    """The preview is where an author decides whether a chart says what they
    meant. A series cut short by the row cap with nothing to say so is the same
    misreading #116 fixed on tiles — and the builder's own table preview already
    said it, so the two halves of one screen disagreed.
    """
    root = tmp_path_factory.mktemp("builder")
    create_demo(root)
    app = create_app(root / ".sqldash", allowed_hosts=["127.0.0.1", "localhost"], row_limit=5)
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/demo/query", wait_until="load")
        page.wait_for_selector("#qb-preview", timeout=30_000)
        page.locator(".ace_content").first.click()
        # ControlOrMeta so this selects all on the Linux CI runner too, not just macOS.
        page.keyboard.press("ControlOrMeta+A")
        page.keyboard.type("SELECT i AS x, i*2 AS y FROM range(1, 40) t(i)")
        # Assert the editor holds only our query before running it: if the
        # select-all does not take, the appended text is invalid SQL and the
        # failure arrives later as an opaque wait_for_selector timeout.
        editor_text = page.evaluate("() => document.querySelector('.ace_content').innerText")
        assert editor_text.count("SELECT") == 1, editor_text
        page.locator("#run-btn").click()
        page.wait_for_timeout(3000)
        page.locator("#qb-type button", has_text="Line").first.click()
        page.wait_for_selector("#qb-preview .truncated-note", timeout=15_000)

        geometry = page.evaluate("""() => {
            const prev = document.getElementById('qb-preview');
            const note = prev.querySelector('.truncated-note');
            const mount = prev.querySelector('.chart-mount');
            const r = note.getBoundingClientRect();
            return {
                hasChart: !!mount,
                inside: r.bottom <= prev.getBoundingClientRect().bottom + 1,
                overlapsChart: r.top < mount.getBoundingClientRect().bottom - 0.5,
            };
        }""")
        assert geometry["hasChart"], geometry
        assert geometry["inside"], geometry
        assert not geometry["overlapsChart"], geometry
    finally:
        _stop_server(server, thread, page)


def _probe(tag: str) -> str:
    return f"<img src=x onerror=\"window.pwned=(window.pwned||[]).concat('{tag}')\">"


def test_result_text_and_yaml_names_render_as_text_not_markup(page, tmp_path_factory):
    """A pie label is a query result and a chart builder option is a column name
    or a YAML `chart.x`; all three used to be spliced into HTML, so a warehouse
    row or a cloned dashboard ran script with the viewer's token. The context
    bypasses CSP so this pins the escaping itself, not the nonce behind it."""
    root = tmp_path_factory.mktemp("xss")
    label = _probe("pie").replace("'", "''")
    pie_sql = f"SELECT * FROM (VALUES ('{label}', 5), ('plain', 3)) t(label, v)"
    col_x, col_y = _probe("colx"), _probe("coly")
    quoted = [c.replace('"', '""') for c in (col_x, col_y)]
    builder_sql = f'SELECT \'a\' AS "{quoted[0]}", 1 AS "{quoted[1]}"'
    tiles = [
        {"id": "pie", "title": "Pie", "chart": "pie", "sql": pie_sql},
        {
            "id": "builder",
            "title": "Builder",
            "chart": {"type": "bar", "x": _probe("yamlx"), "y": [_probe("yamly")]},
            "sql": builder_sql,
        },
    ]
    (root / "d.yaml").write_text(
        f"title: XSS\nsource: {{type: duckdb, database: ':memory:'}}\ntiles: {json.dumps(tiles)}\n"
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    context = page.context.browser.new_context(bypass_csp=True)
    tab = context.new_page()
    try:
        tab.goto(f"http://127.0.0.1:{port}/d/d", wait_until="load")
        tab.wait_for_function(
            """() => {
                const mount = document.querySelector('.tile[data-tile-id="pie"] .chart-mount');
                return Boolean(mount && echarts.getInstanceByDom(mount));
            }""",
            timeout=15_000,
        )
        tip = tab.evaluate(
            """async () => {
                const mount = document.querySelector('.tile[data-tile-id="pie"] .chart-mount');
                echarts.getInstanceByDom(mount).dispatchAction(
                    {type: 'showTip', seriesIndex: 0, dataIndex: 0});
                await new Promise((r) => setTimeout(r, 400));
                const box = [...mount.querySelectorAll('div')].find(
                    (d) => d.style.position === 'absolute' && d.textContent.includes('62.5%'));
                return box && {text: box.textContent, imgs: box.querySelectorAll('img').length,
                               bold: box.querySelector('b')?.textContent};
            }"""
        )
        assert tip, "pie tooltip never showed"
        assert _probe("pie") in tip["text"], tip
        assert tip["imgs"] == 0, tip
        assert tip["bold"] == "5", tip

        tab.goto(f"http://127.0.0.1:{port}/d/d/query?tile=builder", wait_until="load")
        tab.wait_for_selector('#qb-encoding select[data-spec="x"]', state="attached")
        before = tab.evaluate(
            """() => ({
                x: document.querySelector('#qb-encoding select[data-spec="x"]').value,
                y: [...document.querySelectorAll('#qb-encoding [data-spec-y]')].map(
                    (b) => [b.value, b.checked, b.parentElement.textContent.trim()]),
            })"""
        )
        assert before["x"] == _probe("yamlx"), before
        assert before["y"] == [[_probe("yamly"), True, _probe("yamly")]], before
        tab.locator("#run-btn").click()
        tab.wait_for_function(
            """(name) => [...document.querySelectorAll('#qb-encoding [data-spec-y]')]
                .some((b) => b.value === name)""",
            arg=col_y,
            timeout=15_000,
        )
        after = tab.evaluate(
            """() => ({
                options: [...document.querySelector('#qb-encoding select[data-spec="x"]').options]
                    .map((o) => o.textContent),
                imgs: document.querySelectorAll('#qb-encoding img').length,
            })"""
        )
        assert col_x in after["options"], after
        assert col_y in after["options"], after
        assert after["imgs"] == 0, after
        assert tab.evaluate("() => window.pwned ?? null") is None
    finally:
        context.close()
        _stop_server(server, thread)


def test_the_csp_nonce_refuses_an_injected_inline_handler(page, served):
    """Defense in depth behind the escaping: markup that does reach the DOM
    cannot run an inline handler, while the nonced theme bootstrap still ran."""
    _ensure_dashboard(page, served)
    result = page.evaluate(
        """async () => {
            const holder = document.createElement('div');
            holder.innerHTML = '<img src=/nope onerror="window.cspProbe=1">';
            document.body.append(holder);
            await new Promise((r) => setTimeout(r, 500));
            holder.remove();
            return {ran: window.cspProbe ?? null, theme: document.documentElement.dataset.theme};
        }"""
    )
    assert result["ran"] is None, result
    assert result["theme"] in ("light", "dark"), result


def test_the_query_editor_shows_why_a_write_was_refused(page, served):
    """The ad-hoc guard answers 422 (#355); the editor has to show that verdict
    in its error box rather than a generic failure, since the editor is the
    surface a person is typing into when they hit it."""
    page.goto(f"{served}/d/demo/query", wait_until="load")
    page.wait_for_selector("#qb-preview", timeout=30_000)
    page.locator(".ace_content").first.click()
    page.keyboard.press("ControlOrMeta+A")
    page.keyboard.type("DELETE FROM orders")
    editor_text = page.evaluate("() => document.querySelector('.ace_content').innerText")
    assert "SELECT" not in editor_text, editor_text
    page.locator("#run-btn").click()
    page.wait_for_selector("#results-body .err", timeout=15_000)
    text = page.locator("#results-body .err").inner_text()
    assert "ad-hoc sql is read-only" in text, text
    assert "DELETE" in text, text
    assert "error" in page.locator("#results-meta").inner_text()


def _accept_completion(page, typed, caption):
    page.keyboard.press("Escape")
    page.keyboard.type(typed)
    page.wait_for_function(
        """([typed, caption]) => {
            const completer = ace.edit('sql-editor').completer;
            const list = completer?.activated && completer.completions;
            if (!list || list.filterText !== typed) return false;
            const row = list.filtered.findIndex(c => (c.caption || c.value) === caption);
            if (row < 0) return false;
            completer.popup.setRow(row);
            return true;
        }""",
        arg=[typed, caption],
    )
    page.keyboard.press("Tab")


def test_accepted_completions_run_for_names_that_need_quotes(page, tmp_path_factory):
    """Autocomplete inserted the raw name, so picking `my col` or `my table`
    wrote SQL that failed to parse, and a case-sensitive Snowflake column
    became an invalid identifier. It has to insert what the schema says to
    write."""
    root = tmp_path_factory.mktemp("quoted")
    with duckdb.connect(str(root / "q.duckdb")) as con:
        con.execute('CREATE TABLE "my table" ("my col" INT, "Mixed" INT)')
        con.execute('INSERT INTO "my table" VALUES (41, 1)')
    (root / "d.yaml").write_text(
        "title: Q\nsource: {type: duckdb, database: q.duckdb}\ntiles: []\n"
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/d/query", wait_until="load")
        page.wait_for_function(
            "() => document.getElementById('schema-status').textContent === '1 table'"
        )
        page.evaluate("() => ace.edit('sql-editor').setValue('', -1)")
        page.locator(".ace_content").first.click()
        page.keyboard.type("SELECT ")
        _accept_completion(page, "my", "my col")
        page.keyboard.type(" + ")
        _accept_completion(page, "Mix", "Mixed")
        page.keyboard.type(" AS total FROM ")
        _accept_completion(page, "my", "main.my table")
        sql = page.evaluate("() => ace.edit('sql-editor').getValue()")
        assert sql == 'SELECT "my col" + "Mixed" AS total FROM "my table"', sql
        page.locator("#run-btn").click()
        page.wait_for_selector("#results-body table")
        assert "42" in page.locator("#results-body").inner_text()
    finally:
        _stop_server(server, thread, page)


def test_a_one_row_markdown_tile_shows_all_of_its_text(page, tmp_path_factory):
    """`.tile` clips its overflow, so shrinking a prose tile below its content
    made the sentence under the heading vanish with nothing to say it was still
    there. A heading plus a wrapped sentence has to fit the single row authors
    reach for, and anything longer has to stay reachable rather than disappear.
    """
    root = tmp_path_factory.mktemp("md")
    (root / "d.yaml").write_text(
        "title: MD\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  - markdown: |\n"
        "      ### 2. Question leaderboard\n"
        "\n"
        "      Ranked by leverage. Prompt-variant grain is the default deliberately —\n"
        "      rolling variants together hides the signal.\n"
        "    size: 12x1\n"
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/d", wait_until="load")
        page.wait_for_selector(".tile.tile-text .tile-body")
        metrics = page.evaluate(
            """() => {
                const body = document.querySelector('.tile.tile-text .tile-body');
                return {
                    box: body.clientHeight,
                    content: body.scrollHeight,
                    overflowY: getComputedStyle(body).overflowY,
                };
            }"""
        )
        assert metrics["content"] <= metrics["box"] + 1, metrics
        assert metrics["overflowY"] in ("auto", "scroll"), metrics
    finally:
        _stop_server(server, thread, page)


def test_a_markdown_heading_sits_against_what_it_heads(page, tmp_path_factory):
    """A prose tile taller than its text left the slack *below* the text, so a
    section heading floated with ~80px of nothing under it and read as a
    footnote to the tiles above rather than a heading for the tiles below.
    """
    root = tmp_path_factory.mktemp("hdr")
    (root / "d.yaml").write_text(
        "title: H\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        'queries: {q: "SELECT 1 AS v"}\n'
        "tiles:\n"
        "  - {title: Above, query: q, position: {x: 0, y: 0, w: 12, h: 3}}\n"
        "  - markdown: |\n"
        "      ### Section two\n"
        "\n"
        "      A sentence of standfirst under the heading.\n"
        "    position: {x: 0, y: 3, w: 12, h: 2}\n"
        "  - {title: Below, query: q, position: {x: 0, y: 5, w: 12, h: 3}}\n"
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/d", wait_until="load")
        page.wait_for_selector(".tile.tile-text .tile-body p")
        gaps = page.evaluate(
            """() => {
                const body = document.querySelector('.tile.tile-text .tile-body');
                const last = body.querySelector(':scope > :last-child');
                const first = body.querySelector(':scope > :first-child');
                const b = body.getBoundingClientRect();
                return {
                    below: Math.round(b.bottom - last.getBoundingClientRect().bottom),
                    above: Math.round(first.getBoundingClientRect().top - b.top),
                };
            }"""
        )
        # The slack belongs above the prose, not under it.
        assert gaps["below"] <= 8, gaps
        assert gaps["above"] > gaps["below"], gaps
    finally:
        _stop_server(server, thread, page)


def test_growing_a_tile_does_not_409_when_nothing_settles(page, tmp_path_factory):
    """A resize that opens no hole never enters settle()'s batchUpdate, so
    GridStack still fires `change`. Saving on every resizestop *and* on
    change sent two PATCHes with the same If-Match; one 409'd and the toast
    claimed the file changed on disk."""
    root = tmp_path_factory.mktemp("nohole")
    (root / "d.yaml").write_text(
        "title: NoHole\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        'queries: {q: "SELECT 1 AS v"}\n'
        "tiles:\n"
        "  - {title: Left, query: q, position: {x: 0, y: 0, w: 6, h: 2}}\n"
        "  - {title: Right, query: q, position: {x: 6, y: 0, w: 6, h: 2}}\n"
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    statuses = []
    try:
        page.on(
            "response",
            lambda r: statuses.append(r.status) if "/positions" in r.url else None,
        )
        page.goto(f"http://127.0.0.1:{port}/d/d?edit=1", wait_until="load")
        page.wait_for_selector(".grid-stack-item")
        row_h = page.evaluate(
            "() => document.querySelector('.grid-stack').gridstack.getCellHeight()"
        )
        page.locator('.grid-stack-item[gs-id="right"]').hover()
        handle = page.locator('.grid-stack-item[gs-id="right"] .ui-resizable-se')
        handle.wait_for(state="visible")
        box = handle.bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()
        page.mouse.move(
            box["x"] + box["width"] / 2, box["y"] + box["height"] / 2 + row_h * 2, steps=10
        )
        page.mouse.up()
        page.wait_for_timeout(1200)
        assert 409 not in statuses, statuses
        assert statuses, "no /positions request — the resize did not save"
        assert all(s == 200 for s in statuses), statuses
        saved = (root / "d.yaml").read_text()
        assert "h: 4}" in saved or "h: 3}" in saved, saved
    finally:
        _stop_server(server, thread, page)


def test_shrinking_a_tile_does_not_leave_a_hole_under_it(page, tmp_path_factory):
    """A tile made smaller left the rows it vacated empty and the positions
    PATCH saved that hole, so a heading trimmed to one row still had 80px of
    nothing under it — a gap no padding could close, because it was an empty
    grid row.

    Driven through the real resize handle: the settle is keyed on `resizestop`,
    so a programmatic `grid.update()` proves nothing about what a user does.
    """
    root = tmp_path_factory.mktemp("hole")
    (root / "d.yaml").write_text(
        "title: Hole\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        'queries: {q: "SELECT 1 AS v"}\n'
        "tiles:\n"
        '  - {markdown: "### Section", position: {x: 0, y: 0, w: 12, h: 3}}\n'
        "  - {title: Left, query: q, position: {x: 0, y: 3, w: 6, h: 3}}\n"
        "  - {title: Right, query: q, position: {x: 6, y: 3, w: 6, h: 3}}\n"
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/d?edit=1", wait_until="load")
        page.wait_for_selector(".grid-stack-item")
        dashboard_row_height = page.evaluate(
            "() => document.querySelector('.grid-stack').gridstack.getCellHeight()"
        )
        # The resize handle is display:none until the tile is hovered.
        page.locator('.grid-stack-item[gs-id="tile_1"]').hover()
        handle = page.locator('.grid-stack-item[gs-id="tile_1"] .ui-resizable-se')
        handle.wait_for(state="visible")
        box = handle.bounding_box()
        tile_box = page.locator('.grid-stack-item[gs-id="tile_1"]').bounding_box()
        # Drop the bottom edge one row below the tile's top: 3 rows -> 1.
        target_y = tile_box["y"] + dashboard_row_height
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()
        page.mouse.move(box["x"] + box["width"] / 2, target_y, steps=12)
        page.mouse.up()
        page.wait_for_timeout(1200)

        rows = page.evaluate(
            """() => document.querySelector('.grid-stack').gridstack.engine.nodes
                .map((n) => ({id: n.id, y: n.y, h: n.h}))"""
        )
        covered = {y for r in rows for y in range(r["y"], r["y"] + r["h"])}
        assert covered == set(range(max(covered) + 1)), (rows, sorted(covered))
        # ...and it has to reach the file. `"position:" in text` was true of the
        # authored YAML before the resize ran, so it passed while the resize was
        # being discarded entirely — the settle swallowed the only save.
        saved = (root / "d.yaml").read_text()
        assert "h: 1}" in saved, saved
        assert "y: 1," in saved, saved
    finally:
        _stop_server(server, thread, page)


def test_resizing_a_tile_does_not_close_a_gap_above_it(page, tmp_path_factory):
    """Whole-grid gravity closed authored gaps the resize did not open. A
    heading, two empty rows, then a shrink of the tile under them rewrote
    the file with everyone packed to y: 1.
    """
    root = tmp_path_factory.mktemp("authored-gap")
    (root / "d.yaml").write_text(
        "title: Gap\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        'queries: {q: "SELECT 1 AS v"}\n'
        "tiles:\n"
        '  - {markdown: "### Section", position: {x: 0, y: 0, w: 12, h: 1}}\n'
        "  - {title: Left, query: q, position: {x: 0, y: 3, w: 6, h: 2}}\n"
        "  - {title: Right, query: q, position: {x: 6, y: 3, w: 6, h: 2}}\n"
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/d?edit=1", wait_until="load")
        page.wait_for_selector(".grid-stack-item")
        dashboard_row_height = page.evaluate(
            "() => document.querySelector('.grid-stack').gridstack.getCellHeight()"
        )
        page.locator('.grid-stack-item[gs-id="left"]').hover()
        handle = page.locator('.grid-stack-item[gs-id="left"] .ui-resizable-se')
        handle.wait_for(state="visible")
        box = handle.bounding_box()
        tile_box = page.locator('.grid-stack-item[gs-id="left"]').bounding_box()
        target_y = tile_box["y"] + dashboard_row_height
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()
        page.mouse.move(box["x"] + box["width"] / 2, target_y, steps=12)
        page.mouse.up()
        page.wait_for_timeout(1200)

        rows = _engine_rows(page)
        assert rows["left"]["y"] == 3, rows
        assert rows["right"]["y"] == 3, rows
        assert rows["tile_1"]["y"] == 0, rows
        saved = (root / "d.yaml").read_text()
        assert "y: 3," in saved, saved
        assert "h: 1}" in saved, saved
    finally:
        _stop_server(server, thread, page)


def test_shrinking_a_half_width_tile_does_not_leave_a_hole_beside_a_wide_one(
    page, tmp_path_factory
):
    """One pass only packs tiles that overlap the hole. A full-width tile
    under a half-width shrink moved up and left the other column empty —
    the hole just moved down a row.
    """
    root = tmp_path_factory.mktemp("cascade-hole")
    (root / "d.yaml").write_text(
        "title: Cascade\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        'queries: {q: "SELECT 1 AS v"}\n'
        "tiles:\n"
        "  - {title: A, query: q, position: {x: 0, y: 0, w: 6, h: 2}}\n"
        "  - {title: Wide, query: q, position: {x: 0, y: 2, w: 12, h: 2}}\n"
        "  - {title: B, query: q, position: {x: 6, y: 4, w: 6, h: 2}}\n"
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/d?edit=1", wait_until="load")
        page.wait_for_selector(".grid-stack-item")
        dashboard_row_height = page.evaluate(
            "() => document.querySelector('.grid-stack').gridstack.getCellHeight()"
        )
        page.locator('.grid-stack-item[gs-id="a"]').hover()
        handle = page.locator('.grid-stack-item[gs-id="a"] .ui-resizable-se')
        handle.wait_for(state="visible")
        box = handle.bounding_box()
        tile_box = page.locator('.grid-stack-item[gs-id="a"]').bounding_box()
        target_y = tile_box["y"] + dashboard_row_height
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
        page.mouse.down()
        page.mouse.move(box["x"] + box["width"] / 2, target_y, steps=12)
        page.mouse.up()
        page.wait_for_timeout(1200)

        rows = _engine_rows(page)
        covered = {y for r in rows.values() for y in range(r["y"], r["y"] + r["h"])}
        assert covered == set(range(max(covered) + 1)), (rows, sorted(covered))
        assert rows["wide"]["y"] == 1, rows
        assert rows["b"]["y"] == 3, rows
        saved = (root / "d.yaml").read_text()
        assert "y: 1," in saved, saved
        assert "y: 3," in saved, saved
    finally:
        _stop_server(server, thread, page)


def test_dragging_a_tile_keeps_where_it_was_dropped(page, tmp_path_factory):
    """Settling the whole grid on every change put a dragged tile straight back
    in the cell it had just left — `compact()` re-packs from the top-left, and
    the vacated cell is the first one free. The drag looked like it worked until
    the mouse came up, and the PATCH saved the pre-drag position.
    """
    root = tmp_path_factory.mktemp("drag")
    (root / "d.yaml").write_text(
        "title: Drag\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        'queries: {q: "SELECT 1 AS v"}\n'
        "tiles:\n"
        "  - {title: A, query: q, position: {x: 0, y: 0, w: 6, h: 2}}\n"
        "  - {title: B, query: q, position: {x: 6, y: 0, w: 6, h: 2}}\n"
        "  - {title: C, query: q, position: {x: 0, y: 2, w: 6, h: 2}}\n"
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/d?edit=1", wait_until="load")
        page.wait_for_selector(".grid-stack-item")
        tile = page.locator('.grid-stack-item[gs-id="c"]')
        box = tile.bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + 12)
        page.mouse.down()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + 12 + 320, steps=15)
        page.mouse.up()
        page.wait_for_timeout(1200)

        landed = page.evaluate(
            """() => Number(document.querySelector('.grid-stack-item[gs-id="c"]')
                .getAttribute('gs-y'))"""
        )
        assert landed > 2, landed
        assert f"y: {landed}" in (root / "d.yaml").read_text(), landed
    finally:
        _stop_server(server, thread, page)


def test_switching_into_edit_mode_does_not_rewrite_the_layout(page, tmp_path_factory):
    """Entering the editor restores authored tile sizes, which fires `change`.
    While the handler settled the whole grid on every change, merely clicking
    Edit — touching nothing — closed deliberate gaps and wrote them to disk.
    """
    root = tmp_path_factory.mktemp("mode")
    (root / "d.yaml").write_text(
        "title: Mode\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        'queries: {q: "SELECT 1 AS v UNION ALL SELECT 2"}\n'
        "tiles:\n"
        "  - {title: Tbl, query: q, position: {x: 0, y: 0, w: 12, h: 5}}\n"
        "  - {title: Mid, query: q, position: {x: 0, y: 6, w: 6, h: 2}}\n"
        "  - {title: Bot, query: q, position: {x: 6, y: 6, w: 6, h: 2}}\n"
    )
    before = (root / "d.yaml").read_text()
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/d", wait_until="load")
        page.wait_for_selector(".grid-stack-item")
        page.get_by_text("Edit", exact=True).first.click()
        page.wait_for_timeout(1500)
        rows = page.evaluate(
            """() => Object.fromEntries(document.querySelector('.grid-stack').gridstack
                .engine.nodes.map((n) => [n.id, n.y]))"""
        )
        assert rows["mid"] == 6, rows
        assert rows["bot"] == 6, rows
        assert (root / "d.yaml").read_text() == before
    finally:
        _stop_server(server, thread, page)


DELETE_THEN_DRAG = """title: DelDrag
source: {type: duckdb, database: ':memory:'}
queries: {q: "SELECT 1 AS v"}
tiles:
  - {title: A, query: q, position: {x: 0, y: 0, w: 6, h: 2}}
  - {title: B, query: q, position: {x: 6, y: 0, w: 6, h: 2}}
  - {title: C, query: q, position: {x: 0, y: 2, w: 6, h: 2}}
"""


def _engine_rows(page):
    return page.evaluate(
        """() => Object.fromEntries(document.querySelector('.grid-stack').gridstack
            .engine.nodes.map((n) => [n.id, {x: n.x, y: n.y, h: n.h}]))"""
    )


def test_deleting_a_tile_closes_the_rows_it_vacated(page, tmp_path_factory):
    """A removal emits `removed`, never `change`, so nothing settled or saved
    it: the vacated rows stayed empty and were never written. The earlier fix
    set a flag here and waited for a `change` that a delete does not produce.
    """
    root = tmp_path_factory.mktemp("del")
    (root / "d.yaml").write_text(DELETE_THEN_DRAG)
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.on("dialog", lambda d: d.accept())
        page.goto(f"http://127.0.0.1:{port}/d/d?edit=1", wait_until="load")
        page.wait_for_selector(".grid-stack-item")
        tile = page.locator('.tile[data-tile-id="a"]')
        tile.hover()
        tile.locator('.wa-btn[data-action="delete"]').click()
        page.wait_for_function("() => !document.body.innerText.includes('A')", timeout=15_000)
        page.wait_for_timeout(1200)

        # Row coverage alone proves nothing here: B still spans rows 0-1 at
        # x=6, so the rows stay contiguous while column 0 is empty. What has to
        # be true is that C settled into the space A vacated.
        rows = _engine_rows(page)
        assert rows["c"]["y"] == 0, rows
        # Straight up its own column. A whole-grid re-pack answered "delete one
        # tile" by swapping the survivors' columns — B into the vacated x=0 and
        # C across to x=6 — and wrote the swap to the file.
        assert rows["c"]["x"] == 0, rows
        assert rows["b"] == {"x": 6, "y": 0, "h": 2}, rows
        # C's *own* settled position has to reach the file. Asserting a bare
        # "y: 0" passed before the delete ran — B already sits at y: 0 — so it
        # gave no cover at all.
        saved = (root / "d.yaml").read_text()
        assert "{title: C, query: q, position: {x: 0, y: 0, w: 6, h: 2}}" in saved, saved
    finally:
        _stop_server(server, thread, page)


def test_a_drag_after_a_delete_still_lands_where_it_was_dropped(page, tmp_path_factory):
    """The delete set a settle flag that no `change` ever consumed, so it sat
    armed until the next drag and re-packed the tile the author had just
    dropped — the drop was lost and the pre-drag position saved.
    """
    root = tmp_path_factory.mktemp("deldrag")
    (root / "d.yaml").write_text(DELETE_THEN_DRAG)
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.on("dialog", lambda d: d.accept())
        page.goto(f"http://127.0.0.1:{port}/d/d?edit=1", wait_until="load")
        page.wait_for_selector(".grid-stack-item")
        tile = page.locator('.tile[data-tile-id="a"]')
        tile.hover()
        tile.locator('.wa-btn[data-action="delete"]').click()
        page.wait_for_function("() => !document.body.innerText.includes('A')", timeout=15_000)
        page.wait_for_timeout(900)

        before = _engine_rows(page)["c"]["y"]
        target = page.locator('.grid-stack-item[gs-id="c"]')
        box = target.bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + 12)
        page.mouse.down()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + 12 + 320, steps=15)
        page.mouse.up()
        page.wait_for_timeout(1200)

        landed = _engine_rows(page)["c"]["y"]
        assert landed > before, (before, landed)
        assert f"y: {landed}" in (root / "d.yaml").read_text(), landed
    finally:
        _stop_server(server, thread, page)


def test_a_clipped_table_cell_opens_the_full_value(page, tmp_path_factory):
    root = tmp_path_factory.mktemp("cell")
    (root / "d.yaml").write_text(
        "title: C\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  - title: Payload\n"
        "    chart: table\n"
        "    sql: |\n"
        '      SELECT \'{"error":"timeout","sql":"SELECT 1"}\' AS payload\n'
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/d")
        page.wait_for_selector("table.results td.cell-inspect", timeout=30_000)
        cell = page.locator("table.results td.cell-inspect").first
        assert cell.inner_text() == '{"error":"timeout","sql":"SELECT 1"}'
        assert "timeout" in (cell.get_attribute("title") or "")
        cell_box = cell.bounding_box()
        cell.click()
        pop = page.locator(".cell-pop")
        pop.wait_for()
        box = pop.bounding_box()
        assert box is not None
        assert box["y"] >= 0
        assert box["y"] + box["height"] <= page.viewport_size["height"]
        assert box["y"] < cell_box["y"] + cell_box["height"]
        assert box["y"] + box["height"] > cell_box["y"]
        assert box["width"] + 1 >= min(cell_box["width"], 280)
        assert pop.locator(".cell-pop-bar").count() == 0
        assert pop.locator(".cell-pop-copy").get_attribute("aria-label") == "Copy"
        body = pop.locator(".cell-pop-body")
        assert "is-pretty" in (body.get_attribute("class") or "")
        text = body.inner_text()
        assert '"error": "timeout"' in text
        assert "SELECT 1" in text
        assert page.evaluate(
            "() => document.querySelector('.cell-pop').parentElement.classList.contains('tile')"
        )
        page.evaluate(
            """() => {
              const spacer = document.createElement('div');
              spacer.style.height = '2000px';
              document.body.appendChild(spacer);
            }"""
        )
        before = page.evaluate(
            """() => {
              const pop = document.querySelector('.cell-pop');
              const tile = pop.closest('.tile');
              return pop.getBoundingClientRect().top - tile.getBoundingClientRect().top;
            }"""
        )
        page.evaluate("() => window.scrollBy(0, 240)")
        page.wait_for_timeout(50)
        after = page.evaluate(
            """() => {
              const pop = document.querySelector('.cell-pop');
              const tile = pop.closest('.tile');
              const cell = document.querySelector('td.cell-inspect');
              const pr = pop.getBoundingClientRect();
              const cr = cell.getBoundingClientRect();
              return {
                dy: pr.top - tile.getBoundingClientRect().top,
                onCell: pr.bottom > cr.top && pr.top < cr.bottom,
                parentTile: tile.contains(pop),
              };
            }"""
        )
        assert after["parentTile"], after
        assert abs(after["dy"] - before) < 2, (before, after)
        assert after["onCell"], after
        page.keyboard.press("Escape")
        page.wait_for_selector(".cell-pop", state="detached")
    finally:
        _stop_server(server, thread, page)


def test_a_tall_cell_value_stays_readable_inside_the_tile(page, tmp_path_factory):
    """The overlay stays on the card, but the body must scroll — overflow:hidden
    on the pop used to clip the value with no scrollbar."""
    root = tmp_path_factory.mktemp("tallcell")
    (root / "d.yaml").write_text(
        "title: C\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  - title: Notes\n"
        "    chart: table\n"
        "    size: 12x6\n"
        "    sql: |\n"
        "      SELECT repeat('the quick brown fox jumps. ', 80) AS note\n"
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/d")
        page.wait_for_selector("table.results td.cell-inspect", timeout=30_000)
        page.locator("table.results td.cell-inspect").first.click()
        page.wait_for_selector(".cell-pop")
        state = page.evaluate(
            """() => {
              const pop = document.querySelector('.cell-pop');
              const body = pop.querySelector('.cell-pop-body');
              const text = body.textContent;
              const canScroll = body.scrollHeight > body.clientHeight + 1;
              body.scrollTop = body.scrollHeight;
              return {
                parentTile: pop.parentElement.classList.contains('tile'),
                canScroll,
                atEnd: body.scrollTop + body.clientHeight >= body.scrollHeight - 2,
                hasHead: text.startsWith('the quick brown fox'),
                hasTail: text.trim().endsWith('jumps.'),
                len: text.length,
              };
            }"""
        )
        assert state["parentTile"], state
        assert state["hasHead"], state
        assert state["hasTail"], state
        assert state["canScroll"], state
        assert state["atEnd"], state
        page.keyboard.press("Escape")
        page.wait_for_selector(".cell-pop", state="detached")
    finally:
        _stop_server(server, thread, page)


def test_a_horizontal_bar_puts_values_on_the_x_axis(page, tmp_path_factory):
    root = tmp_path_factory.mktemp("hbar")
    (root / "d.yaml").write_text(
        "title: H\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  - title: Rank\n"
        "    chart: {type: bar, orientation: horizontal}\n"
        "    sql: |\n"
        "      SELECT * FROM (VALUES ('north', 10), ('south', 20)) t(region, n)\n"
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/d")
        page.wait_for_selector(".tile canvas", timeout=30_000)
        axes = page.evaluate(
            """() => {
              const el = document.querySelector('.tile .chart-mount');
              const chart = echarts.getInstanceByDom(el);
              const opt = chart.getOption();
              return {x: opt.xAxis[0].type, y: opt.yAxis[0].type};
            }"""
        )
        assert axes == {"x": "value", "y": "category"}, axes
    finally:
        _stop_server(server, thread, page)


def test_editing_a_tile_uses_the_query_page(page, tmp_path_factory):
    """Pencil used to open a right-side drawer. Add and edit now share /query."""
    root = tmp_path_factory.mktemp("qedit")
    (root / "d.yaml").write_text(
        "title: D\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "queries:\n  q: |\n    SELECT 1 AS n\n"
        "tiles:\n"
        "  - title: Numbers\n"
        "    query: q\n"
        "    chart: table\n"
        "    size: 6x4\n"
        "  - title: Note\n"
        "    markdown: hello\n"
        "    size: 6x2\n"
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/d?edit=1", wait_until="load")
        page.wait_for_selector(".tile", timeout=30_000)
        assert page.locator("#tile-drawer").count() == 0

        tile = page.locator(".tile", has_text="Numbers")
        tile.hover()
        tile.locator('.wa-btn[data-action="edit"]').click()
        page.wait_for_url("**/query?tile=numbers**", timeout=15_000)
        assert page.locator("#qb-title").input_value() == "Numbers"
        assert "Save tile" in page.locator("#qb-add").inner_text()
        page.locator("#qb-title").fill("Renamed")
        with page.expect_response(
            lambda r: "/tiles/" in r.url and r.request.method == "PUT",
            timeout=15_000,
        ) as saved:
            page.locator("#qb-add").click()
        assert saved.value.status < 400, saved.value.status
        page.wait_for_url("**/d/d?edit=1**", timeout=15_000)
        text = (root / "d.yaml").read_text()
        assert "Renamed" in text, text
        assert "query: q" in text, text
        assert "SELECT 1 AS n" in text, text

        page.wait_for_selector(".tile", timeout=30_000)
        note = page.locator(".tile", has_text="hello")
        note.hover()
        note.locator('.wa-btn[data-action="edit"]').click()
        page.wait_for_url("**/query?tile=note**", timeout=15_000)
        page.wait_for_selector("#text-editor:not([hidden])", timeout=10_000)
        page.locator("#text-markdown").fill("goodbye")
        with page.expect_response(
            lambda r: "/tiles/" in r.url and r.request.method == "PUT",
            timeout=15_000,
        ):
            page.locator("#qb-add").click()
        page.wait_for_url("**/d/d?edit=1**", timeout=15_000)
        assert "goodbye" in (root / "d.yaml").read_text()
    finally:
        _stop_server(server, thread, page)


def test_unknown_tile_id_does_not_look_like_a_fresh_tile(page, served):
    """A stale `?tile=` used to render a blank New tile with only a toast, which
    expired. The notice has to outlive a toast, so this waits one out."""
    page.goto(f"{served}/d/demo/query?tile=gone", wait_until="load")
    page.wait_for_selector("#missing-tile", timeout=15_000)
    page.wait_for_timeout(5_000)
    state = page.evaluate("""() => ({
        title: document.title,
        crumb: document.querySelectorAll('.page-title')[1]?.textContent.trim(),
        banner: document.getElementById('missing-tile')?.innerText ?? '',
        visible: Boolean(document.getElementById('missing-tile')?.offsetParent),
        toasts: document.getElementById('toasts')?.children.length,
        overflow: document.documentElement.scrollHeight - window.innerHeight,
    })""")
    assert state["title"].startswith("Tile not found ·"), state
    assert state["crumb"] == "tile not found", state
    assert "gone" in state["banner"], state
    assert state["visible"], state
    assert state["toasts"] == 0, state
    assert state["overflow"] <= 0, state


_SOURCE_PICKER_SHOWN = """() => {
  const s = document.getElementById('source-picker');
  return Boolean(s) && !s.hidden && !s.closest('.dd')?.hasAttribute('hidden');
}"""


def test_editing_a_metric_tile_opens_the_query_page(page, tmp_path_factory):
    root = tmp_path_factory.mktemp("qmetric")
    create_demo(root)
    app = create_app(root / ".sqldash", allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/demo?edit=1", wait_until="load")
        page.wait_for_selector(".tile", timeout=30_000)
        tile = page.locator(".tile", has_text="Total revenue")
        tile.hover()
        tile.locator('.wa-btn[data-action="edit"]').click()
        page.wait_for_url("**/query?tile=total_revenue**", timeout=15_000)
        page.wait_for_selector("#metric-pane:not([hidden])", timeout=10_000)
        page.wait_for_function(
            "() => document.getElementById('metric-picker')?.value === 'revenue'",
            timeout=30_000,
        )
        assert page.locator("#sql-editor").evaluate("el => el.style.display") == "none"
        assert page.evaluate(_SOURCE_PICKER_SHOWN)
    finally:
        _stop_server(server, thread, page)


def test_source_picker_stays_visible_in_metric_mode(page, tmp_path_factory):
    root = tmp_path_factory.mktemp("srcpick")
    create_demo(root)
    app = create_app(root / ".sqldash", allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/demo/query", wait_until="load")
        assert page.evaluate(_SOURCE_PICKER_SHOWN)
        assert page.evaluate(
            """() => {
              const q = document.getElementById('query-picker')?.closest('.dd');
              const s = document.getElementById('source-picker')?.closest('.dd');
              const ghost = q?.querySelector('.dd-btn')?.classList.contains('dd-btn-ghost');
              return ghost && q?.dataset.icon === 'file' && q?.dataset.crumb === 'saved'
                && s?.dataset.icon === 'db' && s?.dataset.skin === 'chip';
            }"""
        )
        page.locator('#mode-toggle [data-mode="metric"]').click()
        page.wait_for_selector("#metric-pane:not([hidden])", timeout=10_000)
        assert page.evaluate(_SOURCE_PICKER_SHOWN)
        page.wait_for_function(
            """() => {
              const s = document.getElementById('metric-picker');
              return s && [...s.options].some(o => o.value === 'revenue');
            }""",
            timeout=30_000,
        )
        page.evaluate(
            """() => {
              const s = document.getElementById('metric-picker');
              s.value = 'revenue';
              s.dispatchEvent(new Event('change', { bubbles: true }));
            }"""
        )
        page.wait_for_function(
            """() => {
              const opt = document.querySelector('#source-picker option[value=""]');
              return opt && opt.textContent.startsWith('definition ·');
            }""",
            timeout=5_000,
        )
        page.locator('#mode-toggle [data-mode="sql"]').click()
        assert page.evaluate(
            """() => {
              const opt = document.querySelector('#source-picker option[value=""]');
              return opt && opt.textContent.startsWith('default ·');
            }"""
        )
        page.locator('#mode-toggle [data-mode="text"]').click()
        page.wait_for_selector("#text-editor:not([hidden])", timeout=10_000)
        assert not page.evaluate(_SOURCE_PICKER_SHOWN)
    finally:
        _stop_server(server, thread, page)


def test_dashboard_metric_tile_runs_against_its_source(page, tmp_path_factory):
    root = tmp_path_factory.mktemp("msrc")
    create_demo(root)
    path = root / ".sqldash" / "demo.yaml"
    text = path.read_text()
    text = text.replace(
        "source: {type: duckdb, attach_files: true}",
        "source: {type: duckdb, attach_files: true}\n"
        "sources:\n  alt: {type: duckdb, attach_files: true}",
        1,
    )
    text = text.replace(
        "    metric: revenue\n    size: 6x2",
        "    metric: revenue\n    source: alt\n    size: 6x2",
        1,
    )
    path.write_text(text)
    app = create_app(root / ".sqldash", allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    posted = []

    def on_request(req):
        if "/api/run" in req.url and req.method == "POST" and req.post_data:
            posted.append(req.post_data)

    page.on("request", on_request)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/demo", wait_until="load")
        page.wait_for_selector(".tile", timeout=30_000)
        deadline = time.monotonic() + 15
        sourced = []
        while time.monotonic() < deadline:
            sourced = [
                json.loads(body)
                for body in posted
                if body and '"metric"' in body and '"source"' in body
            ]
            if any(body.get("source") == "alt" for body in sourced):
                break
            time.sleep(0.05)
        assert any(
            body.get("metric") == "revenue" and body.get("source") == "alt" for body in sourced
        ), posted
    finally:
        page.remove_listener("request", on_request)
        _stop_server(server, thread, page)


def test_dashboard_default_tile_of_a_shared_query_names_the_default(page, tmp_path_factory):
    """A tile with no `source:` posts source "" so /api/run answers from the
    dashboard default instead of owner-walking a query other tiles run
    elsewhere and refusing (#458)."""
    import duckdb

    root = tmp_path_factory.mktemp("sharedsrc")
    for name, amount in (("a.db", 300.0), ("b.db", 500.0)):
        conn = duckdb.connect(str(root / name))
        conn.execute("CREATE TABLE orders (amount DOUBLE)")
        conn.execute(f"INSERT INTO orders VALUES ({amount})")
        conn.close()
    (root / ".sqldash").mkdir()
    (root / ".sqldash" / "owned.yaml").write_text(
        "title: Owned\n"
        f"source: {{type: duckdb, database: '{root / 'a.db'}'}}\n"
        f"sources:\n  src_b: {{type: duckdb, database: '{root / 'b.db'}'}}\n"
        'queries:\n  shared: "SELECT SUM(amount) AS total FROM orders"\n'
        "tiles:\n"
        "  - {title: Shared default, chart: big_number, query: shared}\n"
        "  - {title: Shared B, chart: big_number, query: shared, source: src_b}\n"
    )
    app = create_app(root / ".sqldash", allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    posted = []

    def on_request(req):
        if "/api/run" in req.url and req.method == "POST" and req.post_data:
            posted.append(json.loads(req.post_data))

    page.on("request", on_request)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/owned", wait_until="load")
        page.wait_for_function(
            "() => document.querySelectorAll('.tile .big-number .value').length === 2",
            timeout=30_000,
        )
        values = page.locator(".tile .big-number .value").all_text_contents()
        assert [v.replace(",", "") for v in values] == ["300", "500"], values
        assert page.locator(".tile .err").count() == 0
        sources = sorted(body.get("source") for body in posted if body.get("query") == "shared")
        assert sources == ["", "src_b"], posted
    finally:
        page.remove_listener("request", on_request)
        _stop_server(server, thread, page)


def test_new_tile_can_add_an_existing_metric(page, tmp_path_factory):
    root = tmp_path_factory.mktemp("addmetric")
    create_demo(root)
    app = create_app(root / ".sqldash", allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        js_errors = []

        def on_pageerror(err):
            js_errors.append(str(err))

        page.on("pageerror", on_pageerror)
        page.goto(f"http://127.0.0.1:{port}/d/demo/query", wait_until="load")
        page.locator('#mode-toggle [data-mode="metric"]').click()
        page.wait_for_selector("#metric-pane:not([hidden])", timeout=10_000)
        page.wait_for_function(
            """() => {
              const s = document.getElementById('metric-picker');
              return s && [...s.options].some(o => o.value === 'revenue');
            }""",
            timeout=30_000,
        )
        page.evaluate(
            """() => {
              const s = document.getElementById('metric-picker');
              s.value = '';
              s.dispatchEvent(new Event('change', { bubbles: true }));
              s.value = 'revenue';
              s.dispatchEvent(new Event('change', { bubbles: true }));
            }"""
        )
        page.locator("#qb-title").fill("Revenue from UI")
        page.wait_for_function(
            "() => !document.getElementById('qb-add').disabled",
            timeout=5_000,
        )
        with page.expect_response(
            lambda r: "/tiles" in r.url and r.request.method == "POST",
            timeout=15_000,
        ) as saved:
            page.locator("#qb-add").click()
        assert saved.value.status == 200, saved.value.status
        page.wait_for_function("() => !document.getElementById('qb-another').hidden")
        assert js_errors == [], js_errors
        path = root / ".sqldash" / "demo.yaml"
        text = ""
        dash = None
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            text = path.read_text()
            if "Revenue from UI" in text:
                try:
                    dash, _, _ = DashboardStore(root / ".sqldash").load("demo")
                    break
                except Exception:
                    time.sleep(0.05)
                    continue
            time.sleep(0.05)
        assert dash is not None, text
        tail = text.split("Revenue from UI", 1)[1]
        cut = tail.find("\n  - ")
        block = tail if cut < 0 else tail[:cut]
        assert "metric: revenue" in block, block
        assert "sql:" not in block, block
        tile = next(t for t in dash.tiles if t.title == "Revenue from UI")
        assert tile.metric is not None
        assert tile.metric.name == "revenue"
        assert tile.metric.compare is None
        assert tile.sql is None
        assert tile.query is None
    finally:
        page.remove_listener("pageerror", on_pageerror)
        _stop_server(server, thread, page)


def test_new_tile_cannot_add_a_metric_missing_from_the_catalog(page, tmp_path_factory):
    """A new tile with a picker value the catalog does not have used to enable
    Add and save a bare name — no dimensions, no grain, big_number of the
    wrong result."""
    root = tmp_path_factory.mktemp("mghost")
    _metric_edit_project(root)
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/d/query", wait_until="load")
        page.locator('#mode-toggle [data-mode="metric"]').click()
        page.wait_for_function(
            """() => {
              const s = document.getElementById('metric-picker');
              return s && [...s.options].some(o => o.value === 'revenue');
            }""",
            timeout=30_000,
        )
        page.evaluate(
            """() => {
              const s = document.getElementById('metric-picker');
              s.add(new Option('ghost', 'ghost'));
              s.value = 'ghost';
              s.dispatchEvent(new Event('change', { bubbles: true }));
            }"""
        )
        assert page.locator("#qb-add").is_disabled()
    finally:
        _stop_server(server, thread, page)


def test_switching_to_metric_mode_drops_a_sql_preview(page, tmp_path_factory):
    """SQL Run left builder.result set, so Metric mode inherited that result
    and a later dim change rewrote the chart from it."""
    root = tmp_path_factory.mktemp("msqlm")
    _metric_edit_project(root)
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/d/query", wait_until="load")
        page.locator("#run-btn").click()
        page.wait_for_selector("#results-body table", timeout=15_000)
        page.locator('#mode-toggle [data-mode="metric"]').click()
        page.wait_for_selector("#metric-pane:not([hidden])", timeout=10_000)
        assert page.locator("#results-body table").count() == 0
        page.wait_for_function(
            """() => {
              const s = document.getElementById('metric-picker');
              return s && [...s.options].some(o => o.value === 'revenue');
            }""",
            timeout=30_000,
        )
        page.evaluate(
            """() => {
              const s = document.getElementById('metric-picker');
              s.value = 'revenue';
              s.dispatchEvent(new Event('change', { bubbles: true }));
            }"""
        )
        assert page.locator("#qb-type .seg-btn.active").inner_text() == "Number"
    finally:
        _stop_server(server, thread, page)


def test_adding_a_dimensioned_metric_without_a_run_saves_a_table(page, tmp_path_factory):
    """Add is enabled without Run. defaultChartSpec used to ignore dimensions
    and write big_number, which renders rows[0] of a multi-row result."""
    root = tmp_path_factory.mktemp("adddim")
    _metric_edit_project(root)
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/d/query", wait_until="load")
        page.locator('#mode-toggle [data-mode="metric"]').click()
        page.wait_for_function(
            """() => {
              const s = document.getElementById('metric-picker');
              return s && [...s.options].some(o => o.value === 'revenue');
            }""",
            timeout=30_000,
        )
        page.evaluate(
            """() => {
              const s = document.getElementById('metric-picker');
              s.value = 'revenue';
              s.dispatchEvent(new Event('change', { bubbles: true }));
            }"""
        )
        page.wait_for_function(
            """() => document.querySelector('#metric-dims input[value="region"]')""",
            timeout=10_000,
        )
        page.evaluate(
            """() => {
              const box = document.querySelector('#metric-dims input[value="region"]');
              box.checked = true;
              box.dispatchEvent(new Event('change', { bubbles: true }));
            }"""
        )
        assert page.locator("#qb-type .seg-btn.active").inner_text() == "Table"
        page.locator("#qb-title").fill("By region")
        with page.expect_response(
            lambda r: "/tiles" in r.url and r.request.method == "POST",
            timeout=15_000,
        ) as saved:
            page.locator("#qb-add").click()
        assert saved.value.status == 200, saved.value.status
        page.wait_for_function("() => !document.getElementById('qb-another').hidden")
        text = (root / "d.yaml").read_text()
        block = text.split("By region", 1)[1]
        cut = block.find("\n  - ")
        block = block if cut < 0 else block[:cut]
        assert "dimensions:" in block, block
        assert "region" in block, block
        assert "chart: table" in block, block
        assert "chart: {" not in block, block
        assert "big_number" not in block, block
    finally:
        _stop_server(server, thread, page)


_METRIC_EDIT_YAML = (
    "source: {type: duckdb, database: ':memory:'}\n"
    "relations:\n"
    "  t: {sql: \"SELECT 1 AS n, DATE '2026-01-01' AS d, 'us' AS region\"}\n"
    "metrics:\n"
    "  revenue:\n"
    "    relation: t\n"
    "    expr: SUM(n)\n"
    "    time_dimension: {name: d, grain: day}\n"
    "    dimensions: [{name: region}]\n"
    "  order_count:\n"
    "    relation: t\n"
    "    expr: COUNT(*)\n"
    "    time_dimension: {name: d, grain: day}\n"
    "    dimensions: [{name: region}]\n"
)


def _metric_edit_project(root):
    (root / "metrics.yaml").write_text(_METRIC_EDIT_YAML)
    (root / "d.yaml").write_text(
        "title: M\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  - title: Total\n"
        "    metric: {name: revenue, compare: previous_period}\n"
        "    chart: {type: line, group_by: region}\n"
        "    size: 6x4\n"
    )


def test_a_dashboard_name_that_needs_encoding_still_saves_from_the_page(page, tmp_path_factory):
    """The page built `/api/dashboards/${name}/...` from the raw name, so a
    hand-written `we ird?q#h%.yaml` sent `?q#h%/meta` as a query string and a
    fragment: the title, tile and delete saves all missed the dashboard."""
    root = tmp_path_factory.mktemp("encoded-name")
    name = "we ird?q#h%"
    path = root / f"{name}.yaml"
    path.write_text(
        "title: Weird\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  - id: 't?1'\n"
        "    title: Rows\n"
        "    chart: table\n"
        "    size: 6x4\n"
        '    sql: "SELECT 1 AS n"\n'
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    base = f"http://127.0.0.1:{port}/d/{quote(name)}"
    try:
        page.goto(f"{base}?edit=1", wait_until="load")
        page.wait_for_selector(".tile", timeout=30_000)
        page.locator("#dash-title").click()
        page.keyboard.press("End")
        page.keyboard.type(" renamed")
        with page.expect_response(lambda r: r.request.method == "PATCH") as meta:
            page.locator("#dash-title").evaluate("el => el.blur()")
        assert meta.value.status == 200, meta.value.url
        assert "title: Weird renamed" in path.read_text()

        page.locator(".tile", has_text="Rows").hover()
        page.locator('.wa-btn[data-action="edit"]').click()
        page.wait_for_url(re.compile(r"/query\?tile="), timeout=15_000)
        page.wait_for_selector("#qb-add:not([disabled])", timeout=30_000)
        with page.expect_response(
            lambda r: "/tiles/" in r.url and r.request.method == "PUT", timeout=15_000
        ) as saved:
            page.locator("#qb-add").click()
        assert saved.value.status == 200, saved.value.url
        page.wait_for_url(re.compile(r"\?edit=1$"), timeout=15_000)
        assert unquote(urlparse(page.url).path) == f"/d/{name}"

        page.wait_for_selector(".tile", timeout=30_000)
        page.once("dialog", lambda dialog: dialog.accept())
        page.locator(".tile", has_text="Rows").hover()
        with page.expect_response(lambda r: r.request.method == "DELETE") as deleted:
            page.locator('.tile .wa-btn[data-action="delete"]').click()
        assert deleted.value.status == 200, deleted.value.url
        assert "t?1" not in path.read_text()
    finally:
        _stop_server(server, thread, page)


def test_saving_a_metric_tile_keeps_its_authored_chart(page, tmp_path_factory):
    """A no-op save used to rewrite chart: line to big_number because the
    builder has no result until Run."""
    root = tmp_path_factory.mktemp("mchart")
    _metric_edit_project(root)
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/d?edit=1", wait_until="load")
        page.wait_for_selector(".tile", timeout=30_000)
        page.locator(".tile", has_text="Total").hover()
        page.locator('.wa-btn[data-action="edit"]').click()
        page.wait_for_url(re.compile(r"/d/d/query\?tile="), timeout=15_000)
        page.wait_for_selector("#metric-pane:not([hidden])", timeout=10_000)
        with page.expect_response(
            lambda r: "/tiles/" in r.url and r.request.method == "PUT",
            timeout=15_000,
        ) as saved:
            page.locator("#qb-add").click()
        assert saved.value.status == 200, saved.value.status
        page.wait_for_url(re.compile(r"/d/d/?(\?.*)?$"), timeout=15_000)
        text = (root / "d.yaml").read_text()
        assert "type: line" in text, text
        assert "group_by: region" in text, text
        assert "previous_period" in text, text
        assert "big_number" not in text, text
    finally:
        _stop_server(server, thread, page)


def test_toggling_a_dimension_keeps_the_authored_chart(page, tmp_path_factory):
    """A dim/grain change used to reset builder to defaultChartSpec, so an
    authored line/group_by became area (or big_number) on save."""
    root = tmp_path_factory.mktemp("mdim")
    _metric_edit_project(root)
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/d?edit=1", wait_until="load")
        page.wait_for_selector(".tile", timeout=30_000)
        page.locator(".tile", has_text="Total").hover()
        page.locator('.wa-btn[data-action="edit"]').click()
        page.wait_for_url(re.compile(r"/d/d/query\?tile="), timeout=15_000)
        page.wait_for_function(
            """() => document.querySelector('#metric-dims input[value="region"]')""",
            timeout=30_000,
        )
        assert page.locator("#qb-type .seg-btn.active").inner_text() == "Line"
        page.evaluate(
            """() => {
              const box = document.querySelector('#metric-dims input[value="region"]');
              box.checked = true;
              box.dispatchEvent(new Event('change', { bubbles: true }));
            }"""
        )
        assert page.locator("#qb-type .seg-btn.active").inner_text() == "Line"
        with page.expect_response(
            lambda r: "/tiles/" in r.url and r.request.method == "PUT",
            timeout=15_000,
        ) as saved:
            page.locator("#qb-add").click()
        assert saved.value.status == 200, saved.value.status
        page.wait_for_url(re.compile(r"/d/d/?(\?.*)?$"), timeout=15_000)
        text = (root / "d.yaml").read_text()
        assert "type: line" in text, text
        assert "group_by: region" in text, text
        assert "big_number" not in text, text
        dash, _, _ = DashboardStore(root).load("d")
        assert dash.tiles[0].metric.dimensions == ["region"]
    finally:
        _stop_server(server, thread, page)


_DIMENSION_ORDER_METRICS = (
    "source: {type: duckdb, database: ':memory:'}\n"
    "relations:\n"
    "  t: {sql: \"SELECT 1 AS n, 'us' AS region, 'home' AS category, 'mug' AS product\"}\n"
    "metrics:\n"
    "  revenue:\n"
    "    relation: t\n"
    "    expr: SUM(n)\n"
    "    dimensions: [{name: region}, {name: category}, {name: product}]\n"
)


def _toggle_dimension(frame, name, checked):
    frame.evaluate(
        """([name, checked]) => {
          const box = document.querySelector(`#metric-dims input[value="${name}"]`);
          box.checked = checked;
          box.dispatchEvent(new Event('change', { bubbles: true }));
        }""",
        [name, checked],
    )


def test_saving_a_metric_tile_keeps_its_authored_dimension_order(page, tmp_path_factory):
    """Dimensions came back in catalog order, so a no-op save rewrote
    [product, category] to [category, product] and swapped the columns (#544)."""
    root = tmp_path_factory.mktemp("mdimorder")
    (root / "metrics.yaml").write_text(_DIMENSION_ORDER_METRICS)
    authored = (
        "title: M\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  - id: products\n"
        "    title: Products\n"
        "    metric: {name: revenue, dimensions: [product, category]}\n"
        "    chart: table\n"
        "    size: 8x3\n"
    )
    (root / "d.yaml").write_text(authored)
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    base = f"http://127.0.0.1:{port}"

    def save(toggles=()):
        page.goto(f"{base}/d/d/query?tile=products", wait_until="load")
        page.wait_for_selector('#metric-dims input[value="product"]:checked', timeout=30_000)
        for name, checked in toggles:
            _toggle_dimension(page, name, checked)
        with page.expect_response(
            lambda r: "/tiles/" in r.url and r.request.method == "PUT",
            timeout=15_000,
        ) as saved:
            page.locator("#qb-add").click()
        assert saved.value.status == 200, saved.value.status
        page.wait_for_url(re.compile(r"/d/d/?(\?.*)?$"), timeout=15_000)
        return (root / "d.yaml").read_text()

    try:
        assert save() == authored
        page.wait_for_selector('.tile[data-tile-id="products"] th', timeout=30_000)
        headers = page.locator('.tile[data-tile-id="products"] th').all_inner_texts()
        assert [h.strip().lower() for h in headers] == ["product", "category", "revenue"]
        text = save([("region", True), ("category", False)])
        assert "dimensions: [product, region]" in text, text
    finally:
        _stop_server(server, thread, page)


def test_workspace_restore_keeps_the_ticked_dimension_order(page, tmp_path_factory):
    root = tmp_path_factory.mktemp("wsdimorder")
    (root / "metrics.yaml").write_text(_DIMENSION_ORDER_METRICS)
    (root / "d.yaml").write_text(
        "title: M\nsource: {type: duckdb, database: ':memory:'}\ntiles: []\n"
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/d/workspace", wait_until="networkidle")
        page.locator("#metric-browser > details > summary").click()
        page.locator('[data-metric="revenue"]').click()
        frame = page.frames[-1]
        frame.locator(".workspace-editor[data-ready=true]").wait_for()
        frame.locator('#metric-dims input[value="product"]').check()
        frame.locator('#metric-dims input[value="category"]').check()
        page.reload(wait_until="networkidle")
        frame = page.frames[-1]
        frame.locator(".workspace-editor[data-ready=true]").wait_for()
        assert frame.locator('#metric-dims input[value="category"]').is_checked()
        with page.expect_request(
            lambda r: r.method == "POST" and "metric" in (r.post_data or ""),
            timeout=15_000,
        ) as ran:
            frame.locator("#run-btn").click()
        assert json.loads(ran.value.post_data)["dimensions"] == ["product", "category"]
    finally:
        _stop_server(server, thread, page)


@pytest.mark.parametrize("width", [1440, 1280])
def test_workspace_chart_types_each_fit_their_label(page, tmp_path_factory, width):
    """Seven equal-width buttons were narrower than "Number" (and "Scatter" at
    1280), so the label ran under the next button and the active "Table" pill
    painted over it. With more types than one row holds they form an even
    grid, never a ragged wrap with one button stretched across a row."""
    root = tmp_path_factory.mktemp("wscharttypes")
    (root / "d.yaml").write_text(
        "title: M\nsource: {type: duckdb, database: ':memory:'}\ntiles: []\n"
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.set_viewport_size({"width": width, "height": 900})
        page.goto(f"http://127.0.0.1:{port}/d/d/workspace", wait_until="networkidle")
        frame = page.frames[-1]
        frame.locator(".workspace-editor[data-ready=true]").wait_for()
        frame.locator("#qb-type .seg-btn", has_text="Table").click()
        clipped = frame.eval_on_selector_all(
            "#qb-type .seg-btn",
            "els => els.filter(e => e.scrollWidth > e.clientWidth).map(e => e.textContent)",
        )
        assert clipped == []
        rows = frame.eval_on_selector_all(
            "#qb-type .seg-btn", "els => new Set(els.map(e => e.offsetTop)).size"
        )
        assert rows <= 3
        widths = frame.eval_on_selector_all(
            "#qb-type .seg-btn", "els => els.map(e => e.getBoundingClientRect().width)"
        )
        assert max(widths) - min(widths) < 1, widths
        assert len(widths) % rows == 0, (len(widths), rows)
    finally:
        _stop_server(server, thread, page)


def test_editing_switch_then_a_dimension_does_not_save_big_number(page, tmp_path_factory):
    """The authored-chart guard also fired after switching to a different
    metric, so checking a dim saved big_number of rows[0]."""
    root = tmp_path_factory.mktemp("mswitchdim")
    _metric_edit_project(root)
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/d?edit=1", wait_until="load")
        page.wait_for_selector(".tile", timeout=30_000)
        page.locator(".tile", has_text="Total").hover()
        page.locator('.wa-btn[data-action="edit"]').click()
        page.wait_for_url(re.compile(r"/d/d/query\?tile="), timeout=15_000)
        page.wait_for_function(
            """() => {
              const s = document.getElementById('metric-picker');
              return s && [...s.options].some(o => o.value === 'order_count');
            }""",
            timeout=30_000,
        )
        page.evaluate(
            """() => {
              const s = document.getElementById('metric-picker');
              s.value = 'order_count';
              s.dispatchEvent(new Event('change', { bubbles: true }));
            }"""
        )
        page.wait_for_function(
            """() => document.querySelector('#metric-dims input[value="region"]')""",
            timeout=10_000,
        )
        page.evaluate(
            """() => {
              const box = document.querySelector('#metric-dims input[value="region"]');
              box.checked = true;
              box.dispatchEvent(new Event('change', { bubbles: true }));
            }"""
        )
        assert page.locator("#qb-type .seg-btn.active").inner_text() == "Table"
        with page.expect_response(
            lambda r: "/tiles/" in r.url and r.request.method == "PUT",
            timeout=15_000,
        ) as saved:
            page.locator("#qb-add").click()
        assert saved.value.status == 200, saved.value.status
        page.wait_for_url(re.compile(r"/d/d/?(\?.*)?$"), timeout=15_000)
        dash, _, _ = DashboardStore(root).load("d")
        tile = dash.tiles[0]
        assert tile.metric.name == "order_count"
        assert tile.metric.dimensions == ["region"]
        assert tile.chart.type == "table"
    finally:
        _stop_server(server, thread, page)


def test_checking_a_dim_during_a_run_does_not_save_big_number(page, tmp_path_factory):
    """builder.result is null for the whole poll, so the authored-chart guard
    swallowed a dim change made while Run was in flight."""
    root = tmp_path_factory.mktemp("mdimrun")
    _metric_edit_project(root)
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    delayed = {"n": 0}

    @app.middleware("http")
    async def delay_polls(request, call_next):
        if delayed["n"] < 1 and request.url.path.startswith("/api/executions/"):
            delayed["n"] += 1
            await asyncio.sleep(1.5)
        return await call_next(request)

    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/d?edit=1", wait_until="load")
        page.wait_for_selector(".tile", timeout=30_000)
        page.locator(".tile", has_text="Total").hover()
        page.locator('.wa-btn[data-action="edit"]').click()
        page.wait_for_url(re.compile(r"/d/d/query\?tile="), timeout=15_000)
        page.wait_for_function(
            """() => document.querySelector('#metric-dims input[value="region"]')""",
            timeout=30_000,
        )
        page.locator("#run-btn").click()
        page.wait_for_selector(".status-pill.running", timeout=10_000)
        page.evaluate(
            """() => {
              const box = document.querySelector('#metric-dims input[value="region"]');
              box.checked = true;
              box.dispatchEvent(new Event('change', { bubbles: true }));
            }"""
        )
        assert page.locator("#qb-type .seg-btn.active").inner_text() == "Table"
        with page.expect_response(
            lambda r: "/tiles/" in r.url and r.request.method == "PUT",
            timeout=15_000,
        ) as saved:
            page.locator("#qb-add").click()
        assert saved.value.status == 200, saved.value.status
        dash, _, _ = DashboardStore(root).load("d")
        tile = dash.tiles[0]
        assert tile.metric.dimensions == ["region"]
        assert tile.chart.type == "table"
    finally:
        _stop_server(server, thread, page)


def test_running_another_metric_then_switching_back_keeps_the_authored_chart(
    page, tmp_path_factory
):
    """After Run on a different metric, builder.result is set, so switching
    back skipped the authored-chart restore and wrote big_number."""
    root = tmp_path_factory.mktemp("mback")
    _metric_edit_project(root)
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/d?edit=1", wait_until="load")
        page.wait_for_selector(".tile", timeout=30_000)
        page.locator(".tile", has_text="Total").hover()
        page.locator('.wa-btn[data-action="edit"]').click()
        page.wait_for_url(re.compile(r"/d/d/query\?tile="), timeout=15_000)
        page.wait_for_function(
            """() => {
              const s = document.getElementById('metric-picker');
              return s && [...s.options].some(o => o.value === 'order_count');
            }""",
            timeout=30_000,
        )
        page.evaluate(
            """() => {
              const s = document.getElementById('metric-picker');
              s.value = 'order_count';
              s.dispatchEvent(new Event('change', { bubbles: true }));
            }"""
        )
        page.locator("#run-btn").click()
        page.wait_for_selector("#results-body table", timeout=15_000)
        page.evaluate(
            """() => {
              const s = document.getElementById('metric-picker');
              s.value = 'revenue';
              s.dispatchEvent(new Event('change', { bubbles: true }));
            }"""
        )
        assert page.locator("#qb-type .seg-btn.active").inner_text() == "Line"
        with page.expect_response(
            lambda r: "/tiles/" in r.url and r.request.method == "PUT",
            timeout=15_000,
        ) as saved:
            page.locator("#qb-add").click()
        assert saved.value.status == 200, saved.value.status
        text = (root / "d.yaml").read_text()
        assert "type: line" in text, text
        assert "group_by: region" in text, text
        assert "big_number" not in text, text
    finally:
        _stop_server(server, thread, page)


def test_changing_grain_after_a_run_resets_the_chart_type(page, tmp_path_factory):
    """A run infers big_number; setting a grain used to keep that spec and save
    a period-total chart against a multi-row result."""
    root = tmp_path_factory.mktemp("mgrain")
    _metric_edit_project(root)
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/d/query", wait_until="load")
        page.locator('#mode-toggle [data-mode="metric"]').click()
        page.wait_for_function(
            """() => {
              const s = document.getElementById('metric-picker');
              return s && [...s.options].some(o => o.value === 'revenue');
            }""",
            timeout=30_000,
        )
        page.evaluate(
            """() => {
              const s = document.getElementById('metric-picker');
              s.value = 'revenue';
              s.dispatchEvent(new Event('change', { bubbles: true }));
            }"""
        )
        with page.expect_response(
            lambda r: "/api/run" in r.url and r.request.method == "POST",
            timeout=15_000,
        ):
            page.locator("#run-btn").click()
        page.wait_for_selector("#results-body table", timeout=15_000)
        assert page.locator("#qb-type .seg-btn.active").inner_text() == "Number"
        page.evaluate(
            """() => {
              const s = document.getElementById('metric-grain');
              s.value = 'day';
              s.dispatchEvent(new Event('change', { bubbles: true }));
            }"""
        )
        assert page.locator("#qb-type .seg-btn.active").inner_text() == "Area"
        page.locator("#qb-title").fill("Grained")
        with page.expect_response(
            lambda r: "/tiles" in r.url and r.request.method == "POST",
            timeout=15_000,
        ) as saved:
            page.locator("#qb-add").click()
        assert saved.value.status == 200, saved.value.status
        page.wait_for_function("() => !document.getElementById('qb-another').hidden")
        text = (root / "d.yaml").read_text()
        block = text.split("Grained", 1)[1]
        cut = block.find("\n  - ")
        block = block if cut < 0 else block[:cut]
        assert "grain: day" in block, block
        assert "chart: area" in block, block
        assert "chart: {" not in block, block
        assert "big_number" not in block, block
    finally:
        _stop_server(server, thread, page)


def test_editing_a_missing_metric_keeps_the_picker_value(page, tmp_path_factory):
    """A metric the catalog has never heard of renders no dimension boxes and
    no grain field, so a no-op save must fall back to the authored ref rather
    than rebuilding one from the empty DOM."""
    root = tmp_path_factory.mktemp("mgone")
    (root / "metrics.yaml").write_text(_METRIC_EDIT_YAML)
    (root / "d.yaml").write_text(
        "title: M\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  - title: Total\n"
        "    metric: {name: gone_metric, dimensions: [region], grain: day}\n"
        "    size: 6x4\n"
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/d?edit=1", wait_until="load")
        page.wait_for_selector(".tile", timeout=30_000)
        page.locator(".tile", has_text="Total").hover()
        page.locator('.wa-btn[data-action="edit"]').click()
        page.wait_for_url(re.compile(r"/d/d/query\?tile="), timeout=15_000)
        page.wait_for_function(
            "() => document.getElementById('metric-picker')?.value === 'gone_metric'",
            timeout=30_000,
        )
        with page.expect_response(
            lambda r: "/tiles/" in r.url and r.request.method == "PUT",
            timeout=15_000,
        ) as saved:
            page.locator("#qb-add").click()
        assert saved.value.status == 200, saved.value.status
        text = (root / "d.yaml").read_text()
        assert "gone_metric" in text, text
        assert "region" in text, text
        assert "grain: day" in text, text
    finally:
        _stop_server(server, thread, page)


def test_saving_when_the_catalog_fails_keeps_the_authored_ref(page, tmp_path_factory):
    """If /api/metrics never succeeds the picker is rebuilt from an empty
    catalog, so a known metric also has no dimension boxes and no grain. The
    save must still round-trip the ref the file was authored with."""
    root = tmp_path_factory.mktemp("mcatfail")
    (root / "metrics.yaml").write_text(_METRIC_EDIT_YAML)
    (root / "d.yaml").write_text(
        "title: M\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  - title: Total\n"
        "    metric: {name: revenue, dimensions: [region], grain: day}\n"
        "    size: 6x4\n"
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    ctx = page.context.browser.new_context(
        viewport={"width": 1400, "height": 1000}, color_scheme="dark"
    )
    tab = ctx.new_page()
    tab.set_default_timeout(30_000)
    tab.set_default_navigation_timeout(30_000)
    try:
        tab.goto(f"http://127.0.0.1:{port}/d/d?edit=1", wait_until="load")
        tab.wait_for_selector(".tile", timeout=30_000)
        (root / "metrics.yaml").write_text("[]")
        tab.locator(".tile", has_text="Total").hover()
        tab.locator('.wa-btn[data-action="edit"]').click()
        tab.wait_for_url(re.compile(r"/d/d/query\?tile="), timeout=15_000)
        tab.wait_for_function(
            "() => document.getElementById('metric-picker')?.value === 'revenue'",
            timeout=30_000,
        )
        assert tab.locator("#metric-dims input").count() == 0
        with tab.expect_response(
            lambda r: "/tiles/" in r.url and r.request.method == "PUT",
            timeout=15_000,
        ) as saved:
            tab.locator("#qb-add").click()
        assert saved.value.status == 200, saved.value.status
        text = (root / "d.yaml").read_text()
        assert "region" in text, text
        assert "grain: day" in text, text
    finally:
        ctx.close()
        _stop_server(server, thread, page)


def test_switching_metric_clears_the_previous_results(page, tmp_path_factory):
    """The old rows stayed in the table after picking a different metric, so a
    user could save believing the preview belonged to the metric they picked."""
    root = tmp_path_factory.mktemp("mstale")
    (root / "metrics.yaml").write_text(_METRIC_EDIT_YAML)
    (root / "d.yaml").write_text(
        "title: M\nsource: {type: duckdb, database: ':memory:'}\ntiles: []\n"
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/d/query", wait_until="load")
        page.locator('#mode-toggle [data-mode="metric"]').click()
        page.wait_for_selector("#metric-pane:not([hidden])", timeout=10_000)
        page.wait_for_function(
            """() => {
              const s = document.getElementById('metric-picker');
              return s && [...s.options].some(o => o.value === 'order_count');
            }""",
            timeout=30_000,
        )
        page.evaluate(
            """() => {
              const s = document.getElementById('metric-picker');
              s.value = 'revenue';
              s.dispatchEvent(new Event('change', { bubbles: true }));
            }"""
        )
        page.locator("#run-btn").click()
        page.wait_for_selector("#results-body table td", timeout=30_000)
        assert page.locator("#results-body table td").count() > 0
        page.evaluate(
            """() => {
              const s = document.getElementById('metric-picker');
              s.value = 'order_count';
              s.dispatchEvent(new Event('change', { bubbles: true }));
            }"""
        )
        page.wait_for_function(
            "() => document.querySelectorAll('#results-body table td').length === 0",
            timeout=10_000,
        )
        assert "row" not in page.locator("#results-meta").inner_text().lower()
    finally:
        _stop_server(server, thread, page)


def test_switching_metric_during_a_run_does_not_paint_stale_results(page, tmp_path_factory):
    """clearResults nulls currentExecution, but run() still awaited the old
    poll and painted those rows under the new picker value."""
    root = tmp_path_factory.mktemp("mrace")
    _metric_edit_project(root)
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])

    delayed = {"n": 0}

    @app.middleware("http")
    async def delay_polls(request, call_next):
        if delayed["n"] < 1 and request.url.path.startswith("/api/executions/"):
            delayed["n"] += 1
            await asyncio.sleep(1.5)
        return await call_next(request)

    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/d/query", wait_until="load")
        page.locator('#mode-toggle [data-mode="metric"]').click()
        page.wait_for_function(
            """() => {
              const s = document.getElementById('metric-picker');
              return s && [...s.options].some(o => o.value === 'order_count');
            }""",
            timeout=30_000,
        )
        page.evaluate(
            """() => {
              const s = document.getElementById('metric-picker');
              s.value = 'revenue';
              s.dispatchEvent(new Event('change', { bubbles: true }));
            }"""
        )
        page.locator("#run-btn").click()
        page.wait_for_selector(".status-pill.running", timeout=10_000)
        page.evaluate(
            """() => {
              const s = document.getElementById('metric-picker');
              s.value = 'order_count';
              s.dispatchEvent(new Event('change', { bubbles: true }));
            }"""
        )
        page.wait_for_timeout(2500)
        assert page.locator("#metric-picker").input_value() == "order_count"
        assert page.locator("#results-body table").count() == 0
        assert "row" not in page.locator("#results-meta").inner_text().lower()
    finally:
        _stop_server(server, thread, page)


def test_switching_back_to_the_authored_metric_keeps_the_chart(page, tmp_path_factory):
    """The picker handler forced defaultChartSpec even when the selection
    landed back on the tile's metric, so a round-trip plus Save rewrote
    line/group_by to big_number."""
    root = tmp_path_factory.mktemp("mround")
    _metric_edit_project(root)
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/d?edit=1", wait_until="load")
        page.wait_for_selector(".tile", timeout=30_000)
        page.locator(".tile", has_text="Total").hover()
        page.locator('.wa-btn[data-action="edit"]').click()
        page.wait_for_url(re.compile(r"/d/d/query\?tile="), timeout=15_000)
        page.wait_for_function(
            """() => {
              const s = document.getElementById('metric-picker');
              return s && s.value === 'revenue'
                && [...s.options].some(o => o.value === 'order_count');
            }""",
            timeout=30_000,
        )
        assert page.locator("#qb-type .seg-btn.active").inner_text() == "Line"
        page.evaluate(
            """() => {
              const s = document.getElementById('metric-picker');
              s.value = 'order_count';
              s.dispatchEvent(new Event('change', { bubbles: true }));
              s.value = 'revenue';
              s.dispatchEvent(new Event('change', { bubbles: true }));
            }"""
        )
        assert page.locator("#qb-type .seg-btn.active").inner_text() == "Line"
        with page.expect_response(
            lambda r: "/tiles/" in r.url and r.request.method == "PUT",
            timeout=15_000,
        ) as saved:
            page.locator("#qb-add").click()
        assert saved.value.status == 200, saved.value.status
        page.wait_for_url(re.compile(r"/d/d/?(\?.*)?$"), timeout=15_000)
        text = (root / "d.yaml").read_text()
        assert "type: line" in text, text
        assert "group_by: region" in text, text
        assert "previous_period" in text, text
        assert "big_number" not in text, text
    finally:
        _stop_server(server, thread, page)


def test_switching_metric_does_not_keep_the_old_compare(page, tmp_path_factory):
    root = tmp_path_factory.mktemp("mswitch")
    _metric_edit_project(root)
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/d?edit=1", wait_until="load")
        page.wait_for_selector(".tile", timeout=30_000)
        page.locator(".tile", has_text="Total").hover()
        page.locator('.wa-btn[data-action="edit"]').click()
        page.wait_for_url(re.compile(r"/d/d/query\?tile="), timeout=15_000)
        page.wait_for_function(
            """() => {
              const s = document.getElementById('metric-picker');
              return s && [...s.options].some(o => o.value === 'order_count');
            }""",
            timeout=30_000,
        )
        page.evaluate(
            """() => {
              const s = document.getElementById('metric-picker');
              s.value = 'order_count';
              s.dispatchEvent(new Event('change', { bubbles: true }));
            }"""
        )
        with page.expect_response(
            lambda r: "/tiles/" in r.url and r.request.method == "PUT",
            timeout=15_000,
        ) as saved:
            page.locator("#qb-add").click()
        assert saved.value.status == 200, saved.value.status
        page.wait_for_url(re.compile(r"/d/d/?(\?.*)?$"), timeout=15_000)
        dash, _, _ = DashboardStore(root).load("d")
        tile = dash.tiles[0]
        assert tile.metric.name == "order_count"
        assert tile.metric.compare is None
        assert tile.metric.dimensions == []
    finally:
        _stop_server(server, thread, page)


def test_two_blank_title_tiles_stay_distinct_and_render_no_empty_header(page, tmp_path_factory):
    """A blank Add-to-dashboard title must not render an empty header (#295),
    and two blank adds must not collapse onto one id (the 'untitled' slug
    regression the review caught)."""
    root = tmp_path_factory.mktemp("blank")
    create_demo(root)
    app = create_app(root / ".sqldash", allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/demo/query", wait_until="networkidle")
        for body in ["first blank body", "second blank body"]:
            page.locator('#mode-toggle [data-mode="text"]').click()
            page.fill("#text-markdown", body)
            with page.expect_response(lambda r: "/tiles" in r.url and r.request.method == "POST"):
                page.locator("#qb-add").click()
            page.wait_for_function("() => !document.getElementById('qb-another').hidden")
            page.locator("#qb-another").click()
        page.goto(f"http://127.0.0.1:{port}/d/demo", wait_until="networkidle")
        page.wait_for_selector(".grid-stack-item", timeout=30_000)
        empty = page.evaluate(
            "[...document.querySelectorAll('.tile-head h3')]"
            ".filter(h => !h.textContent.trim()).length"
        )
        assert empty == 0
        bodies = page.evaluate(
            "[...document.querySelectorAll('.tile-text .tile-body')].map(e => e.textContent.trim())"
        )
        assert any("first blank body" in b for b in bodies)
        assert any("second blank body" in b for b in bodies)
        ids = page.evaluate(
            "[...document.querySelectorAll('.grid-stack-item')].map(e => e.getAttribute('gs-id'))"
        )
        assert len(ids) == len(set(ids))
    finally:
        _stop_server(server, thread, page)


def test_adding_a_chart_from_the_query_page_pins_no_inferred_encodings(page, tmp_path_factory):
    """ "Add to dashboard" used to write `chart: {type: bar, x: a, y: [b]}` from
    that first run's columns, so a later alias rename rendered an empty chart
    with no error (#357). Encodings the author never picked stay inferred at
    render time, the way the preview inferred them."""
    root = tmp_path_factory.mktemp("encodings")
    create_demo(root)
    app = create_app(root / ".sqldash", allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/demo/query", wait_until="load")
        page.wait_for_selector("#qb-preview", timeout=30_000)
        page.fill("#qb-title", "Enc Test")
        page.locator(".ace_content").first.click()
        page.keyboard.press("ControlOrMeta+A")
        page.keyboard.type("SELECT region AS a, SUM(amount) AS b FROM orders GROUP BY 1")
        page.locator("#run-btn").click()
        page.wait_for_function("() => !document.getElementById('qb-add').disabled", timeout=30_000)
        page.locator("#qb-type button", has_text="Bar").first.click()
        # The preview still inferred the axes for the author to see.
        x_pick = page.locator('#qb-encoding [data-spec="x"]')
        assert x_pick.input_value() == "a"
        page.locator("#qb-add").click()
        page.wait_for_function("() => !document.getElementById('qb-another').hidden")
    finally:
        _stop_server(server, thread, page)
    text = (root / ".sqldash" / "demo.yaml").read_text()
    block = text.split("Enc Test", 1)[1].split("queries:", 1)[0]
    assert "chart: bar\n" in block, block
    assert "chart: {" not in block, block


def test_a_pinned_encoding_the_result_lacks_falls_back_to_inference(page, tmp_path_factory):
    """`chart: {type: bar, x: a, y: [b]}` over a query that now returns `c`
    plotted three nulls. The stale name is dropped and the axis inferred from
    the columns that exist (#357)."""
    root = tmp_path_factory.mktemp("stale")
    create_demo(root)
    (root / ".sqldash" / "d.yaml").write_text(
        "title: Stale\n"
        "source: {type: duckdb, attach_files: true}\n"
        "tiles:\n"
        "  - title: Enc\n"
        "    chart: {type: bar, x: a, y: [b]}\n"
        "    sql: SELECT region AS a, SUM(amount) AS c FROM orders GROUP BY 1 ORDER BY 1\n"
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    warnings = []

    def handler(message):
        if message.type == "warning":
            warnings.append(message.text)

    page.on("console", handler)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/d", wait_until="load")
        page.wait_for_function(
            """() => {
                const mount = document.querySelector('.tile[data-tile-id="enc"] .chart-mount');
                return Boolean(mount && echarts.getInstanceByDom(mount));
            }""",
            timeout=30_000,
        )
        page.wait_for_timeout(500)
        series = page.evaluate("""() => {
            const mount = document.querySelector('.tile[data-tile-id="enc"] .chart-mount');
            return echarts.getInstanceByDom(mount).getOption().series
                .map((s) => ({name: s.name, data: s.data}));
        }""")
    finally:
        page.remove_listener("console", handler)
        _stop_server(server, thread, page)
    assert len(series) == 1, series
    assert series[0]["name"] == "c", series
    assert [row[0] for row in series[0]["data"]] == ["apac", "eu", "us"], series
    assert all(row[1] is not None for row in series[0]["data"]), series
    assert any("y: b" in w and "inferring instead" in w for w in warnings), warnings


def test_query_page_saves_a_tile_against_a_named_source_under_one_key(page, tmp_path_factory):
    """#397: with every connection named under the one `source:` key, the picker
    still lists them and a tile saved against the non-default one reloads onto it."""
    root = tmp_path_factory.mktemp("onekey")
    create_demo(root)
    path = root / ".sqldash" / "demo.yaml"
    path.write_text(
        path.read_text().replace(
            "source: {type: duckdb, attach_files: true}",
            "source:\n"
            "  warehouse: {type: duckdb, attach_files: true, default: true}\n"
            "  alt: {type: duckdb, attach_files: true}",
            1,
        )
    )
    app = create_app(root / ".sqldash", allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/demo/query", wait_until="load")
        page.wait_for_selector("#qb-preview", timeout=30_000)
        options = page.evaluate(
            "[...document.querySelectorAll('#source-picker option')].map(o => o.value)"
        )
        assert "" in options, options
        assert "alt" in options, options
        page.evaluate(
            """() => {
              const s = document.getElementById('source-picker');
              s.value = 'alt';
              s.dispatchEvent(new Event('change', { bubbles: true }));
            }"""
        )
        page.fill("#qb-title", "Alt orders")
        page.locator(".ace_content").first.click()
        page.keyboard.press("ControlOrMeta+A")
        page.keyboard.type("SELECT COUNT(*) AS n FROM orders")
        page.locator("#run-btn").click()
        page.wait_for_function("() => !document.getElementById('qb-add').disabled", timeout=30_000)
        page.locator("#qb-add").click()
        page.wait_for_function("() => !document.getElementById('qb-another').hidden")
    finally:
        _stop_server(server, thread, page)
    text = path.read_text()
    assert "sources:" not in text, text
    block = text.split("Alt orders", 1)[1].split("queries:", 1)[0]
    assert "source: alt" in block, block
    dashboard, _, _ = DashboardStore(root / ".sqldash").load("demo")
    tile = next(t for t in dashboard.tiles if t.title == "Alt orders")
    assert dashboard.default_source_name == "warehouse"
    assert dashboard.named_source(tile.source) == dashboard.sources["alt"]


def test_a_tile_naming_the_default_source_by_name_shows_it_selected(page, tmp_path_factory):
    """A tile may name the default connection by its own name, and the picker's
    entry for the default has the empty value, so `selectTileSource` finds no
    matching option. It must still read as the default rather than blank. #397
    review."""
    root = tmp_path_factory.mktemp("named-default")
    create_demo(root)
    demo = root / ".sqldash" / "demo.yaml"
    text = demo.read_text().replace(
        "source: {type: duckdb, attach_files: true}",
        "source:\n  warehouse: {type: duckdb, attach_files: true, default: true}\n",
        1,
    )
    text = text.replace(
        "  - title: Recent orders\n",
        "  - title: Recent orders\n    source: warehouse\n",
        1,
    )
    demo.write_text(text)
    app = create_app(root / ".sqldash", allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.goto(
            f"http://127.0.0.1:{port}/d/demo/query?tile=recent_orders",
            wait_until="networkidle",
        )
        page.wait_for_selector("#source-picker", timeout=15_000)
        shown = page.evaluate(
            "() => document.querySelector('#source-picker')"
            "?.closest('.dd')?.querySelector('.dd-label')?.textContent?.trim()"
        )
        assert shown, "the source picker rendered no label at all"
        chosen = page.evaluate("() => document.querySelector('#source-picker').value")
        assert chosen == "", chosen
    finally:
        _stop_server(server, thread, page)


def test_studio_close_button_is_a_square_beside_the_tour_link(page, tmp_path):
    """The close glyph sat at 20px inside an 11px quiet button, so the button
    was 20x36 and the focus ring it gets on open traced a tall pill."""
    create_demo(tmp_path)
    app = create_app(tmp_path, studio=True, allowed_hosts=["127.0.0.1"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/demo")
        page.evaluate("localStorage.setItem('sqldash-ai-studio-tour-v1', 'seen')")
        page.locator("#studio-open").click()
        close = page.locator("#studio-close")
        close.wait_for(state="visible")
        box = close.bounding_box()
        tour = page.locator("#studio-tour-replay").bounding_box()
        assert round(box["width"]) == round(box["height"]) == 28, box
        centre = box["y"] + box["height"] / 2
        assert abs(centre - (tour["y"] + tour["height"] / 2)) <= 1, (box, tour)
    finally:
        _stop_server(server, thread, page)


@pytest.mark.parametrize("cleanup_failure", [False, True])
def test_studio_headless_edit_review_and_undo(page, tmp_path, monkeypatch, cleanup_failure):
    create_demo(tmp_path)
    dashboard_path = tmp_path / ".sqldash" / "demo.yaml"
    baseline = dashboard_path.read_bytes()
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "add", ".sqldash/demo.yaml"], check=True)
    agent = tmp_path / "edit_dashboard.py"
    agent.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        'assert sys.stdin.read() == ""\n'
        "context = sys.argv[1]\n"
        'assert "Preserve the current colors." in context\n'
        'assert "Change the dashboard title." in context\n'
        'p = Path(".sqldash/demo.yaml")\n'
        "text = p.read_text()\n"
        'p.write_text(text.replace("title:", "title: Studio ", 1))\n'
        'print("Dashboard updated")\n'
    )
    monkeypatch.setattr(
        "sqldash.studio.entrypoints.entrypoints_path", lambda: tmp_path / "studio.json"
    )
    save_entrypoint(
        AgentEntrypoint(name="Test agent", command=[sys.executable, str(agent), "{prompt}"])
    )
    app = create_app(tmp_path, studio=True, allowed_hosts=["127.0.0.1"])
    server, thread, port = _start_server(app)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(f"http://127.0.0.1:{port}/d/demo")
        page.evaluate("localStorage.setItem('sqldash-ai-studio-tour-v1', 'seen')")
        page.locator("#studio-open").click()
        page.locator("#studio-pick").click()
        page.locator(".tile").first.click(position={"x": 40, "y": 30})
        page.locator("#studio-note").fill("Change the dashboard title.")
        page.locator("#studio-note-form button[type=submit]").click()
        assert page.locator(".studio-pin").count() == 1
        page.locator("#studio-entrypoint").select_option("Test agent")
        if cleanup_failure:

            def denied(*args):
                raise PermissionError("SECRET_SENTINEL")

            monkeypatch.setattr("sqldash.studio.process.AgentProcess._signal", denied)
            page.locator("#studio-message").fill("Preserve the current colors.")
            page.locator("#studio-send").click()
            page.wait_for_function(
                "document.getElementById('studio-panel').textContent"
                ".includes('Could not stop all agent processes')"
            )
            assert page.locator("#studio-review").is_disabled()
            assert "SECRET_SENTINEL" not in page.locator("#studio-panel").inner_text()
            page.locator("#studio-close").click()
            sheet = page.locator("#studio-close-sheet")
            sheet.wait_for(state="visible")
            assert page.locator("#studio-close-keep").inner_text() == "Stop agent and close"
            assert page.locator("#studio-close-review").is_hidden()
            page.locator("#studio-close-back").click()
            sheet.wait_for(state="hidden")
            assert not page.locator("#studio-panel").is_hidden()
            page.wait_for_function("document.activeElement?.id === 'studio-close'")
            page.locator("#studio-close").click()
            page.keyboard.press("Escape")
            sheet.wait_for(state="hidden")
            assert not page.locator("#studio-panel").is_hidden()
            page.locator("#studio-close").click()
            page.locator("#studio-close-keep").click()
            page.wait_for_function('document.getElementById("studio-panel")?.hidden === true')
            assert not errors
            return
        assert page.locator("#studio-preview").count() == 0
        header_y = page.locator(".studio-head").bounding_box()["y"]
        page.locator("main.container").evaluate("el => el.scrollTop = 400")
        assert page.locator(".studio-head").bounding_box()["y"] == header_y
        assert page.evaluate("window.scrollY") == 0
        page.locator("main.container").evaluate("el => el.scrollTop = 0")
        for choice in ["undo", "keep"]:
            if choice == "keep":
                page.locator("#studio-add").click()
                page.locator("#studio-note").fill("Change the dashboard title.")
                page.locator("#studio-note-form button[type=submit]").click()
            page.evaluate("window.studioOriginalPanel = document.getElementById('studio-panel')")
            page.locator("#studio-message").fill("Preserve the current colors.")
            page.locator("#studio-message").press("Shift+Enter")
            assert page.locator("#studio-message").input_value().endswith("\n")
            page.locator("#studio-message").press("Enter")
            page.wait_for_function('!document.getElementById("studio-review").disabled')
            page.wait_for_function(
                "document.getElementById('dash-title').textContent.includes('Studio')"
            )
            assert page.evaluate(
                "window.studioOriginalPanel === document.getElementById('studio-panel')"
            )
            assert page.locator("#studio-notes .studio-note-card").count() == (
                1 if choice == "undo" else 2
            )
            assert page.locator("#studio-message").input_value() == ""
            assert "Dashboard updated" in page.locator("#studio-output").inner_text()
            assert page.locator("#studio-fresh-context").is_visible()
            if choice == "undo":

                def fail_validation(*args, **kwargs):
                    raise RuntimeError("password=SECRET_SENTINEL")

                with monkeypatch.context() as patch:
                    patch.setattr("sqldash.studio.sessions.lint_project", fail_validation)
                    page.locator("#studio-review").click()
                    page.wait_for_function(
                        "document.getElementById('studio-validation').textContent"
                        ".includes('Validation could not complete')"
                    )
                validation = page.locator("#studio-validation").inner_text()
                assert "0 validation errors" not in validation
                assert "SECRET_SENTINEL" not in page.locator("#studio-panel").inner_text()
                page.locator("#studio-recheck").click()
                page.wait_for_function(
                    "document.getElementById('studio-validation').textContent"
                    ".includes('0 validation errors')"
                )
            else:
                page.locator("#studio-review").click()
            page.locator("#studio-review-panel").wait_for(state="visible")
            assert "demo.yaml" in page.locator("#studio-diff").inner_text()
            assert "Studio" in page.locator("#studio-diff").inner_text()
            diff = subprocess.check_output(
                ["git", "-C", str(tmp_path), "diff", "--", ".sqldash/demo.yaml"], text=True
            )
            assert "+title: Studio" in diff
            page.locator("#studio-keep").click()
            page.wait_for_function('document.getElementById("studio-review-panel").hidden')
            page.locator("#studio-close").click()
            sheet = page.locator("#studio-close-sheet")
            sheet.wait_for(state="visible")
            assert page.locator("#studio-close-keep").inner_text() == "Keep edits and close"
            assert page.locator("#studio-close-review").is_visible()
            page.locator("#studio-close-back").click()
            sheet.wait_for(state="hidden")
            assert not page.locator("#studio-panel").is_hidden()
            page.locator("#studio-close").click()
            page.locator("#studio-close-review").click()
            sheet.wait_for(state="hidden")
            page.locator("#studio-review-panel").wait_for(state="visible")
            page.locator(f"#studio-{choice}").click()
            page.wait_for_function('document.getElementById("studio-review-panel").hidden')
            if choice == "undo":
                assert dashboard_path.read_bytes() == baseline
            else:
                assert dashboard_path.read_bytes() != baseline
                assert "Studio" in page.locator("h1").inner_text()
        assert not errors
    finally:
        _stop_server(server, thread, page)


def test_studio_live_activity_tracks_agent_until_completion(page, tmp_path, monkeypatch):
    create_demo(tmp_path)
    agent = tmp_path / "activity_agent.py"
    agent.write_text("""
import json,sys,time
from pathlib import Path
init=json.loads(sys.stdin.readline())
print(json.dumps({'type':'control_response','response':{
 'subtype':'success','request_id':init['request_id'],'response':{}}}),flush=True)
json.loads(sys.stdin.readline())
print(json.dumps({'type':'control_request','request_id':'edit','request':{
 'subtype':'can_use_tool','tool_name':'Edit','input':{'file_path':'.sqldash/demo.yaml'}}}),flush=True)
answer=json.loads(sys.stdin.readline())['response']['response']
assert answer['behavior']=='allow'
print(json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use',
 'name':'Edit','input':{'file_path':'.sqldash/demo.yaml'}}]}}),flush=True)
while not Path('resume.txt').exists():
 time.sleep(0.05)
print(json.dumps({'type':'assistant','message':{'content':[{'type':'text',
 'text':'Activity finished'}]}}),flush=True)
print(json.dumps({'type':'result','subtype':'success'}),flush=True)
sys.stdin.read()
""")
    monkeypatch.setattr(
        "sqldash.studio.entrypoints.entrypoints_path", lambda: tmp_path / "studio.json"
    )
    save_entrypoint(
        AgentEntrypoint(
            name="Activity test",
            protocol="claude",
            command=[sys.executable, str(agent), "{prompt}"],
        )
    )
    app = create_app(tmp_path, studio=True, allowed_hosts=["127.0.0.1"])
    server, thread, port = _start_server(app)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    activity = page.locator(".studio-live-activity")
    label = activity.locator("[role=status]")
    try:
        page.goto(f"http://127.0.0.1:{port}/d/demo")
        page.evaluate("localStorage.setItem('sqldash-ai-studio-tour-v1','seen')")
        page.locator("#studio-open").click()
        page.locator("#studio-entrypoint").select_option("Activity test")
        page.locator("#studio-message").fill("Show me the live activity")
        page.locator("#studio-send").click()
        page.locator(".studio-live-activity[data-waiting=true]").wait_for(state="visible")
        assert activity.locator("strong").inner_text() == "Activity test"
        assert label.inner_text() == "Waiting for your approval"
        assert (
            activity.locator(".studio-live-icon").evaluate("e => getComputedStyle(e).animationName")
            == "none"
        )
        assert page.locator("#studio-output > *").last.evaluate(
            "e => e.classList.contains('studio-live-activity')"
        )
        page.locator(".studio-permission").get_by_role("button", name="Allow once").click()
        page.wait_for_function(
            "document.querySelector('.studio-live-activity [role=status]')?.textContent"
            " === 'Running Edit'"
        )
        assert activity.get_attribute("data-waiting") == "false"
        assert activity.locator("strong").inner_text() == "Activity test"
        assert re.fullmatch(r"\d+s", activity.locator(".studio-live-elapsed").inner_text())
        assert (
            activity.locator(".studio-live-icon").evaluate("e => getComputedStyle(e).animationName")
            == "studio-working"
        )
        (tmp_path / "resume.txt").write_text("go")
        activity.wait_for(state="detached")
        page.wait_for_function("!document.querySelector('#studio-review').hidden")
        assert "Activity finished" in page.locator("#studio-output").inner_text()
        assert "Done." in page.locator("#studio-status").inner_text()
        assert not errors
    finally:
        _stop_server(server, thread, page)


def test_studio_pin_entrypoint_capture_and_docked_panel(page, tmp_path, monkeypatch):
    create_demo(tmp_path)
    monkeypatch.setattr(
        "sqldash.studio.entrypoints.entrypoints_path", lambda: tmp_path / "studio.json"
    )
    monkeypatch.setattr("sqldash.studio.entrypoints.shutil.which", lambda name: None)
    app = create_app(tmp_path, studio=True, allowed_hosts=["127.0.0.1"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/demo")
        page.evaluate("localStorage.setItem('sqldash-ai-studio-tour-v1', 'seen')")
        page.locator("#studio-open").click()
        page.locator("#studio-no-agents").wait_for(state="visible")
        assert page.locator("#studio-permission-summary").is_hidden()
        assert (
            page.locator("#studio-panel").evaluate("el => getComputedStyle(el).position") == "fixed"
        )
        viewport = page.viewport_size
        panel_box = page.locator("#studio-panel").bounding_box()
        topbar_box = page.locator(".topbar").bounding_box()
        assert abs(panel_box["y"] - (topbar_box["y"] + topbar_box["height"])) < 1
        assert abs(panel_box["x"] + panel_box["width"] - viewport["width"]) < 1
        assert abs(panel_box["y"] + panel_box["height"] - viewport["height"]) < 1
        assert page.evaluate("scrollY") == 0
        page.locator("#studio-settings-toggle").click()
        page.locator("#studio-entry-name").fill("Custom agent")
        page.locator("#studio-entry-command").fill("claude-custom")
        page.locator("#studio-entry-shell").select_option("/bin/zsh")
        page.locator("#studio-entrypoint-form button").click()
        page.wait_for_function(
            'document.getElementById("studio-entrypoint").value === "Custom agent"'
        )
        entrypoint = json.loads((tmp_path / "studio.json").read_text())[0]
        assert entrypoint["command"] == [
            "claude-custom",
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
            "{prompt}",
        ]
        assert entrypoint["shell"] == "/bin/zsh"
        page.locator("#studio-pick").click()
        page.locator("#dash-title").click(position={"x": 30, "y": 10})
        page.locator("#studio-note").fill("Make this heading smaller.")
        page.locator("#studio-note-form button[type=submit]").click()
        note = page.evaluate('JSON.parse(sessionStorage.getItem("sqldash-studio:demo")).notes[0]')
        assert note["selector"] == "#dash-title"
        pin = page.locator(".studio-pin").bounding_box()
        target = page.locator("#dash-title").bounding_box()
        assert abs(pin["x"] + pin["width"] / 2 - (target["x"] + 30)) < 2
        page.evaluate("window.scrollTo(0, 250)")
        panel_box = page.locator("#studio-panel").bounding_box()
        topbar_box = page.locator(".topbar").bounding_box()
        assert abs(panel_box["y"] - (topbar_box["y"] + topbar_box["height"])) < 1
        page.evaluate("""() => {
            navigator.mediaDevices.getDisplayMedia = async () => {
                const canvas = document.createElement('canvas');
                canvas.width = 640; canvas.height = 480;
                canvas.getContext('2d').fillRect(0,0,640,480);
                const stream = canvas.captureStream(1); window.captureTestStream = stream;
                return stream;
            };
        }""")
        page.locator("#studio-context-toggle").click()
        page.locator("#studio-capture").click()
        page.locator("#studio-capture-preview").wait_for(state="visible")
        assert (
            page.locator("#studio-capture-image")
            .get_attribute("src")
            .startswith("data:image/png;base64,")
        )
        assert page.evaluate(
            'window.captureTestStream.getTracks().every(t => t.readyState === "ended")'
        )
        page.locator("#studio-capture-remove").click()
        assert page.locator("#studio-capture-preview").is_hidden()
        page.locator("#studio-prepare").click()
        page.locator("#studio-ready").wait_for(state="visible")
        context = json.loads(page.locator("#studio-context").text_content())
        assert context["annotations"][0]["selector"] == "#dash-title"
        page.locator("#studio-back").click()
        page.locator("#studio-draft").wait_for(state="visible")
        page.set_viewport_size({"width": 600, "height": 900})
        page.locator("#studio-add").click()
        form = page.locator("#studio-note-form").bounding_box()
        assert form["x"] >= 0
        assert form["x"] + form["width"] <= 600
        assert form["y"] + form["height"] <= 900
    finally:
        page.set_viewport_size({"width": 1400, "height": 1000})
        _stop_server(server, thread, page)


def test_studio_codex_entrypoint_saves_skip_git_repo_check(page, tmp_path, monkeypatch):
    create_demo(tmp_path)
    monkeypatch.setattr(
        "sqldash.studio.entrypoints.entrypoints_path", lambda: tmp_path / "studio.json"
    )
    monkeypatch.setattr("sqldash.studio.entrypoints.shutil.which", lambda name: None)
    app = create_app(tmp_path, studio=True, allowed_hosts=["127.0.0.1"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/demo")
        page.evaluate("localStorage.setItem('sqldash-ai-studio-tour-v1', 'seen')")
        page.locator("#studio-open").click()
        page.locator("#studio-no-agents").wait_for(state="visible")
        page.locator("#studio-settings-toggle").click()
        page.locator("#studio-entry-name").fill("Custom Codex")
        page.locator("#studio-entry-command").fill("codex-custom")
        page.locator("#studio-entry-kind").select_option("codex")
        page.locator("#studio-entrypoint-form button").click()
        page.wait_for_function(
            'document.getElementById("studio-entrypoint").value === "Custom Codex"'
        )
        entrypoint = json.loads((tmp_path / "studio.json").read_text())[0]
        assert entrypoint["command"] == [
            "codex-custom",
            "exec",
            "--skip-git-repo-check",
            "--json",
            "{prompt}",
        ]
        assert entrypoint["protocol"] == "text"
    finally:
        _stop_server(server, thread, page)


def test_studio_request_queue_preserves_draft_and_tour_is_local(page, tmp_path, monkeypatch):
    create_demo(tmp_path)
    monkeypatch.setattr(
        "sqldash.studio.entrypoints.entrypoints_path", lambda: tmp_path / "studio.json"
    )
    app = create_app(tmp_path, studio=True, allowed_hosts=["127.0.0.1"])
    server, thread, port = _start_server(app)
    baseline = (tmp_path / ".sqldash/demo.yaml").read_bytes()
    try:
        page.goto(f"http://127.0.0.1:{port}/d/demo")
        page.evaluate("""() => {
          localStorage.setItem('sqldash-ai-studio-tour-v1', 'seen');
          sessionStorage.setItem('sqldash-studio:demo', JSON.stringify({notes:
            Array.from({length:20}, (_, i) => ({tile:null,target:'Dashboard',
              x:0.5,y:0.5,note:'Requested change ' + (i+1)}))}));
        }""")
        page.reload()
        page.locator("#studio-open").click()
        page.locator("#studio-message").fill("Keep the current colors.")
        assert page.locator(".studio-note-card").count() == 20
        assert page.locator("#studio-notes").evaluate("e => e.scrollHeight > e.clientHeight")
        page.locator(".studio-note-remove").last.click()
        assert page.locator(".studio-note-card").count() == 19
        page.locator("#studio-note-undo").click()
        assert page.locator(".studio-note-card").count() == 20
        assert page.locator("#studio-message").input_value() == "Keep the current colors."
        page.locator(".studio-note-card").first.click()
        page.locator("#studio-note").fill("Updated general request")
        page.locator("#studio-note-form button[type=submit]").click()
        page.reload()
        page.locator("#studio-open").click()
        assert "Updated general request" in page.locator(".studio-note-card").first.inner_text()
        assert page.locator("#studio-message").input_value() == "Keep the current colors."
        page.locator("#studio-settings-toggle").click()
        page.keyboard.press("Escape")
        assert page.locator("#studio-settings").is_hidden()
        assert page.locator("#studio-panel").is_visible()
        page.emulate_media(reduced_motion="reduce")
        page.locator("#studio-tour-replay").click()
        assert page.locator(".tour-seconds").inner_text() == "Paused"
        assert page.locator(".tour-live-pin").inner_text() == "1"
        page.locator('[data-tour="next"]').click()
        assert (
            page.locator(".tour-queued-card").evaluate("el => getComputedStyle(el).opacity") == "1"
        )
        assert "Show weekly totals" in page.locator(".tour-queued-card").inner_text()
        page.locator('[data-tour="next"]').click()
        assert page.locator(".send-demo-typing").inner_text() == "Keep the current colors."
        assert (
            page.locator(".tour-agent-processing").evaluate("el => getComputedStyle(el).opacity")
            == "1"
        )
        assert "Updating the dashboard" in page.locator(".tour-agent-status").inner_text()
        assert (
            page.locator("#studio-entrypoint").locator("..").get_attribute("class")
            == "studio-agent-picker"
        )
        page.locator('[data-tour="next"]').click()
        assert page.locator(".studio-tour").is_hidden()
        assert page.locator(".tour-live").is_hidden()
        page.locator("#studio-tour-replay").click()
        assert page.locator('[data-tour="skip"]').inner_text() == "Skip tour"
        page.locator('[data-tour="skip"]').click()
        assert page.locator(".tour-live").is_hidden()
        assert page.locator("#studio-message").input_value() == "Keep the current colors."
        assert page.locator(".studio-note-card").count() == 20
        assert (tmp_path / ".sqldash/demo.yaml").read_bytes() == baseline
    finally:
        page.emulate_media(reduced_motion="no-preference")
        _stop_server(server, thread, page)


def test_studio_refreshes_dashboard_structure_without_losing_drafts(page, tmp_path):
    path = tmp_path / "d.yaml"
    path.write_text(
        "title: Initial\nsource: {type: duckdb, database: ':memory:'}\n"
        "filters: [{name: value, type: text, default: '3'}]\n"
        "tiles: [{id: count, title: Initial tile, sql: 'SELECT {{ value }} AS n'}]\n"
    )
    app = create_app(tmp_path, studio=True, allowed_hosts=["127.0.0.1"])
    server, thread, port = _start_server(app)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(f"http://127.0.0.1:{port}/d/d?f_value=8")
        page.evaluate("localStorage.setItem('sqldash-ai-studio-tour-v1', 'seen')")
        page.locator("#studio-open").click()
        page.locator("#studio-message").fill("Keep this draft")
        page.evaluate("window.originalComposer = document.getElementById('studio-message')")
        path.write_text(
            "title: Updated\nsource: {type: duckdb, database: ':memory:'}\n"
            "layout: {columns: 12, row_height: 60}\n"
            "css: '.tile { border-radius: 23px; }'\n"
            "filters: [{name: value, type: text, default: '5'}]\n"
            "tiles:\n"
            "- {id: count, title: Updated tile, sql: 'SELECT {{ value }} AS n'}\n"
            "- {id: extra, title: Added tile, type: text, markdown: Added content}\n"
        )
        page.wait_for_function("document.getElementById('dash-title').textContent === 'Updated'")
        assert page.locator(".tile").count() == 2
        assert page.locator("#studio-message").input_value() == "Keep this draft"
        assert page.evaluate(
            "window.originalComposer === document.getElementById('studio-message')"
        )
        assert page.locator('[data-filter="value"]').input_value() == "8"
        assert (
            page.locator('[data-tile-id="count"]').evaluate(
                "el => getComputedStyle(el).borderRadius"
            )
            == "23px"
        )
        page.locator('[data-filter="value"]').fill("12")
        page.locator('[data-filter="value"]').dispatch_event("change")
        page.wait_for_function(
            "document.querySelector('[data-tile-id=count] .tile-body').textContent.includes('12')"
        )
        path.write_text("title: [invalid")
        page.wait_for_function(
            "document.getElementById('studio-status').textContent.includes('could not refresh')"
        )
        assert page.locator("#dash-title").inner_text() == "Updated"
        assert page.locator("#studio-message").input_value() == "Keep this draft"
        assert not errors
    finally:
        _stop_server(server, thread, page)


def test_studio_active_request_rim_overrides_copied_tile_background(page, tmp_path):
    create_demo(tmp_path)
    app = create_app(tmp_path, studio=True, allowed_hosts=["127.0.0.1"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/demo")
        page.evaluate("localStorage.setItem('sqldash-ai-studio-tour-v1', 'seen')")
        page.locator("#studio-open").click()
        page.locator("#studio-pick").click()
        page.locator(".tile").first.click(position={"x": 30, "y": 20})
        page.locator("#studio-note").fill("Clarify this tile.")
        page.locator("#studio-note-form button[type=submit]").click()
        card = page.locator(".studio-note-card").first
        card.click()
        assert card.get_attribute("aria-pressed") == "true"
        assert card.evaluate("el => el.style.backgroundImage")
        for theme in ("light", "dark"):
            page.evaluate("value => document.documentElement.dataset.theme = value", theme)
            tile_image = page.locator(".tile").first.evaluate(
                "el => getComputedStyle(el).backgroundImage"
            )
            for skin in ("neon", "iris-rim"):
                page.evaluate("value => document.documentElement.dataset.studioSkin = value", skin)
                page.wait_for_function(
                    """() => {
                      const el = document.querySelector('.studio-note-card[aria-pressed="true"]');
                      const image = el ? getComputedStyle(el).backgroundImage : '';
                      return (image.match(/linear-gradient/g) || []).length === 2;
                    }""",
                    timeout=5000,
                )
                image = card.evaluate("el => getComputedStyle(el).backgroundImage")
                assert image.count("linear-gradient") == 2
                assert card.evaluate("el => getComputedStyle(el).backgroundClip") == (
                    "padding-box, border-box"
                )
                assert (
                    page.locator(".tile").first.evaluate(
                        "el => getComputedStyle(el).backgroundImage"
                    )
                    == tile_image
                )
    finally:
        _stop_server(server, thread, page)


def test_studio_annotation_mode_stays_on_between_pins(page, tmp_path):
    create_demo(tmp_path)
    app = create_app(tmp_path, studio=True, allowed_hosts=["127.0.0.1"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/demo")
        page.evaluate("localStorage.setItem('sqldash-ai-studio-tour-v1', 'seen')")
        page.locator("#studio-open").click()
        page.locator("#studio-pick").click()
        for index in range(2):
            page.locator(".tile").nth(index).click(position={"x": 35, "y": 35})
            page.locator("#studio-note").fill(f"Request {index}")
            page.locator("#studio-note-form button[type=submit]").click()
            assert page.locator("#studio-pick").get_attribute("aria-pressed") == "true"
        assert page.locator("#studio-notes .studio-note-card").count() == 2
        page.locator(".tile").first.click(position={"x": 70, "y": 60})
        page.locator("#studio-note-cancel").click()
        assert page.locator("#studio-pick").get_attribute("aria-pressed") == "true"
        page.locator("#studio-browse").click()
        assert page.locator("#studio-pick").get_attribute("aria-pressed") == "false"
        page.locator(".tile").first.click(position={"x": 70, "y": 60})
        assert page.locator("#studio-note-form").is_hidden()
    finally:
        _stop_server(server, thread, page)


def test_studio_stream_output_handles_chunks_and_plain_agents(page, served):
    page.goto(served + "/d/demo")
    result = page.evaluate(r"""async () => {
      const {agentOutput} = await import('/static/js/studio-output.js');
      const stream = agentOutput();
      const events = [
        {type:'system',subtype:'init',apiKey:'must not display'},
        {type:'rate_limit_event',rate_limit_info:{status:'allowed'}},
        {type:'stream_event',event:{type:'content_block_delta',
          delta:{type:'text_delta',text:'Hello <script> 🌎'}}},
        {type:'assistant',message:{content:[{type:'text',text:'Hello <script> 🌎'},
          {type:'tool_use',name:'Edit',
           input:{file_path:'demo.yaml',secret:'must not display'}}]}},
        {type:'user',message:{content:[
          {type:'tool_result',is_error:true,content:'Permission denied'}]}},
        {type:'result',permission_denials:[{tool_name:'Edit'}]}
      ].map(e => JSON.stringify(e) + '\n').join('');
      let text = '';
      for (const character of events) text += stream.push(character);
      text += stream.push('', true);
      const plain = agentOutput();
      const plainText = plain.push('working') + plain.push(' now\n') + plain.push('done',true);
      const fallback = agentOutput().push(
        JSON.stringify({type:'result',result:'Final only'}), true);
      const malformed = agentOutput().push('{bad json',true);
      const diagnostic = agentOutput();
      const warning = '⚠ claude.ai connectors are disabled because another auth source is set';
      let visible = '';
      for (const char of warning + '\n') visible += diagnostic.push(char);
      return {text,plainText,fallback,malformed,problem:stream.problem,
        diagnostics:stream.diagnostics,warning:diagnostic.diagnostics,visible};
    }""")
    assert result["text"].count("Hello <script> 🌎") == 1
    assert "Running Edit · demo.yaml" in result["text"]
    assert "Permission denied" in result["text"]
    assert "1 tool operation(s) denied" in result["text"]
    assert "permissions blocked" in result["problem"]
    assert "must not display" not in result["text"]
    assert "rate_limit_event" not in result["text"]
    assert "rate_limit_event" in result["diagnostics"]
    assert result["visible"] == ""
    assert "connectors are disabled" in result["warning"]
    assert result["plainText"] == "working now\ndone"
    assert "Final only" in result["fallback"]
    assert result["malformed"] == "{bad json"


def test_studio_missing_session_recovers_draft_but_network_errors_do_not(page, tmp_path):
    create_demo(tmp_path)
    app = create_app(tmp_path, studio=True, allowed_hosts=["127.0.0.1"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/demo")
        page.evaluate("""() => {
          localStorage.setItem('sqldash-ai-studio-tour-v1', 'seen');
          window.seedMissing = () => sessionStorage.setItem('sqldash-studio:demo',
            JSON.stringify({session:{id:'missing',launched:true},message:'Keep my draft',
              notes:[{note:'Keep my request',target:'Dashboard',tile:null,x:.5,y:.5}]}));
          window.seedMissing();
        }""")
        page.reload()
        page.wait_for_function(
            "document.querySelector('#studio-status').textContent"
            ".includes('Previous session ended')"
        )
        assert "Previous session ended" in page.locator("#studio-status").inner_text()
        assert page.locator("#studio-message").input_value() == "Keep my draft"
        assert "Keep my request" in page.locator("#studio-notes").inner_text()
        assert page.locator("#studio-run").is_hidden()
        assert page.locator("#studio-end").count() == 0
        assert (
            page.evaluate("JSON.parse(sessionStorage.getItem('sqldash-studio:demo')).session")
            is None
        )
        page.route(
            "**/api/studio/sessions/missing/output*",
            lambda route: route.fulfill(status=503, json={"detail": "Connection unavailable"}),
        )
        page.evaluate("""() => {
          const key = 'sqldash-studio:demo';
          const saved = JSON.parse(sessionStorage.getItem(key));
          saved.session = {id:'missing',launched:true};
          sessionStorage.setItem(key,JSON.stringify(saved));
        }""")
        page.reload()
        page.wait_for_function(
            "document.querySelector('#studio-status').textContent"
            ".includes('Connection unavailable')"
        )
        assert page.locator("#studio-review").is_hidden()
        assert (
            page.evaluate("JSON.parse(sessionStorage.getItem('sqldash-studio:demo')).session.id")
            == "missing"
        )
    finally:
        page.unroute("**/api/studio/sessions/missing/output*")
        _stop_server(server, thread, page)


def test_studio_chat_groups_tools_and_renders_agent_markup_as_text(page, served):
    page.goto(served + "/d/demo")
    result = page.evaluate("""async () => {
      const {renderAgentChat} = await import('/static/js/studio-chat.js');
      const container = document.createElement('div');
      document.body.append(container);
      const text = 'I will update the chart.\\n\u203a Running Read · demo.yaml\\n' +
        '✓ Tool finished\\n\u203a Running Edit · demo.yaml\\nDone <img src=x onerror=alert(1)>';
      renderAgentChat(container,text,'Claude Code','Use weekly totals',2);
      const collapsed = !container.querySelector('details').open;
      container.querySelector('details').open = true;
      renderAgentChat(container,text + '\\nReady to review.','Claude Code','Use weekly totals',2);
      const result = {
        collapsed,expanded:container.querySelector('details').open,
        groups:container.querySelectorAll('details').length,
        images:container.querySelectorAll('img').length,
        user:container.querySelector('.studio-chat-user').textContent,
        messages:container.querySelectorAll('.studio-chat-assistant').length,
        text:container.textContent
      };
      container.remove();
      return result;
    }""")
    assert result["collapsed"]
    assert result["expanded"]
    assert result["groups"] == 1
    assert result["images"] == 0
    assert result["messages"] == 2
    assert "Use weekly totals" in result["user"]
    assert "2 tool actions" in result["text"]
    assert "<img src=x onerror=alert(1)>" in result["text"]


@pytest.mark.parametrize("decision", ["allow", "deny"])
def test_studio_permission_prompt_round_trip(page, tmp_path, monkeypatch, decision):
    from sqldash.studio.entrypoints import AgentEntrypoint, save_entrypoint

    create_demo(tmp_path)
    monkeypatch.setattr(
        "sqldash.studio.entrypoints.entrypoints_path", lambda: tmp_path / "studio.json"
    )
    script = tmp_path / "approval_agent.py"
    script.write_text("""
import json,sys
from pathlib import Path
init=json.loads(sys.stdin.readline())
print(json.dumps({'type':'control_response','response':{
 'subtype':'success','request_id':init['request_id'],'response':{}}}),flush=True)
json.loads(sys.stdin.readline())
print(json.dumps({'type':'control_request','request_id':'write-request','request':{
 'subtype':'can_use_tool','tool_name':'Write','input':{
 'file_path':'approval.txt','content':'approved'}}}),flush=True)
answer=json.loads(sys.stdin.readline())['response']['response']
if answer['behavior']=='allow':
 Path('approval.txt').write_text(answer['updatedInput']['content'])
print(json.dumps({'type':'assistant','message':{'content':[{'type':'text',
 'text':'Finished with `approval.txt`.\\n\\n```python\\nprint("done")\\n```'}]}}),flush=True)
print(json.dumps({'type':'result','subtype':'success'}),flush=True)
sys.stdin.read()
""")
    save_entrypoint(
        AgentEntrypoint(
            name="Approval test",
            protocol="claude",
            command=[sys.executable, str(script), "{prompt}"],
        )
    )
    app = create_app(tmp_path, studio=True, allowed_hosts=["127.0.0.1"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/demo")
        page.evaluate("localStorage.setItem('sqldash-ai-studio-tour-v1','seen')")
        page.locator("#studio-open").click()
        page.locator("#studio-entrypoint").select_option("Approval test")
        page.locator("#studio-message").fill("Create the requested file")
        page.locator("#studio-send").click()
        page.locator(".studio-permission").wait_for()
        assert not (tmp_path / "approval.txt").exists()
        page.reload()
        page.locator(".studio-permission").wait_for()
        token = page.locator("#studio-permissions").get_attribute("data-requests")
        session = page.evaluate(
            "JSON.parse(sessionStorage.getItem('sqldash-studio:demo')).session.id"
        )
        url = f"http://127.0.0.1:{port}/api/studio/sessions/{session}/permissions/{token}"
        assert page.request.post(url, data={"decision": "allow"}).status == 403
        headers = {"X-Sqldash-Token": app.state.api_token}
        assert (
            page.request.post(
                url, data={"decision": "allow"}, headers={**headers, "Origin": "https://evil.test"}
            ).status
            == 403
        )
        assert (
            page.request.post(url, data={"decision": "allow", "input": {}}, headers=headers).status
            == 422
        )
        page.locator(".studio-permission").get_by_role(
            "button", name="Allow once" if decision == "allow" else "Deny"
        ).click()
        page.wait_for_function("!document.querySelector('#studio-review').hidden")
        assert (tmp_path / "approval.txt").exists() == (decision == "allow")
        assert page.request.post(url, data={"decision": "allow"}, headers=headers).status == 409
        assert page.locator("#studio-output p code").inner_text() == "approval.txt"
        assert page.locator("#studio-output pre code").inner_text() == 'print("done")'
        assert page.locator("#studio-message").evaluate("e => e.offsetHeight") < 100
        page.locator("#studio-close").click()
        page.locator("#studio-close-keep").click()
        page.wait_for_function("document.querySelector('#studio-panel').hidden")
    finally:
        _stop_server(server, thread, page)


def test_studio_chat_markdown_lists_keep_code_and_html_inert(page, served):
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(served + "/d/demo")
    result = page.evaluate(r"""async () => {
      const {renderAgentChat} = await import('/static/js/studio-chat.js');
      const node = document.createElement('div');
      renderAgentChat(node, [
        '## Changes', '**Better labels** and *spacing*.', '',
        '- First `metric`', '  - Nested item', '- Second item', '',
        '3. Check lint', '4. Preview', '',
        '<img src=x onerror=alert(1)>', '',
        '```sql', '- literal bullet', '**literal emphasis**', '```'
      ].join('\n'), 'Claude', '', 0);
      const text = node.querySelector('.studio-chat-text');
      return {
        heading:text.querySelector('h2')?.textContent,
        bold:text.querySelector('strong')?.textContent,
        emphasis:text.querySelector('em')?.textContent,
        bullets:[...text.querySelectorAll(':scope > ul > li')].map(li=>li.textContent),
        nested:text.querySelector('ul ul li')?.textContent,
        ordered:text.querySelector('ol')?.start,
        items:[...text.querySelectorAll('ol li')].map(li=>li.textContent),
        inline:text.querySelector('li code')?.textContent,
        code:text.querySelector('pre code')?.textContent,
        unsafe:text.querySelectorAll('img,script,a').length,
        literal:text.textContent.includes('<img src=x onerror=alert(1)>')
      };
    }""")
    assert result == {
        "heading": "Changes",
        "bold": "Better labels",
        "emphasis": "spacing",
        "bullets": ["First metricNested item", "Second item"],
        "nested": "Nested item",
        "ordered": 3,
        "items": ["Check lint", "Preview"],
        "inline": "metric",
        "code": "- literal bullet\n**literal emphasis**",
        "unsafe": 0,
        "literal": True,
    }
    assert not errors


def test_studio_chat_continuations_return_to_the_parent_list_item(page, served):
    page.goto(served + "/d/demo")
    result = page.evaluate(r"""async () => {
      const {renderAgentChat} = await import('/static/js/studio-chat.js');
      const node = document.createElement('div');
      renderAgentChat(node, [
        '- First', '  - Nested', '    nested continuation',
        '  parent continuation', '- Second', '## Summary', 'Tail'
      ].join('\n'), 'Claude', '', 0);
      const text = node.querySelector('.studio-chat-text');
      const first = text.querySelector('ul > li');
      return {
        parent:[...first.childNodes].filter(n=>n.nodeType===Node.TEXT_NODE)
          .map(n=>n.textContent).join(''),
        nested:first.querySelector('ul > li').textContent,
        lists:text.querySelectorAll(':scope > ul').length,
        items:text.querySelectorAll(':scope > ul > li').length,
        heading:text.querySelector(':scope > h2')?.textContent,
        paragraphs:[...text.querySelectorAll(':scope > p')].map(p=>p.textContent),
        stray:[...text.querySelectorAll('ul,ol')].some(list=>[...list.childNodes]
          .some(child=>child.nodeType!==Node.ELEMENT_NODE||child.tagName!=='LI'))
      };
    }""")
    assert result == {
        "parent": "First parent continuation",
        "nested": "Nested nested continuation",
        "lists": 1,
        "items": 2,
        "heading": "Summary",
        "paragraphs": ["Tail"],
        "stray": False,
    }


def test_studio_continuous_claude_chat_and_session_auto_approval(page, tmp_path, monkeypatch):
    create_demo(tmp_path)
    path = tmp_path / ".sqldash" / "demo.yaml"
    agent = tmp_path / "conversation.py"
    agent.write_text("""
import json,sys
from pathlib import Path
identity='00000000-0000-4000-8000-000000000002'
init=json.loads(sys.stdin.readline())
print(json.dumps({'type':'control_response','response':{
 'subtype':'success','request_id':init['request_id'],'response':{}}}),flush=True)
print(json.dumps({'type':'system','subtype':'init','session_id':identity}),flush=True)
prompt=json.loads(sys.stdin.readline())['message']['content']
resumed='--resume='+identity in sys.argv
assert ('Second request' in prompt) == resumed
print(json.dumps({'type':'control_request','request_id':'edit','request':{
 'subtype':'can_use_tool','tool_name':'Edit','input':{'file_path':'.sqldash/demo.yaml'}}}),flush=True)
answer=json.loads(sys.stdin.readline())['response']['response']
assert answer['behavior']=='allow'
path=Path('.sqldash/demo.yaml')
text=path.read_text()
path.write_text(text.replace('title:', 'title: Updated', 1))
print(json.dumps({'type':'assistant','message':{'content':[{
 'type':'text','text':'Second edit finished' if resumed else 'First edit finished'}]}}),flush=True)
print(json.dumps({'type':'result','subtype':'success'}),flush=True)
sys.stdin.read()
""")
    monkeypatch.setattr(
        "sqldash.studio.entrypoints.entrypoints_path", lambda: tmp_path / "studio.json"
    )
    save_entrypoint(
        AgentEntrypoint(
            name="Conversation", protocol="claude", command=[sys.executable, str(agent), "{prompt}"]
        )
    )
    app = create_app(tmp_path, studio=True, allowed_hosts=["127.0.0.1"])
    server, thread, port = _start_server(app)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(f"http://127.0.0.1:{port}/d/demo")
        page.evaluate("localStorage.setItem('sqldash-ai-studio-tour-v1','seen')")
        page.locator("#studio-open").click()
        page.locator("#studio-entrypoint").select_option("Conversation")
        assert not page.locator("#studio-auto-approve").is_checked()
        page.locator("#studio-message").fill("First request")
        page.locator("#studio-send").click()
        page.locator("#studio-permissions").wait_for(state="visible")
        assert page.locator("#studio-message").is_visible()
        assert page.locator("#studio-fresh-context").is_hidden()
        assert page.locator("#studio-send").is_disabled()
        page.locator("#studio-message").fill("Second request")
        page.locator("#studio-auto-approve").check()
        page.locator("#studio-undo-last").wait_for(state="visible")
        assert page.locator("#studio-message").input_value() == "Second request"
        assert page.locator("#studio-review-panel").is_hidden()
        first = path.read_bytes()
        assert b"title: Updated" in first
        page.wait_for_function(
            "document.querySelector('#dash-title').textContent.includes('Updated')"
        )
        page.locator("#studio-send").click()
        page.wait_for_function(
            "document.querySelector('#studio-output').textContent.includes('Second edit finished')"
        )
        page.locator("#studio-undo-last").wait_for(state="visible")
        assert path.read_bytes() != first
        assert "First edit finished" in page.locator("#studio-output").inner_text()
        assert page.locator("#studio-message").is_visible()
        page.locator("#studio-undo-last").click()
        page.wait_for_function(
            "document.querySelector('#studio-status').textContent.includes('Last edits undone')"
        )
        assert path.read_bytes() == first
        assert not page.locator("#studio-send").is_disabled()
        page.reload()
        page.locator("#studio-output").wait_for(state="visible")
        page.wait_for_function("!document.querySelector('#studio-send').disabled")
        assert page.locator("#studio-auto-approve").is_checked()
        assert page.locator("#studio-undo-last").is_hidden()
        assert "First edit finished" in page.locator("#studio-output").inner_text()
        page.locator("#studio-auto-approve").uncheck()
        page.wait_for_function("!document.querySelector('#studio-auto-approve').disabled")
        assert not next(iter(app.state.studio.sessions.values())).auto_approve
        page.locator("#studio-close").click()
        page.locator("#studio-close-keep").click()
        page.locator("#studio-panel").wait_for(state="hidden")
        page.locator("#studio-open").click()
        assert not page.locator("#studio-auto-approve").is_checked()
        assert not errors
    finally:
        _stop_server(server, thread, page)


def test_studio_codex_json_separates_replies_from_command_output(page, served):
    page.goto(f"{served}/d/demo")
    result = page.evaluate("""async () => {
      const {agentOutput} = await import('/static/js/studio-output.js');
      const {renderAgentChat} = await import('/static/js/studio-chat.js');
      const formatter = agentOutput();
      const events = [
        {type:'thread.started',thread_id:'test'},
        {type:'item.completed',item:{type:'reasoning',text:'INTERNAL_REASONING'}},
        {type:'item.started',item:{id:'cmd',type:'command_execution',command:'sqldash lint'}},
        {type:'item.completed',item:{id:'cmd',type:'command_execution',status:'completed',exit_code:0,aggregated_output:'VERBOSE_COMMAND_LOG'}},
        {type:'item.updated',item:{id:'reply',type:'agent_message',text:'Partial reply'}},
        {type:'item.completed',item:{id:'reply',type:'agent_message',
          text:'Updated `revenue`.\\n\\n```sql\\nSELECT 1\\n```'}},
        {type:'turn.completed',usage:{output_tokens:100}}
      ];
      const wire = events.map(e => JSON.stringify(e)).join('\\n')+'\\n';
      let text='';
      for(let i=0;i<wire.length;i+=17) text+=formatter.push(wire.slice(i,i+17));
      text+=formatter.push('',true);
      const root=document.createElement('div');
      renderAgentChat(root,text,'Codex','Make it clearer',0);
      const failed = agentOutput();
      const failureEvents = [
        {type:'item.started',item:{id:'rm',type:'command_execution',command:'rm -rf build',
          status:'in_progress'}},
        {type:'item.completed',item:{id:'rm',type:'command_execution',command:'rm -rf build',
          status:'declined',exit_code:null}},
        {type:'item.completed',item:{id:'edit',type:'file_change',status:'completed',
          changes:[{path:'demo.yaml',kind:'update'}]}},
        {type:'item.completed',item:{id:'blocked-edit',type:'file_change',status:'declined',
          changes:[{path:'untouched.yaml',kind:'update'}]}},
        {type:'turn.failed',error:{message:'Unable to continue'}}
      ].map(e => JSON.stringify(e)).join('\\n')+'\\n';
      let failure = '';
      for (const character of failureEvents) failure += failed.push(character);
      failure += failed.push('', true);
      const failedRoot=document.createElement('div');
      renderAgentChat(failedRoot,failure,'Codex','Clean up',0);
      const rows=[...failedRoot.querySelectorAll('.studio-chat-tools div')].map(n => n.textContent);
      const plain = agentOutput().push('Ordinary custom agent reply',true);
      return {text:root.textContent,
        messages:root.querySelectorAll('.studio-chat-assistant').length,
        tools:root.querySelectorAll('.studio-chat-tools').length,
        code:root.querySelector('pre code').textContent,
        diagnostics:formatter.diagnostics, failure, problem:failed.problem, plain,
        failedText:failedRoot.textContent, rows,
        failedGroups:failedRoot.querySelectorAll('.studio-chat-tools').length,
        summary:failedRoot.querySelector('.studio-chat-tools summary').textContent,
        errorsOutsideTools:[...failedRoot.querySelectorAll('.studio-chat-error')]
          .every(n => !n.closest('details')),
        errors:[...failedRoot.querySelectorAll('.studio-chat-error')].map(n => n.textContent)};
    }""")
    assert result["messages"] == 1
    assert result["tools"] == 1
    assert result["code"] == "SELECT 1"
    assert "VERBOSE_COMMAND_LOG" not in result["text"]
    assert "INTERNAL_REASONING" not in result["text"]
    assert "Partial reply" not in result["text"]
    assert "VERBOSE_COMMAND_LOG" in result["diagnostics"]
    assert result["failure"].count("Unable to continue") == 1
    assert "Unable to continue" not in result["problem"]
    assert "conversation" in result["problem"]
    assert result["failedGroups"] == 1
    assert result["rows"] == [
        "command · rm -rf build",
        "Command declined",
        "Edited demo.yaml",
        "File edit declined",
    ]
    assert result["summary"] == "2 tool actions"
    assert "Running file edit" not in result["failedText"]
    assert "Edited untouched.yaml" not in result["failedText"]
    assert result["errorsOutsideTools"]
    assert len(result["errors"]) == 1
    assert result["errors"][0].count("Unable to continue") == 1
    assert result["plain"] == "Ordinary custom agent reply"


def test_studio_keeps_plain_timestamped_failures_until_codex_identifies_itself(page, served):
    page.goto(f"{served}/d/demo")
    result = page.evaluate("""async () => {
      const {agentOutput} = await import('/static/js/studio-output.js');
      const {renderAgentChat} = await import('/static/js/studio-chat.js');
      const failure = '2026-09-10T12:00:00Z ERROR unable to edit dashboard';
      const plain = agentOutput();
      const plainText = plain.push(failure + '\\n', true);
      const root = document.createElement('div');
      renderAgentChat(root, plainText, 'Custom agent', 'Edit dashboard', 0);
      const codex = agentOutput();
      const preamble = codex.push(failure + '\\n');
      const event = JSON.stringify({type:'thread.started',thread_id:'test'});
      const connected = codex.push(event + '\\n');
      const hidden = codex.push(failure + '\\n', true);
      const next = agentOutput().push(failure, true);
      return {plainText, rendered:root.textContent, plainDiagnostics:plain.diagnostics,
        preamble, connected, hidden, codexDiagnostics:codex.diagnostics, next};
    }""")
    failure = "2026-09-10T12:00:00Z ERROR unable to edit dashboard"
    assert result["plainText"] == failure + "\n"
    assert failure in result["rendered"]
    assert result["plainDiagnostics"] == ""
    assert result["preamble"] == failure + "\n"
    assert result["connected"] == ""
    assert result["hidden"] == ""
    assert result["codexDiagnostics"] == failure + "\n"
    assert result["next"] == failure


def test_studio_identifies_codex_items_by_shape_before_hiding_logs(page, served):
    page.goto(f"{served}/d/demo")
    result = page.evaluate(r"""async () => {
      const {agentOutput} = await import('/static/js/studio-output.js');
      const failure = '2026-09-10T12:00:00Z ERROR custom agent failed';
      const custom = [
        {type:'item.updated',progress:30},
        {type:'item.updated',item:{id:'task',type:'custom_progress',progress:30}},
        {type:'item.updated',item:{type:'agent_message',text:'No item ID'}},
        {type:'item.updated',item:{id:'task',type:'agent_message'}}
      ].map(event => {
        const output = agentOutput();
        output.push(JSON.stringify(event)+'\n');
        return output.push(failure+'\n',true);
      });
      const identified = [
        {id:'reply',type:'agent_message',text:'Updated dashboard'},
        {id:'cmd',type:'command_execution',command:'sqldash lint'}
      ].map(item => {
        const output = agentOutput();
        output.push(JSON.stringify({type:'item.completed',item})+'\n');
        return {visible:output.push(failure+'\n',true), diagnostics:output.diagnostics};
      });
      return {custom, identified};
    }""")
    failure = "2026-09-10T12:00:00Z ERROR custom agent failed\n"
    assert result["custom"] == [failure] * 4
    assert all(item["visible"] == "" for item in result["identified"])
    assert all(failure in item["diagnostics"] for item in result["identified"])


def test_studio_clear_requests_preserves_draft_and_can_undo(page, tmp_path):
    create_demo(tmp_path)
    app = create_app(tmp_path, studio=True, allowed_hosts=["127.0.0.1"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/demo")
        page.evaluate("localStorage.setItem('sqldash-ai-studio-tour-v1','seen')")
        page.locator("#studio-open").click()
        page.locator("#studio-pick").click()
        page.locator(".tile").first.click(position={"x": 40, "y": 30})
        page.locator("#studio-note").fill("Keep this request")
        page.locator("#studio-note-form button[type=submit]").click()
        page.locator("#studio-message").fill("Keep this draft")
        page.locator(".tile").first.click(position={"x": 200, "y": 30})
        page.locator("#studio-note").fill("Unsent pin text")
        assert page.locator(".studio-pin-pending").text_content() == "2"
        page.locator("#studio-clear").click()
        assert page.locator(".studio-pin:not(.studio-pin-pending)").count() == 0
        assert page.locator(".studio-note-card").count() == 0
        assert page.locator("#studio-message").input_value() == "Keep this draft"
        assert page.locator("#studio-note-form").is_visible()
        assert page.locator("#studio-note").input_value() == "Unsent pin text"
        assert page.locator(".studio-pin-pending").text_content() == "1"
        page.locator("#studio-clear-undo").click()
        assert page.locator(".studio-pin:not(.studio-pin-pending)").count() == 1
        assert page.locator(".studio-note-card").count() == 1
        assert page.locator("#studio-note").input_value() == "Unsent pin text"
        assert page.locator(".studio-pin-pending").text_content() == "2"
        page.locator("#studio-note-form button[type=submit]").click()
        assert page.locator(".studio-note-card").count() == 2
        assert page.locator(".studio-pin").count() == 2
        page.locator(".studio-pin").first.click()
        page.locator("#studio-note").fill("Edited in place")
        page.locator("#studio-clear").click()
        assert page.locator("#studio-note-form").is_hidden()
        assert page.locator(".studio-note-card").count() == 0
        page.locator("#studio-clear-undo").click()
        assert page.locator("#studio-note-form").is_visible()
        assert page.locator("#studio-note").input_value() == "Edited in place"
        assert page.locator(".studio-note-card").count() == 2
        page.locator("#studio-note-form button[type=submit]").click()
        assert "Edited in place" in page.locator(".studio-note-card").first.inner_text()
        assert page.locator(".studio-note-card").count() == 2
        page.locator(".studio-pin").first.click()
        page.locator("#studio-note").fill("Older unfinished edit")
        page.locator("#studio-clear").click()
        page.locator("#studio-pick").click()
        page.locator(".tile").first.click(position={"x": 200, "y": 30})
        page.locator("#studio-note").fill("Newer draft after clearing")
        page.locator("#studio-clear-undo").click()
        assert page.locator("#studio-note").input_value() == "Newer draft after clearing"
        assert page.locator(".studio-note-card").count() == 2
        assert "Edited in place" in page.locator(".studio-note-card").first.inner_text()
        assert page.locator(".studio-pin-pending").text_content() == "3"
        page.locator("#studio-note-cancel").click()
        page.locator("#studio-clear").click()
        page.reload()
        page.locator("#studio-open").click()
        assert page.locator(".studio-note-card").count() == 0
        assert page.locator("#studio-message").input_value() == "Keep this draft"
        assert page.locator("#studio-clear").is_disabled()
        assert page.locator("#studio-clear-undo").is_disabled()
    finally:
        _stop_server(server, thread, page)


def test_studio_late_entrypoints_refresh_preserves_cleared_pending_pin(page, tmp_path, monkeypatch):
    create_demo(tmp_path)
    app = create_app(tmp_path, studio=True, allowed_hosts=["127.0.0.1"])
    entered, release = threading.Event(), threading.Event()
    original = routes_studio.available_entrypoints

    def delayed_entrypoints():
        entered.set()
        assert release.wait(timeout=30), "entrypoint refresh was not released"
        return original()

    monkeypatch.setattr(routes_studio, "available_entrypoints", delayed_entrypoints)
    server, thread, port = _start_server(app)
    context = page.context.browser.new_context(viewport={"width": 1400, "height": 1000})
    tab = context.new_page()
    tab.add_init_script("""window.studioOpened = false;
      window.addEventListener('sqldash:studio-open', () => window.studioOpened = true);""")
    try:
        tab.goto(f"http://127.0.0.1:{port}/d/demo")
        tab.evaluate("localStorage.setItem('sqldash-ai-studio-tour-v1','seen')")
        tab.locator("#studio-open").click()
        assert entered.wait(timeout=5)
        tab.locator("#studio-pick").click()
        tab.locator(".tile").first.click(position={"x": 40, "y": 30})
        tab.locator("#studio-note").fill("Saved request")
        tab.locator("#studio-note-form button[type=submit]").click()
        tab.locator(".tile").first.click(position={"x": 200, "y": 30})
        tab.locator("#studio-note").fill("Unfinished request")
        tab.locator("#studio-clear").click()
        assert tab.locator(".studio-pin-pending").text_content() == "1"
        assert not tab.evaluate("window.studioOpened")
        release.set()
        tab.wait_for_function("window.studioOpened")
        assert tab.locator(".studio-pin-pending").count() == 1
        assert tab.locator(".studio-pin-pending").text_content() == "1"
        assert tab.locator("#studio-note").input_value() == "Unfinished request"
        assert tab.locator("#studio-note-form").is_visible()
        assert tab.locator(".studio-note-card").count() == 0
    finally:
        release.set()
        try:
            _stop_server(server, thread, tab)
        finally:
            context.close()


def test_dashboard_css_cannot_exfiltrate_or_restyle_the_chrome(page, tmp_path):
    create_demo(tmp_path)
    dashboard = tmp_path / ".sqldash" / "demo.yaml"
    dashboard.write_text(
        dashboard.read_text()
        + """
css: |
  .tile { outline: 3px solid rgb(1, 2, 3); background-image: url(https://exfil.invalid/tile.png); }
  header.topbar, .studio-panel { display: none; }
  @font-face { font-family: Leak; src: url(https://exfil.invalid/leak.woff2); unicode-range: U+41; }
  h1 { font-family: Leak, sans-serif; }
"""
    )
    app = create_app(tmp_path, allowed_hosts=["127.0.0.1"], studio=True)
    server, thread, port = _start_server(app)
    finished, failed, console, errors = [], {}, [], []
    page.on("requestfinished", lambda request: finished.append(request.url))
    page.on(
        "requestfailed",
        lambda request: failed.__setitem__(request.url, (request.failure or "")),
    )
    page.on("console", lambda message: console.append(message.text))
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        response = page.goto(f"http://127.0.0.1:{port}/d/demo")
        assert "img-src 'self' data: blob:" in response.headers["content-security-policy"]
        page.wait_for_selector(".tile")
        outline = page.locator(".tile").first.evaluate("el => getComputedStyle(el).outlineColor")
        assert outline == "rgb(1, 2, 3)"
        assert page.locator("header.topbar").is_visible()
        page.locator("#studio-open").click()
        assert page.locator("#studio-panel").is_visible()
        assert not any("exfil.invalid" in url for url in finished)
        leaks = {url: reason for url, reason in failed.items() if "exfil.invalid" in url}
        assert leaks
        assert all("csp" in reason.lower() for reason in leaks.values()), leaks
        blocked = [text for text in console if "Content Security Policy" in text]
        assert any("exfil.invalid" in text for text in blocked)
        assert page.evaluate("document.querySelector('meta[name=\"sqldash-token\"]')") is None
        assert app.state.api_token not in page.content()
        token = page.evaluate("(async () => (await import('/static/js/token.js')).apiToken())()")
        assert token == app.state.api_token
        assert not errors
    finally:
        _stop_server(server, thread, page)


def test_studio_refresh_repaints_the_page_tokens_without_a_reload(page, tmp_path):
    create_demo(tmp_path)
    dashboard = tmp_path / ".sqldash" / "demo.yaml"
    original = dashboard.read_text()
    app = create_app(tmp_path, allowed_hosts=["127.0.0.1"])
    server, thread, port = _start_server(app)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(f"http://127.0.0.1:{port}/d/demo")
        page.wait_for_selector(".tile")
        before = page.evaluate("getComputedStyle(document.body).backgroundColor")
        assert before != "rgb(12, 9, 24)"
        page.evaluate("window.__loaded = true; document.body.dataset.studioActive = 'true'")
        themed = (
            "css: |\n  :root { --page: rgb(12, 9, 24); }\n"
            "  .tile { outline: 2px solid rgb(1, 2, 3); }\n"
        )
        dashboard.write_text(original + themed)
        page.evaluate("import('/static/js/edit.js').then((m) => m.refreshDashboard())")
        page.wait_for_function(
            "getComputedStyle(document.body).backgroundColor === 'rgb(12, 9, 24)'"
        )
        assert page.evaluate("window.__loaded") is True
        outline = page.locator(".tile").first.evaluate("el => getComputedStyle(el).outlineColor")
        assert outline == "rgb(1, 2, 3)"
        dashboard.write_text(original)
        page.evaluate("import('/static/js/edit.js').then((m) => m.refreshDashboard())")
        page.wait_for_function(f"getComputedStyle(document.body).backgroundColor === '{before}'")
        assert page.evaluate("document.getElementById('dash-page')") is None
        assert page.evaluate("window.__loaded") is True
        assert not errors
    finally:
        _stop_server(server, thread, page)


def test_a_workspace_metrics_edit_reloads_only_that_repos_dashboards(page, tmp_path):
    """The watcher names a workspace repo's semantic layer `<repo>/metrics`, never
    bare `metrics`, so an open dashboard that only listened for the bare name kept
    rendering stale governed numbers after its repo's metrics.yaml changed (#552)."""
    for repo in ("r1", "r2"):
        (tmp_path / repo).mkdir()
        create_demo(tmp_path / repo)
    app = create_app(
        workspace=[("acme", tmp_path / "r1"), ("beta", tmp_path / "r2")],
        allowed_hosts=["127.0.0.1"],
    )
    server, thread, port = _start_server(app)
    try:
        time.sleep(1.0)
        page.goto(f"http://127.0.0.1:{port}/d/acme/demo")
        page.wait_for_selector(".tile")
        page.evaluate("""() => {
            window.__loaded = true;
            window.__seen = [];
            window.__ready = false;
            const source = new EventSource('/api/events');
            source.addEventListener('ready', () => { window.__ready = true; });
            source.onmessage = (e) => window.__seen.push(JSON.parse(e.data).name);
        }""")
        page.wait_for_function("window.__ready")
        beta_metrics = tmp_path / "r2" / ".sqldash" / "metrics.yaml"
        beta_metrics.write_text(beta_metrics.read_text() + "\n")
        page.wait_for_function("window.__seen.includes('beta/metrics')")
        page.wait_for_timeout(500)
        assert page.evaluate("window.__loaded") is True
        acme_metrics = tmp_path / "r1" / ".sqldash" / "metrics.yaml"
        with page.expect_navigation():
            acme_metrics.write_text(acme_metrics.read_text() + "\n")
        page.wait_for_selector(".tile")
        assert page.evaluate("window.__loaded") is None
    finally:
        _stop_server(server, thread, page)


def test_scrollable_table_tiles_fade_until_scrolled_to_the_end(page, tmp_path):
    create_demo(tmp_path)
    dashboard = tmp_path / ".sqldash" / "demo.yaml"
    text = dashboard.read_text()
    dashboard.write_text(
        text[: text.index("tiles:")]
        + "tiles:\n"
        + "  - {id: long, title: Long, sql: 'SELECT i FROM range(60) t(i)', chart: table}\n"
        + "  - {id: short, title: Short, sql: 'SELECT 1 AS n', chart: table}\n"
    )
    app = create_app(tmp_path, allowed_hosts=["127.0.0.1"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/demo")
        page.wait_for_selector('.tile[data-tile-id="long"] .table-wrap.has-more')
        assert not page.locator('.tile[data-tile-id="short"] .table-wrap.has-more').count()
        page.locator('.tile[data-tile-id="long"] .table-wrap').evaluate(
            "el => el.scrollTo(0, el.scrollHeight)"
        )
        page.wait_for_selector('.tile[data-tile-id="long"] .table-wrap:not(.has-more)')
    finally:
        _stop_server(server, thread, page)


def test_css_root_tokens_paint_the_page_while_the_rest_stays_scoped(page, tmp_path):
    create_demo(tmp_path)
    dashboard = tmp_path / ".sqldash" / "demo.yaml"
    dashboard.write_text(
        dashboard.read_text()
        + """
css: |
  body { background: rgb(12, 9, 24); }
  --accent: rgb(190, 145, 255);
  .tile { outline: 2px solid rgb(1, 2, 3); }
"""
    )
    app = create_app(tmp_path, allowed_hosts=["127.0.0.1"])
    server, thread, port = _start_server(app)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(f"http://127.0.0.1:{port}/d/demo")
        page.wait_for_selector(".tile")
        body = page.evaluate("getComputedStyle(document.body).backgroundColor")
        assert body == "rgb(12, 9, 24)"
        accent = page.evaluate(
            "getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()"
        )
        assert accent == "rgb(190, 145, 255)"
        outline = page.locator(".tile").first.evaluate("el => getComputedStyle(el).outlineColor")
        assert outline == "rgb(1, 2, 3)"
        assert not errors
    finally:
        _stop_server(server, thread, page)


def test_theme_root_blocks_and_custom_tokens_apply_in_each_theme(page, tmp_path):
    """`:root[data-theme]` blocks stayed inside the scope where `:root` never matches, and
    a custom `--crawl` was dropped, so a stroked title rendered as a 0px stroke."""
    create_demo(tmp_path)
    dashboard = tmp_path / ".sqldash" / "demo.yaml"
    dashboard.write_text(
        dashboard.read_text()
        + """
css: |
  :root, :root[data-theme] { --accent: rgb(255, 0, 0); --crawl: rgb(255, 232, 31); }
  :root[data-theme="light"] { --accent: rgb(0, 255, 0); --crawl: rgb(0, 0, 255); }
  :root[data-theme="dark"] { --page: rgb(1, 2, 3); }
  .dash-title-row h1 { -webkit-text-stroke: 2px var(--crawl); }
"""
    )
    app = create_app(tmp_path, allowed_hosts=["127.0.0.1"])
    server, thread, port = _start_server(app)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    read = """(theme) => {
        document.documentElement.dataset.theme = theme;
        const root = getComputedStyle(document.documentElement);
        const title = getComputedStyle(document.querySelector('.dash-title-row h1'));
        return [
            root.getPropertyValue('--accent').trim(),
            root.getPropertyValue('--crawl').trim(),
            getComputedStyle(document.body).backgroundColor,
            title.webkitTextStrokeWidth,
            title.webkitTextStrokeColor,
        ];
    }"""
    try:
        page.goto(f"http://127.0.0.1:{port}/d/demo")
        page.wait_for_selector(".tile")
        dark = page.evaluate(read, "dark")
        assert dark == ["rgb(255, 0, 0)", "", "rgb(1, 2, 3)", "2px", "rgb(255, 232, 31)"]
        light = page.evaluate(read, "light")
        assert light[:2] == ["rgb(0, 255, 0)", ""]
        assert light[2] != "rgb(1, 2, 3)"
        assert light[3:] == ["2px", "rgb(0, 0, 255)"]
        assert not errors
    finally:
        _stop_server(server, thread, page)


def test_page_prefixed_rules_reach_tiles_but_nothing_outside_the_dashboard(page, tmp_path):
    create_demo(tmp_path)
    dashboard = tmp_path / ".sqldash" / "demo.yaml"
    dashboard.write_text(
        dashboard.read_text()
        + """
css: |
  :root[data-theme] .tile { outline: 2px solid rgb(1, 2, 3); }
  :root[data-theme="light"] .tile { outline-color: rgb(4, 5, 6); }
  html body header.topbar, :root[data-theme] .studio-panel { display: none; }
  html body .brand, :root[data-theme] .tile .tile-head h3 { color: rgb(7, 8, 9); }
"""
    )
    app = create_app(tmp_path, allowed_hosts=["127.0.0.1"], studio=True)
    server, thread, port = _start_server(app)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(f"http://127.0.0.1:{port}/d/demo")
        page.wait_for_selector(".tile")
        page.evaluate("document.documentElement.dataset.theme = 'dark'")
        tile = page.locator(".tile").first
        assert tile.evaluate("el => getComputedStyle(el).outlineColor") == "rgb(1, 2, 3)"
        title = tile.locator(".tile-head h3").evaluate("el => getComputedStyle(el).color")
        assert title == "rgb(7, 8, 9)"
        assert page.locator("header.topbar").is_visible()
        brand = page.locator(".brand").evaluate("el => getComputedStyle(el).color")
        assert brand != "rgb(7, 8, 9)"
        page.locator("#studio-open").click()
        assert page.locator("#studio-panel").is_visible()
        page.evaluate("document.documentElement.dataset.theme = 'light'")
        assert tile.evaluate("el => getComputedStyle(el).outlineColor") == "rgb(4, 5, 6)"
        assert not errors
    finally:
        _stop_server(server, thread, page)


_SCOPE_NESTING = """:scope {
  & { padding-top: 4px }
  padding-top: 2px;
  &:hover { padding-left: 9px }
  padding-left: 3px;
  @media (min-width: 1px) { padding-bottom: 4px }
  padding-bottom: 2px;
  & { --accent: rgb(0, 0, 255) }
  --accent: rgb(255, 0, 0);
}"""


def test_scope_css_cascades_like_the_same_css_written_as_plain_nesting(page, tmp_path):
    """#656: the served `:scope` block put declarations before nested blocks, so a bare
    `&` or nested `@media` written first won on the dashboard while plain nesting of the
    same source let the later declaration win."""
    create_demo(tmp_path)
    dashboard = tmp_path / ".sqldash" / "demo.yaml"
    indented = "\n".join(f"  {line}" for line in _SCOPE_NESTING.splitlines())
    dashboard.write_text(dashboard.read_text() + "css: |\n" + indented + "\n")
    app = create_app(tmp_path, allowed_hosts=["127.0.0.1"])
    server, thread, port = _start_server(app)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    props = ["padding-top", "padding-left", "padding-bottom", "--accent"]
    read = f"el => {json.dumps(props)}.map(p => getComputedStyle(el).getPropertyValue(p).trim())"
    try:
        page.goto(f"http://127.0.0.1:{port}/d/demo")
        page.wait_for_selector(".tile")
        page.evaluate(
            """(css) => {
                const style = document.createElement('style');
                style.textContent = css;
                document.head.append(style);
                const ref = document.createElement('div');
                ref.id = 'plain-nesting';
                ref.style.height = '40px';
                document.querySelector('main.container').before(ref);
            }""",
            _SCOPE_NESTING.replace(":scope", "#plain-nesting", 1),
        )
        page.mouse.move(0, 0)
        scoped = page.locator("main.container").evaluate(read)
        plain = page.locator("#plain-nesting").evaluate(read)
        assert scoped == plain == ["2px", "3px", "2px", "rgb(255, 0, 0)"]
        page.hover("main.container .tile")
        assert page.locator("main.container").evaluate(read)[1] == "9px"
        page.hover("#plain-nesting")
        assert page.locator("#plain-nesting").evaluate(read)[1] == "9px"
        accent = page.evaluate(
            "getComputedStyle(document.documentElement).getPropertyValue('--accent').trim()"
        )
        assert accent == "rgb(255, 0, 0)"
        assert not errors
    finally:
        _stop_server(server, thread, page)


def test_consecutive_sql_tiles_keep_results_and_refresh_saved_queries(page, tmp_path_factory):
    root = tmp_path_factory.mktemp("keep-query")
    path = root / "d.yaml"
    path.write_text(
        "# Keep this comment\n"
        "title: Query workspace\nsource: {type: duckdb}\n"
        "tiles:\n  - title: Anchor\n    sql: SELECT 1 AS n\n"
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    posts = []

    @app.middleware("http")
    async def delay_creation(request, call_next):
        if request.method == "POST" and request.url.path.endswith("/tiles"):
            posts.append(request.headers.get("if-match"))
            await asyncio.sleep(0.25)
        return await call_next(request)

    server, thread, port = _start_server(app)
    try:
        url = f"http://127.0.0.1:{port}/d/d/query"
        page.goto(url, wait_until="load")
        page.evaluate("ace.edit('sql-editor').setValue('SELECT 42 AS answer', -1)")
        page.locator("#run-btn").click()
        page.wait_for_selector("#results-body table")
        page.locator("#qb-title").fill("Answer")
        with page.expect_response(lambda r: r.request.method == "POST" and "/tiles" in r.url):
            page.evaluate(
                """() => {
                  const button = document.getElementById('qb-add');
                  button.click();
                  document.getElementById('text-markdown').dispatchEvent(new Event('input'));
                  button.click();
                }"""
            )
        page.wait_for_function("() => !document.getElementById('qb-another').disabled")
        assert page.url == url
        assert len(posts) == 1
        assert page.locator("#qb-add").is_disabled()
        assert "42" in page.locator("#results-body").inner_text()
        assert page.evaluate("ace.edit('sql-editor').getValue()") == "SELECT 42 AS answer"
        assert page.locator('#query-picker option[value="answer"]').count() == 1
        page.locator("#qb-another").click()
        with page.expect_response(lambda r: r.request.method == "POST" and "/tiles" in r.url):
            page.locator("#qb-add").click()
        page.wait_for_function("() => !document.getElementById('qb-another').disabled")
        dash, text, _ = DashboardStore(root).load("d")
        assert len(posts) == 2
        assert posts[0] != posts[1]
        assert [t.id for t in dash.tiles] == ["anchor", "answer", "answer_2"]
        assert dash.tiles[1].query == dash.tiles[2].query == "answer"
        assert text.startswith("# Keep this comment\n")
        assert "sql: SELECT 1 AS n" in text
        assert page.locator("#qb-save-feedback a").get_attribute("href") == "/d/d"
    finally:
        _stop_server(server, thread, page)


def test_refresh_after_add_conflict_preserves_draft_and_external_sql(page, tmp_path_factory):
    root = tmp_path_factory.mktemp("query-conflict")
    path = root / "d.yaml"
    path.write_text(
        "title: Conflict\nsource: {type: duckdb}\n"
        "queries:\n  existing: SELECT 1 AS n\n"
        "tiles:\n  - title: Anchor\n    query: existing\n"
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/d/query", wait_until="load")
        page.select_option("#query-picker", "existing", force=True)
        page.locator("#qb-title").fill("Existing")
        page.locator("#run-btn").click()
        page.wait_for_selector("#results-body table")
        path.write_text(path.read_text().replace("SELECT 1 AS n", "SELECT 99 AS n"))
        with page.expect_response(
            lambda r: r.request.method == "POST" and "/tiles" in r.url
        ) as response:
            page.locator("#qb-add").click()
        assert response.value.status == 409
        page.wait_for_selector("#qb-refresh:visible")
        assert page.locator("#qb-add").is_disabled()
        page.locator("#qb-refresh").click()
        page.wait_for_function("() => !document.getElementById('qb-add').disabled")
        assert page.locator("#qb-title").input_value() == "Existing"
        assert page.evaluate("ace.edit('sql-editor').getValue()") == "SELECT 1 AS n"
        assert page.locator("#results-body table").count() == 1
        assert len(DashboardStore(root).load("d")[0].tiles) == 1
        page.locator("#qb-add").click()
        page.wait_for_function("() => !document.getElementById('qb-another').hidden")
        dash, _, _ = DashboardStore(root).load("d")
        assert dash.queries["existing"] == "SELECT 99 AS n"
        assert dash.queries["existing_2"].strip() == "SELECT 1 AS n"
        assert len(dash.tiles) == 2
    finally:
        _stop_server(server, thread, page)


def test_successful_add_with_failed_refresh_cannot_be_added_twice(page, tmp_path_factory):
    root = tmp_path_factory.mktemp("query-refresh")
    (root / "d.yaml").write_text(
        "title: Refresh\nsource: {type: duckdb}\n"
        "tiles:\n  - title: Anchor\n    sql: SELECT 1 AS n\n"
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/d/query", wait_until="load")
        page.locator('#mode-toggle [data-mode="text"]').click()
        page.locator("#text-markdown").fill("Keep this draft")
        page.route(
            "**/api/dashboards/d",
            lambda route: route.fulfill(status=503, json={"detail": "temporarily unavailable"}),
            times=1,
        )
        page.locator("#qb-add").click()
        page.wait_for_selector("#qb-refresh:visible")
        assert "Tile added" in page.locator("#qb-save-status").inner_text()
        assert page.locator("#qb-add").is_disabled()
        assert page.locator("#qb-another").is_disabled()
        assert len(DashboardStore(root).load("d")[0].tiles) == 2
        page.locator("#qb-refresh").click()
        page.wait_for_function("() => !document.getElementById('qb-another').disabled")
        assert page.locator("#text-markdown").input_value() == "Keep this draft"
        assert len(DashboardStore(root).load("d")[0].tiles) == 2
    finally:
        _stop_server(server, thread, page)


def test_workspace_recovers_independent_tabs_without_rerunning(page, served):
    page.goto(f"{served}/d/demo/workspace", wait_until="networkidle")
    first = page.frames[-1]
    first.wait_for_function(
        "document.querySelector('[data-ready=true]') && ace.edit('sql-editor').getValue() === ''"
    )
    first.evaluate("ace.edit('sql-editor').setValue('SELECT 11 AS eleven', -1)")
    first.locator("#run-btn").click()
    first.wait_for_selector("#results-body table")
    page.locator("#new-query").click()
    page.wait_for_function("document.querySelectorAll('#workspace-frames iframe').length === 2")
    second = page.frames[-1]
    second.wait_for_function(
        "document.querySelector('[data-ready=true]') && ace.edit('sql-editor').getValue() === ''"
    )
    second.evaluate("ace.edit('sql-editor').setValue('SELECT 22 AS twenty_two', -1)")
    second.locator("#run-btn").click()
    second.wait_for_selector("#results-body table")
    page.locator("#query-tabs [role=tab]").first.click()
    assert "11" in first.locator("#results-body").inner_text()
    assert "22" in second.locator("#results-body").inner_text()
    page.reload(wait_until="networkidle")
    for frame in page.frames[1:]:
        frame.wait_for_function(
            "document.querySelector('[data-ready=true]') && "
            "ace.edit('sql-editor').getValue().includes('SELECT')"
        )
    assert {f.evaluate("ace.edit('sql-editor').getValue()") for f in page.frames[1:]} == {
        "SELECT 11 AS eleven",
        "SELECT 22 AS twenty_two",
    }
    assert all(f.locator("#results-body table").count() == 0 for f in page.frames[1:])
    page.locator(".tab-action[aria-label^=Close]").first.click()
    page.locator("#query-dialog-confirm").click()
    page.wait_for_function("document.querySelectorAll('#workspace-frames iframe').length === 1")
    page.reload(wait_until="networkidle")
    assert page.locator("#workspace-frames iframe").count() == 1


def test_workspace_library_and_two_visualizations_share_one_query(page, served):
    page.goto(f"{served}/d/demo/workspace", wait_until="networkidle")
    page.evaluate(
        "localStorage.removeItem('sqldash-workspace-v1:' + "
        "document.getElementById('workspace').dataset.identity)"
    )
    page.reload(wait_until="networkidle")
    frame = page.frames[-1]
    frame.wait_for_function(
        "document.querySelector('[data-ready=true]') && ace.edit('sql-editor').getValue() === ''"
    )
    sql = "SELECT label, amount FROM (VALUES ('north', 10), ('south', 20)) t(label, amount)"
    frame.evaluate('(sql) => ace.edit("sql-editor").setValue(sql, -1)', sql)
    frame.locator("#workspace-save").click()
    page.locator("#library-name").fill("Stack workflow")
    page.locator("#library-confirm").click()
    page.wait_for_function("!document.getElementById('library-dialog').open")
    frame.locator("#run-btn").click()
    frame.wait_for_selector("#results-body table")
    frame.locator(".workspace-chart-footer button").click()
    frame.locator("#qb-title").fill("Stack table")
    frame.locator("#qb-add").click()
    frame.wait_for_function(
        "!document.getElementById('qb-another').hidden && "
        "!document.getElementById('qb-another').disabled"
    )
    frame.get_by_role("button", name="Close add to dashboard").click()
    frame.locator("#qb-type button", has_text="Bar").click()
    frame.locator(".workspace-chart-footer button").click()
    frame.locator("#qb-title").fill("Stack bar")
    frame.locator("#qb-add").click()
    frame.wait_for_function(
        "!document.getElementById('qb-another').hidden && "
        "!document.getElementById('qb-another').disabled"
    )
    frame.get_by_role("button", name="Close add to dashboard").click()
    dashboard = page.request.get(f"{served}/api/dashboards/demo").json()["dashboard"]
    table = next(t for t in dashboard["tiles"] if t["title"] == "Stack table")
    bar = next(t for t in dashboard["tiles"] if t["title"] == "Stack bar")
    assert table["query"] == bar["query"]
    assert table["chart"]["type"] == "table"
    assert bar["chart"]["type"] == "bar"
    assert dashboard["queries"][table["query"]].strip() == sql
    page.locator("#library-browser > details > summary").click()
    page.locator("#library-entries .workspace-query", has_text="Stack workflow").click()
    page.wait_for_function("document.querySelectorAll('#workspace-frames iframe').length === 2")
    page.frames[-1].wait_for_function(
        "document.querySelector('[data-ready=true]') && "
        "ace.edit('sql-editor').getValue().includes('VALUES')"
    )
    assert page.frames[-1].locator("#results-body table").count() == 0
    page.get_by_role("button", name="Delete library query Stack workflow").click()
    page.locator("#library-confirm").click()
    page.wait_for_function("!document.getElementById('library-dialog').open")
    assert "VALUES" in page.frames[-1].evaluate("ace.edit('sql-editor').getValue()")


def test_sorted_workspace_download_neutralizes_spreadsheet_formulas(page, served):
    """Sorting switches Download CSV to a file built in the browser, which wrote
    `=HYPERLINK(...)` raw while the server export quoted it."""
    page.goto(f"{served}/d/demo/workspace", wait_until="networkidle")
    frame = page.frames[-1]
    frame.wait_for_function("document.querySelector('[data-ready=true]')")
    sql = (
        "SELECT * FROM (VALUES ('=HYPERLINK(\"http://example.invalid\",\"x\")', -3.5, '-2.25'), "
        "('@cmd', 1, '-1+2')) t(\"=head\", n, s)"
    )
    frame.evaluate('(sql) => ace.edit("sql-editor").setValue(sql, -1)', sql)
    frame.locator("#run-btn").click()
    frame.wait_for_selector("#results-body table")
    frame.locator('[data-sort-column="1"]').click()
    with page.expect_download() as download:
        frame.locator("#csv-btn").click()
    assert download.value.suggested_filename == "sorted-results.csv"
    assert Path(download.value.path()).read_bytes() == (
        b'"\'=head","n","s"\r\n'
        b'"\'=HYPERLINK(""http://example.invalid"",""x"")","-3.5","-2.25"\r\n'
        b'"\'@cmd","1.0","\'-1+2"'
    )


def test_workspace_result_controls_keep_rows_and_drafts_independent(page, served):
    page.goto(f"{served}/d/demo/workspace", wait_until="networkidle")
    page.evaluate(
        "localStorage.removeItem('sqldash-workspace-v1:' + "
        "document.getElementById('workspace').dataset.identity)"
    )
    page.reload(wait_until="networkidle")
    frame = page.frames[-1]
    frame.wait_for_function(
        "document.querySelector('[data-ready=true]') && ace.edit('sql-editor').getValue() === ''"
    )
    sql = "SELECT name, n FROM (VALUES ('two', 2), ('null', NULL), ('one', 1)) t(name, n)"
    frame.evaluate('(sql) => ace.edit("sql-editor").setValue(sql, -1)', sql)
    frame.locator("#run-btn").click()
    frame.wait_for_selector("#results-body table")
    last_run = frame.locator(".workspace-last-run").get_attribute("datetime")
    assert last_run
    frame.locator('[data-sort-column="1"]').click()
    assert frame.locator("#results-body tbody tr td:last-child").all_text_contents() == [
        "1",
        "2",
        "null",
    ]
    with page.expect_download() as download:
        frame.locator("#csv-btn").click()
    assert Path(download.value.path()).read_text().splitlines() == [
        '"name","n"',
        '"one","1"',
        '"two","2"',
        '"null",""',
    ]
    frame.locator('[data-sort-column="1"]').click()
    assert frame.locator("#results-body tbody tr td:last-child").all_text_contents() == [
        "2",
        "1",
        "null",
    ]
    frame.locator('[data-sort-column="1"]').click()
    assert frame.locator("#results-body tbody tr td:last-child").all_text_contents() == [
        "2",
        "null",
        "1",
    ]
    assert frame.locator(".workspace-last-run").get_attribute("datetime") == last_run
    before = frame.locator("#results-body th").nth(1).bounding_box()["width"]
    handle = frame.get_by_role("separator", name="Resize name", exact=True)
    handle.focus()
    handle.press("ArrowRight")
    assert frame.locator("#results-body th").nth(1).bounding_box()["width"] > before
    frame.locator("#toggle-builder").click()
    assert not frame.locator(".chart-side").is_visible()
    frame.locator("#toggle-builder").click()
    assert frame.locator(".chart-side").is_visible()
    page.locator("#new-query").click()
    other = page.frames[-1]
    other.wait_for_function(
        "document.querySelector('[data-ready=true]') && ace.edit('sql-editor').getValue() === ''"
    )
    page.locator(".query-tab:has([aria-selected=true]) select").select_option("text")
    other.locator("#text-markdown").fill("Preserved text mode")
    assert other.locator(".workspace-chart-footer button").is_enabled()
    assert other.locator("#workspace-save").is_disabled()
    page.locator(".query-tab:has([aria-selected=true]) select").select_option("metric")
    assert other.locator("#metric-picker").count() == 1
    page.locator("#query-tabs [role=tab]").first.click()
    assert frame.locator(".workspace-last-run").get_attribute("datetime") == last_run
    assert frame.locator("#results-body tbody tr").count() == 3
    page.set_viewport_size({"width": 430, "height": 932})
    frame.wait_for_function(
        "document.querySelector('#results-body table').getBoundingClientRect().width "
        "<= document.getElementById('results-body').clientWidth + 1"
    )
    page.set_viewport_size({"width": 1400, "height": 1000})


def test_workspace_saved_query_link_preserves_unsaved_work(page, served):
    page.goto(f"{served}/d/demo/workspace", wait_until="networkidle")
    page.evaluate(
        "localStorage.removeItem('sqldash-workspace-v1:' + "
        "document.getElementById('workspace').dataset.identity)"
    )
    page.reload(wait_until="networkidle")
    frame = page.frames[-1]
    frame.wait_for_selector(".workspace-editor[data-ready=true]")
    frame.evaluate("ace.edit('sql-editor').setValue('SELECT 7 AS saved', -1)")
    frame.locator("#workspace-save").click()
    page.locator("#library-name").fill("Linked query")
    page.locator("#library-confirm").click()
    page.wait_for_function("!document.getElementById('library-dialog').open")
    query_id = page.locator(".library-row.active").get_attribute("data-id")
    frame.evaluate("ace.edit('sql-editor').setValue('SELECT 8 AS unsaved', -1)")
    page.wait_for_timeout(100)
    page.goto(f"{served}/d/demo/workspace?query={query_id}", wait_until="networkidle")
    assert page.locator("#workspace-frames iframe").count() == 1
    frame = page.frames[-1]
    frame.wait_for_selector(".workspace-editor[data-ready=true]")
    assert frame.evaluate("ace.edit('sql-editor').getValue()") == "SELECT 8 AS unsaved"
    assert frame.locator("#results-body table").count() == 0
    assert frame.locator("#mode-toggle").is_hidden()
    page.locator(".query-tab select").select_option("text")
    frame.locator("#text-markdown").fill("My text draft")
    assert page.locator(".query-tab .dd-label").inner_text() == "TEXT"
    page.evaluate(
        "localStorage.removeItem('sqldash-workspace-v1:' + "
        "document.getElementById('workspace').dataset.identity)"
    )
    page.goto(f"{served}/d/demo/workspace?query={query_id}", wait_until="networkidle")
    page.wait_for_function(
        "document.querySelector('#query-tabs [aria-selected=true]')?.textContent === 'Linked query'"
    )
    assert page.locator("#workspace-frames iframe").count() == 1
    frame = page.frames[-1]
    frame.wait_for_selector(".workspace-editor[data-ready=true]")
    assert frame.evaluate("ace.edit('sql-editor').getValue()").strip() == "SELECT 7 AS saved"
    assert frame.locator("#results-body table").count() == 0


def test_workspace_metric_explorer_preserves_sql_and_runs_metric(page, served):
    page.goto(f"{served}/d/demo/workspace", wait_until="networkidle")
    sql_frame = page.frames[-1]
    sql_frame.locator(".workspace-editor[data-ready=true]").wait_for()
    sql_frame.evaluate("ace.edit('sql-editor').setValue('SELECT 23 AS keep_me', -1)")
    page.locator("#metric-browser > details > summary").click()
    page.locator("#metric-search").fill("sales")
    page.locator('[data-metric="revenue"]').click()
    frame = page.frames[-1]
    frame.locator(".workspace-editor[data-ready=true]").wait_for()
    assert frame.locator("#metric-pane").is_visible()
    assert frame.locator("#metric-picker").input_value() == "revenue"
    assert "Revenue" in frame.locator("#metric-pane .dd-label").first.inner_text()
    assert frame.locator("#results-body table").count() == 0
    assert sql_frame.evaluate("ace.edit('sql-editor').getValue()") == "SELECT 23 AS keep_me"
    frame.locator('#metric-dims input[value="region"]').check()
    frame.locator("#run-btn").click()
    frame.locator("#results-body tbody tr").first.wait_for()
    assert "region" in frame.locator("#results-body thead").inner_text()
    page.reload(wait_until="networkidle")
    frame = page.frames[-1]
    frame.locator(".workspace-editor[data-ready=true]").wait_for()
    assert frame.locator("#metric-picker").input_value() == "revenue"
    assert "Revenue" in frame.locator("#metric-pane .dd-label").first.inner_text()
    assert frame.locator('#metric-dims input[value="region"]').is_checked()
    assert frame.locator("#results-body table").count() == 0


# ---------- #577: a change made while the page was not subscribed ----------
#
# The page renders its dashboard, then subscribes to /api/events. Two ways a
# change used to be announced to nobody, and a test for each:
#
# - the gap between render and subscribe: the page's first /api/events request
#   is held, the write lands and the watcher emits it to a probe subscriber, and
#   only then is the request released;
# - an outage: the server is stopped and restarted on the same port, so a write
#   made while it was down never produces an event at all.
#
# Either way the only thing that can tell the page is the `ready` handshake.
# Sleeps appear only where a test has to show that nothing happens.


def _serve(app, port=None):
    """An embedded server that can be stopped with a stream open and restarted
    on the same port. uvicorn otherwise waits forever for the SSE response."""
    import uvicorn

    from sqldash.snapshot import _free_port

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port or _free_port(),
        log_level="error",
        timeout_graceful_shutdown=1,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    _until(lambda: server.started, "the embedded server to start")
    return server, thread, config.port


def _halt(server, thread):
    server.should_exit = True
    thread.join(timeout=15)
    assert not thread.is_alive(), "embedded server did not stop"


def _until(predicate, what, timeout=15):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out waiting for {what}")
        time.sleep(0.05)


def _count_subscriptions(app):
    """Record every subscription the page opens, including reconnects."""
    watcher = app.state.watcher
    opened = []
    subscribe = watcher.subscribe
    watcher.subscribe = lambda: (opened.append(time.monotonic()), subscribe())[1]
    return opened


def _announced_before_subscribe(app, write):
    """Run `write` and return once the watcher has emitted it to a probe queue."""
    watcher = app.state.watcher
    probe = type(watcher).subscribe(watcher)
    try:
        write()
        _until(lambda: probe.qsize() > 0, "the watcher to announce the write")
    finally:
        watcher.unsubscribe(probe)


def _hold_first_subscription(page):
    """Hold the page's first /api/events request; let later ones through.

    Release with `_release`, which also removes the route: the sync API only runs
    a route callback while the test thread is inside a Playwright call, so a
    reconnect arriving while the test polls in `time.sleep` would hang."""
    held = []

    def route(r):
        if held:
            r.continue_()
        else:
            held.append(r)

    page.route("**/api/events*", route)
    return held


def _release(page, held):
    held[0].continue_()
    page.unroute("**/api/events*")


def _count_runs(page):
    runs = []
    page.on("request", lambda r: runs.append(r.url) if r.url.endswith("/api/run") else None)
    return runs


def _watch_toasts(page):
    page.evaluate("""() => {
        window.__toasts = [];
        new MutationObserver((records) => {
            for (const r of records) {
                for (const n of r.addedNodes) window.__toasts.push(n.textContent);
            }
        }).observe(document.getElementById('toasts'), {childList: true});
    }""")


def _retitle(path, title):
    text = path.read_text()
    path.write_text(re.sub(r"(?m)^title: .*$", f"title: {title}", text, count=1))


def _restart(app, server, thread, port, while_down=None):
    """Stop the server, optionally change files while it is down, start it again."""
    _halt(server, thread)
    if while_down is not None:
        while_down()
    return _serve(app, port)


def test_a_write_between_render_and_subscribe_refreshes_the_page_once(page, tmp_path):
    create_demo(tmp_path)
    dashboard = tmp_path / ".sqldash" / "demo.yaml"
    app = create_app(tmp_path, allowed_hosts=["127.0.0.1"])
    opened = _count_subscriptions(app)
    server, thread, port = _serve(app)
    try:
        time.sleep(1.0)
        held = _hold_first_subscription(page)
        page.goto(f"http://127.0.0.1:{port}/d/demo")
        page.wait_for_selector(".tile")
        page.evaluate("window.__loaded = true")
        _until(lambda: held, "the page to request /api/events")
        assert not opened, "the page subscribed before it was released"
        _announced_before_subscribe(app, lambda: _retitle(dashboard, "Missed Once"))
        with page.expect_navigation():
            _release(page, held)
        page.wait_for_selector(".tile")
        assert "Missed Once" in page.locator("#dash-title").text_content()

        # The replacement page's handshake finds nothing new: one refresh, no loop.
        page.evaluate("window.__loaded = true")
        _until(lambda: len(opened) >= 2, "the refreshed page to subscribe")
        page.wait_for_timeout(1500)
        assert page.evaluate("window.__loaded") is True
    finally:
        page.unroute_all(behavior="ignoreErrors")
        page.goto("about:blank")
        _halt(server, thread)


def test_a_write_during_an_outage_is_caught_on_reconnect_and_a_quiet_one_is_not(page, tmp_path):
    create_demo(tmp_path)
    dashboard = tmp_path / ".sqldash" / "demo.yaml"
    app = create_app(tmp_path, allowed_hosts=["127.0.0.1"])
    opened = _count_subscriptions(app)
    server, thread, port = _serve(app)
    try:
        time.sleep(1.0)
        runs = _count_runs(page)
        page.goto(f"http://127.0.0.1:{port}/d/demo")
        _wait_tiles(page)
        _until(lambda: opened, "the first subscription")
        page.evaluate("window.__loaded = true")

        # A reconnect with nothing changed must not reload or rerun a query.
        ran = len(runs)
        server, thread, port = _restart(app, server, thread, port)
        _until(lambda: len(opened) >= 2, "the quiet reconnect")
        page.wait_for_timeout(1500)
        assert page.evaluate("window.__loaded") is True, "a no-change reconnect reloaded"
        assert len(runs) == ran, "a no-change reconnect reran SQL"

        # A write while the server was down never makes an event; the handshake
        # on reconnect is the only thing that can catch it.
        with page.expect_navigation(timeout=20_000):
            server, thread, port = _restart(
                app, server, thread, port, lambda: _retitle(dashboard, "Written While Down")
            )
        page.wait_for_selector(".tile")
        assert "Written While Down" in page.locator("#dash-title").text_content()
    finally:
        page.goto("about:blank")
        _halt(server, thread)


def test_an_editor_is_told_once_and_keeps_its_etag_for_the_409(page, tmp_path):
    """Edit mode must not reload, must not rerun SQL, and must not adopt the new
    etag: the stale edit's next save has to meet the explicit conflict path."""
    create_demo(tmp_path)
    dashboard = tmp_path / ".sqldash" / "demo.yaml"
    app = create_app(tmp_path, allowed_hosts=["127.0.0.1"])
    opened = _count_subscriptions(app)
    server, thread, port = _serve(app)
    try:
        time.sleep(1.0)
        runs = _count_runs(page)
        held = _hold_first_subscription(page)
        page.goto(f"http://127.0.0.1:{port}/d/demo?edit=1")
        _wait_tiles(page)
        assert page.evaluate("document.body.classList.contains('editing')")
        page.evaluate("window.__loaded = true")
        _watch_toasts(page)
        before = page.evaluate("import('/static/js/runner.js').then((m) => m.getEtag())")
        ran = len(runs)
        _until(lambda: held, "the page to request /api/events")
        _announced_before_subscribe(app, lambda: _retitle(dashboard, "Changed Under The Editor"))
        _release(page, held)
        page.wait_for_function("window.__toasts.some((t) => t.includes('changed on disk'))")
        assert page.evaluate("window.__loaded") is True
        assert len(runs) == ran
        after = page.evaluate("import('/static/js/runner.js').then((m) => m.getEtag())")
        assert after == before, "the editor adopted the external etag"

        # The same revision, seen again by the next handshake, is not told twice.
        subscribed = len(opened)
        server, thread, port = _restart(app, server, thread, port)
        _until(lambda: len(opened) > subscribed, "the reconnect")
        page.wait_for_timeout(1500)
        told = page.evaluate("window.__toasts.filter((t) => t.includes('changed on disk')).length")
        assert told == 1
        assert page.evaluate("window.__loaded") is True

        # And a save from the stale page is refused rather than written over it.
        status = page.evaluate(
            """async (etag) => {
                const {apiToken} = await import('/static/js/token.js');
                const r = await fetch('/api/dashboards/demo/meta', {method: 'PATCH',
                    headers: {'Content-Type': 'application/json', 'If-Match': etag,
                              'X-Sqldash-Token': apiToken()},
                    body: JSON.stringify({description: 'stale edit'})});
                return r.status;
            }""",
            after,
        )
        assert status == 409
    finally:
        page.unroute_all(behavior="ignoreErrors")
        page.goto("about:blank")
        _halt(server, thread)


def test_a_missed_metrics_edit_reloads_only_its_own_repos_dashboard(page, tmp_path):
    for repo in ("r1", "r2"):
        (tmp_path / repo).mkdir()
        create_demo(tmp_path / repo)
    app = create_app(
        workspace=[("acme", tmp_path / "r1"), ("beta", tmp_path / "r2")],
        allowed_hosts=["127.0.0.1"],
    )
    opened = _count_subscriptions(app)
    server, thread, port = _serve(app)
    beta = tmp_path / "r2" / ".sqldash" / "metrics.yaml"
    acme = tmp_path / "r1" / ".sqldash" / "metrics.yaml"
    try:
        time.sleep(1.0)
        page.goto(f"http://127.0.0.1:{port}/d/acme/demo")
        page.wait_for_selector(".tile")
        _until(lambda: opened, "the first subscription")
        page.evaluate("window.__loaded = true")

        subscribed = len(opened)
        server, thread, port = _restart(
            app, server, thread, port, lambda: beta.write_text(beta.read_text() + "\n")
        )
        _until(lambda: len(opened) > subscribed, "the reconnect")
        page.wait_for_timeout(1500)
        assert page.evaluate("window.__loaded") is True, "another repo's metrics reloaded"

        with page.expect_navigation(timeout=20_000):
            server, thread, port = _restart(
                app, server, thread, port, lambda: acme.write_text(acme.read_text() + "\n")
            )
        page.wait_for_selector(".tile")
    finally:
        page.goto("about:blank")
        _halt(server, thread)


def test_large_results_render_a_page_at_a_time(page, served):
    """A 10,000-row result used to become one table cell per value, twice (the
    grid and the chart preview): 690,000 cells and 4M listeners for a wide
    Snowflake table, which nearly took the tab down. Both render a page and
    offer more; sorting still sees every row and keeps what was revealed."""
    sql = "SELECT i AS n, i % 7 AS bucket FROM range(1200) t(i)"
    page.goto(f"{served}/d/demo/workspace", wait_until="networkidle")
    page.evaluate("localStorage.clear()")
    page.reload(wait_until="networkidle")
    frame = page.locator("#workspace-frames iframe:not([hidden])").element_handle().content_frame()
    frame.wait_for_selector(".workspace-editor[data-ready=true]")
    frame.evaluate("sql => ace.edit('sql-editor').setValue(sql, 1)", sql)
    frame.locator("#run-btn").click()
    frame.wait_for_selector("#results-body .results-more")
    rows = frame.locator("#results-body tbody tr")
    assert rows.count() == 250
    more = frame.locator("#results-body .results-more")
    assert more.locator("span").inner_text() == "Showing 250 of 1,200 rows"
    assert frame.locator("#qb-preview tbody tr").count() <= 250
    more.locator("button").click()
    assert rows.count() == 500
    assert rows.nth(499).locator("td.row-number").inner_text() == "500"
    label = frame.locator("#results-body thead .workspace-column-label").first
    label.click()
    frame.locator("#results-body thead .workspace-column-label").first.click()
    assert rows.count() == 500
    assert rows.first.locator("td").nth(1).inner_text() == "1,199"
    for _ in range(3):
        more.locator("button").click()
    assert rows.count() == 1200
    assert more.is_hidden()

    page.goto(f"{served}/d/demo/query", wait_until="load")
    page.wait_for_selector("#qb-preview", timeout=30_000)
    page.evaluate("sql => ace.edit('sql-editor').setValue(sql, 1)", sql)
    page.locator("#run-btn").click()
    page.wait_for_selector("#results-body .results-more")
    assert page.locator("#results-body tbody tr").count() == 250
    page.locator("#results-body thead th").first.click()
    page.locator("#results-body thead th").first.click()
    assert page.locator("#results-body tbody tr").first.locator("td").first.inner_text() == "1,199"


def test_home_explore_opens_workspace(page, served):
    page.goto(served, wait_until="networkidle")
    assert page.locator("header.topbar #home-explore").is_visible()
    page.locator("#home-explore").click()
    if page.locator("#explore-dialog[open]").count():
        page.locator("#explore-open").click()
    page.wait_for_url("**/workspace")
    assert page.locator("#workspace").is_visible()


def test_home_explore_multiple_dashboards_asks_for_context(page, tmp_path):
    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "second.yaml").write_text(
        "title: Second\nsource: {type: duckdb, database: ':memory:'}\ntiles: []\n"
    )
    server, thread, port = _start_server(create_app(tmp_path))
    try:
        page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
        assert not page.locator("#explore-dialog").is_visible()
        page.locator("#home-explore").click()
        assert page.locator("#explore-dialog").is_visible()
        page.locator("#explore-dashboard").select_option("/d/second/workspace")
        page.locator("#explore-open").click()
        page.wait_for_url("**/d/second/workspace")
    finally:
        _stop_server(server, thread, page)


def test_schema_browser_loading_and_disclosure_state(page, served):
    page.goto(f"{served}/d/demo/workspace", wait_until="networkidle")
    page.evaluate("""async () => {
      const {SchemaBrowser} = await import('/static/js/workspace-schema.js');
      const root=document.createElement('section');root.id='schema-fixture';
      root.innerHTML='<input><p id="schema-message"></p><div id="schema-tree"></div>'+
        '<button id="schema-more">Show more</button>';
      document.body.replaceChildren(root);
      window.schemaFixture=new SchemaBrowser(root,()=>{});
      window.schemaTables=Array.from({length:201},(_,i)=>({schema:i<150?'FIRST':'SECOND',name:`table_${i}`,sql:`table_${i}`,columns:[]}));
      schemaFixture.update({status:'loading',source:'one'});
    }""")
    root = page.locator("#schema-fixture")
    assert root.get_attribute("aria-busy") == "true"
    assert page.locator("#schema-message").is_visible()
    assert (
        page.locator("#schema-message").evaluate(
            "el=>getComputedStyle(el,'::before').animationName"
        )
        == "schema-spin"
    )
    page.evaluate("schemaFixture.update({status:'ready',source:'one',tables:schemaTables})")
    assert root.get_attribute("aria-busy") == "false"
    assert not page.locator(".schema-group").first.evaluate("el=>el.open")
    page.locator(".schema-group > summary").click()
    page.locator("#schema-more").click()
    assert page.locator(".schema-group").first.evaluate("el=>el.open")
    assert not page.locator(".schema-group").nth(1).evaluate("el=>el.open")
    assert page.locator(".schema-table").count() == 200
    page.locator(".schema-group > summary").first.click()
    page.locator("#schema-fixture input").fill("table_1")
    assert page.locator(".schema-group").first.evaluate("el=>el.open")
    page.locator("#schema-fixture input").fill("")
    assert not page.locator(".schema-group").first.evaluate("el=>el.open")
    page.evaluate("schemaFixture.update({status:'loading',source:'one'})")
    page.evaluate("schemaFixture.update({status:'error',message:'Connection unavailable'})")
    assert root.get_attribute("aria-busy") == "false"
    assert "Refresh to retry" in page.locator("#schema-message").inner_text()
    page.evaluate("schemaFixture.update({status:'ready',source:'one',tables:schemaTables})")
    assert not page.locator(".schema-group").first.evaluate("el=>el.open")
    page.evaluate("schemaFixture.update({status:'ready',source:'two',tables:schemaTables})")
    assert not page.locator(".schema-group").first.evaluate("el=>el.open")


def test_workspace_database_browse_inserts_qualified_name(page, served):
    page.goto(served)
    page.evaluate("localStorage.clear()")

    def schema(route):
        database = "OTHER" if "database=OTHER" in route.request.url else "SALES"
        route.fulfill(
            json={
                "tables": [
                    {
                        "schema": "PUBLIC",
                        "name": "orders",
                        "sql": f'"{database}"."PUBLIC"."orders"',
                        "columns": [],
                    }
                ]
            }
        )

    context = '@context:{"source":"demo.source","database":"OTHER"}'
    posted = []
    discoveries = []

    def databases(route):
        if route.request.method == "POST":
            posted.append(route.request.post_data_json)
            route.fulfill(json={"source": context, "database": "OTHER"})
            return
        discoveries.append(route.request.url)
        current = "OTHER" if "OTHER" in route.request.url else "SALES"
        route.fulfill(json={"current": current, "databases": ["OTHER", "SALES"]})

    def roles(route):
        source = route.request.url.split("source=", 1)[1] if "source=" in route.request.url else ""
        route.fulfill(
            json={
                "switchable": False,
                "current": [],
                "roles": [],
                "note": "",
                "label": "Role",
                "source": unquote(source),
                "base_source": "",
                "canonical_source": "demo.source",
                "selected": None,
                "database": "OTHER" if "OTHER" in source else None,
                "source_label": "Demo",
            }
        )

    page.route("**/api/dashboards/*/databases*", databases)
    page.route("**/api/dashboards/*/roles*", roles)
    page.route("**/api/dashboards/*/schema?*", schema)
    try:
        page.goto(f"{served}/d/demo/workspace", wait_until="networkidle")
        assert page.locator(".topbar .page-title").inner_text() == "exploration"
        assert page.locator(".workspace-connection-context").count() == 0
        assert len(discoveries) == 1, discoveries
        page.locator("#workspace-database-controls .dd-btn").click()
        search = page.get_by_role("searchbox", name="Switch database", exact=True)
        menu = page.locator(".dd-menu.dd-open")
        assert menu.locator(".dd-group").all_inner_texts() == ["All databases\n2"]
        assert "DATABASE.SCHEMA.TABLE" in menu.locator(".dd-foot").inner_text()
        selected = menu.locator(".dd-item.selected")
        name_box = selected.locator(".dd-name").bounding_box()
        check_box = selected.locator(".dd-check").bounding_box()
        assert check_box["x"] > name_box["x"] + name_box["width"]
        assert selected.locator(".dd-tag").inner_text() == "default"
        assert (
            page.locator("#workspace-database-controls .dd-row-meta").inner_text() == "default db"
        )
        search.fill("no-such-database")
        assert page.get_by_text("No matching databases", exact=True).is_visible()
        search.fill("oth")
        assert page.locator(".dd-menu.dd-open .dd-item:visible").count() == 1
        with page.expect_response(lambda r: "database=OTHER" in r.url):
            search.press("ArrowDown")
            page.keyboard.press("Enter")
        page.wait_for_function(
            "document.getElementById('schema-browser').getAttribute('aria-busy')==='false'"
        )
        assert posted == [{"source": "", "database": "OTHER"}]
        editor_frame = page.locator("#workspace-frames iframe:not([hidden])").element_handle()
        assert (
            editor_frame.content_frame().evaluate("document.getElementById('source-picker').value")
            == context
        )
        assert page.locator("#workspace-database").input_value() == "OTHER"
        assert page.locator("#workspace-database-controls .dd-row-meta").inner_text() == "database"
        assert page.locator("#schema-tree .schema-db > summary").inner_text().startswith("OTHER")
        assert (
            "database OTHER"
            in editor_frame.content_frame().locator(".workspace-query-context").inner_text()
        )
        page.locator("#workspace-database-controls .dd-btn").click()
        search = page.get_by_role("searchbox", name="Switch database", exact=True)
        assert search.input_value() == ""
        groups = page.locator(".dd-menu.dd-open .dd-group").all_inner_texts()
        assert groups == ["Recent", "All databases\n2"]
        assert page.locator(".dd-menu.dd-open .dd-item:visible").count() == 3
        page.keyboard.press("Escape")
        trigger = page.locator("#workspace-database-controls .dd-btn")
        assert trigger.evaluate("el=>el===document.activeElement")
        trigger.evaluate("el=>{el.click();el.click();el.click();}")
        page.wait_for_timeout(200)
        assert trigger.get_attribute("aria-expanded") == "true"
        assert page.get_by_role("searchbox", name="Switch database", exact=True).is_visible()
        page.emulate_media(reduced_motion="reduce")
        page.keyboard.press("Escape")
        assert page.locator(".dd-search-menu:visible").count() == 0
        trigger.click()
        assert (
            page.locator(".dd-search-menu.dd-open").evaluate("el=>el.getAnimations().length") == 0
        )
        page.keyboard.press("Escape")
        page.emulate_media(reduced_motion="no-preference")
        page.locator(".schema-group > summary").click()
        page.get_by_role("button", name="Insert orders into SQL", exact=True).click()
        frame_element = page.locator("#workspace-frames iframe:not([hidden])").element_handle()
        frame = frame_element.content_frame()
        frame.wait_for_function("ace.edit('sql-editor').getValue().includes('OTHER')")
        assert '"OTHER"."PUBLIC"."orders"' in frame.evaluate("ace.edit('sql-editor').getValue()")
    finally:
        page.unroute("**/api/dashboards/*/databases*")
        page.unroute("**/api/dashboards/*/roles*")
        page.unroute("**/api/dashboards/*/schema?*")


def test_workspace_inline_tab_rename_preserves_sql_and_recovers(page, served):
    page.goto(f"{served}/d/demo/workspace", wait_until="networkidle")
    page.evaluate("localStorage.clear()")
    page.reload(wait_until="networkidle")
    frame = page.locator("#workspace-frames iframe").element_handle().content_frame()
    frame.locator(".workspace-editor[data-ready=true]").wait_for()
    frame.evaluate("ace.edit('sql-editor').setValue('SELECT 42 AS answer', -1)")
    tab = page.get_by_role("tab", name="Untitled query", exact=True)
    tab.click()
    field = page.get_by_role("textbox", name="Query name", exact=True)
    assert field.is_visible()
    assert not page.locator("#query-dialog").is_visible()
    field.fill("Revenue worksheet")
    field.press("Enter")
    renamed = page.get_by_role("tab", name="Revenue worksheet", exact=True)
    assert renamed.evaluate("el=>el===document.activeElement")
    renamed.press("F2")
    field.fill("Discard this name")
    field.press("Escape")
    assert renamed.is_visible()
    renamed.click()
    field.fill("   ")
    field.press("Enter")
    assert renamed.is_visible()
    renamed.press("F2")
    field.fill("Monthly revenue")
    page.locator("#new-query").click()
    assert page.get_by_role("tab", name="Monthly revenue", exact=True).is_visible()
    assert (
        page.get_by_role("tab", name="Untitled query", exact=True).get_attribute("aria-selected")
        == "true"
    )
    page.reload(wait_until="networkidle")
    page.get_by_role("tab", name="Monthly revenue", exact=True).click()
    assert page.locator(".query-tab-name").count() == 0
    frame = page.locator("#workspace-frames iframe").first.element_handle().content_frame()
    frame.locator(".workspace-editor[data-ready=true]").wait_for()
    assert frame.evaluate("ace.edit('sql-editor').getValue()") == "SELECT 42 AS answer"
    assert page.get_by_role("button", name="Close Monthly revenue", exact=True).is_visible()
    page.get_by_role("button", name="Close Monthly revenue", exact=True).click()
    assert page.locator("#query-dialog").is_visible()
    page.locator("#query-dialog").get_by_role("button", name="Cancel", exact=True).click()
    page.evaluate("localStorage.clear()")


def test_workspace_roles_preserve_tab_context_and_failed_switch(page, served):
    page.goto(served)
    page.evaluate("localStorage.clear()")
    requests = []

    def roles(route):
        body = route.request.post_data_json if route.request.method == "POST" else None
        source = (
            body["source"]
            if body
            else parse_qs(urlparse(route.request.url).query).get("source", [""])[0]
        )
        selected = json.loads(source[6:])[1] if source.startswith("@role:") else None
        if body:
            selected = body["role"]
            requests.append(selected)
            if selected == "DENIED":
                route.fulfill(status=422, json={"detail": "Role is no longer granted"})
                return
        key = (
            "@role:" + json.dumps(["demo.source", selected], separators=(",", ":"))
            if selected
            else ""
        )
        route.fulfill(
            json={
                "source": key,
                "base_source": "",
                "canonical_source": "demo.source",
                "source_label": "default · snowflake",
                "selected": selected,
                "current": [selected or "BASE"],
                "switchable": True,
                "label": "Primary role",
                "note": "Secondary roles: ALL",
                "roles": [{"value": r, "label": r} for r in ["BASE", "ANALYST", "DENIED"]],
            }
        )

    page.route("**/api/dashboards/*/roles*", roles)
    try:
        page.goto(f"{served}/d/demo/workspace", wait_until="networkidle")
        frame = page.locator("#workspace-frames iframe").first.element_handle().content_frame()
        frame.locator(".workspace-editor[data-ready=true]").wait_for()
        frame.evaluate("ace.edit('sql-editor').setValue('SELECT 42 AS answer', -1)")
        page.locator("#workspace-role-controls .dd-btn").click()
        assert page.locator("#workspace-role-controls .dd-row-meta").inner_text() == (
            "default role"
        )
        search = page.get_by_role("searchbox", name="Switch role", exact=True)
        assert page.locator(".dd-menu.dd-open .dd-group").all_inner_texts() == [
            "Connection default",
            "Roles\n3",
        ]
        assert "Secondary roles: ALL" in page.locator(".dd-menu.dd-open .dd-foot").inner_text()
        search.fill("anal")
        search.press("ArrowDown")
        search.press("Enter")
        page.wait_for_function(
            "() => { const p=document.querySelector('#workspace-role');"
            "return p.value==='ANALYST' && !p.disabled; }"
        )
        frame.wait_for_function(
            "document.querySelector('#source-picker').value.startsWith('@role:')"
        )
        assert frame.locator("#source-picker").input_value() == '@role:["demo.source","ANALYST"]'
        assert page.locator("#workspace-source").input_value() == ""
        assert frame.evaluate("ace.edit('sql-editor').getValue()") == "SELECT 42 AS answer"
        page.locator("#workspace-role").select_option("DENIED", force=True)
        page.get_by_text("Role is no longer granted", exact=True).wait_for()
        assert page.locator("#workspace-role").input_value() == "ANALYST"
        assert frame.locator("#source-picker").input_value() == '@role:["demo.source","ANALYST"]'
        page.locator("#new-query").click()
        page.wait_for_function(
            "() => { const p=document.querySelector('#workspace-role');"
            "return p.value==='ANALYST' && !p.disabled; }"
        )
        added = page.locator("#workspace-frames iframe").nth(1).element_handle().content_frame()
        added.locator(".workspace-editor[data-ready=true]").wait_for()
        added.wait_for_function(
            "document.querySelector('#source-picker').value.startsWith('@role:')"
        )
        assert added.locator("#source-picker").input_value() == '@role:["demo.source","ANALYST"]'
        assert added.evaluate("ace.edit('sql-editor').getValue()") == ""
        page.get_by_role("tab", name="Untitled query", exact=True).first.click()
        assert page.locator("#workspace-role").input_value() == "ANALYST"
        page.reload(wait_until="networkidle")
        page.wait_for_function(
            "() => { const p=document.querySelector('#workspace-role');"
            "return p.value==='ANALYST' && !p.disabled; }"
        )
        frame = page.locator("#workspace-frames iframe").first.element_handle().content_frame()
        assert (
            frame.locator("#source-picker option:checked").get_attribute("data-unavailable") is None
        )
        assert frame.evaluate("ace.edit('sql-editor').getValue()") == "SELECT 42 AS answer"
        page.locator("#workspace-role").select_option("", force=True)
        page.wait_for_function(
            "() => { const p=document.querySelector('#workspace-role');"
            "return p.value==='' && !p.disabled; }"
        )
        assert requests == ["ANALYST", "DENIED", None]
    finally:
        page.unroute("**/api/dashboards/*/roles*", roles)
        page.evaluate("localStorage.clear()")


def test_workspace_warehouse_picker_persists_and_follows_role_switches(page, served):
    """Snowflake had no way to pick a warehouse, and a primary role without USAGE on
    the source's one left the session with none: the schema panel showed a raw 000606
    with a doubled period. The Warehouse row picks one into the source key, survives a
    reload, notes a fallback after a role switch and says plainly when none is usable."""
    page.goto(served)
    page.evaluate("localStorage.clear()")
    base = "demo.source"
    picked = '@context:{"source":"demo.source","warehouse":"LEARNING_WH"}'
    fallback = '@context:{"source":"demo.source","role":"LEARNER","warehouse":"LEARNER_WH"}'
    stranded = '@role:["demo.source","NOTHING"]'
    posts = []

    def context(source, switched=False):
        role, warehouse, choices, extra = "BASE", "BASE_WH", ["BASE_WH", "LEARNING_WH"], {}
        if source == picked:
            warehouse = "LEARNING_WH"
        elif source == fallback:
            role, warehouse, choices = "LEARNER", "LEARNER_WH", ["LEARNER_WH"]
            if switched:
                extra = {
                    "warehouse_note": "LEARNER cannot use warehouse LEARNING_WH, "
                    "so queries run on LEARNER_WH."
                }
        elif source == stranded:
            role, warehouse, choices = "NOTHING", None, ["WATCHED_WH"]
            extra = {
                "warning": "NOTHING cannot use warehouse BASE_WH or any warehouse it can "
                "see. Pick another role."
            }
        return {
            "source": source,
            "base_source": "",
            "canonical_source": base,
            "source_label": "default · snowflake",
            "selected": None if role == "BASE" else role,
            "current": [role],
            "switchable": True,
            "label": "Primary role",
            "note": "",
            "roles": [{"value": r, "label": r} for r in ["BASE", "LEARNER", "NOTHING"]],
            "warehouse": warehouse,
            "configured_warehouse": "BASE_WH",
            "selected_warehouse": None if warehouse in {"BASE_WH", None} else warehouse,
            "warehouses": [{"value": w, "label": w, "detail": "X-Small"} for w in choices],
            "warning": "",
            **extra,
        }

    def roles(route):
        if route.request.method == "POST":
            body = route.request.post_data_json
            posts.append(("role", body))
            key = fallback if body["role"] == "LEARNER" else stranded
            route.fulfill(json=context(key, switched=True))
            return
        source = parse_qs(urlparse(route.request.url).query).get("source", [""])[0]
        route.fulfill(json=context(source))

    def warehouses(route):
        posts.append(("warehouse", route.request.post_data_json))
        route.fulfill(json=context(picked))

    def schema(route):
        source = parse_qs(urlparse(route.request.url).query).get("source", [""])[0]
        if source == stranded:
            route.fulfill(
                status=422,
                json={
                    "detail": "000606 (57P03): No active warehouse selected in the current "
                    "session.  Select an active warehouse with the 'use warehouse' command."
                },
            )
            return
        route.fulfill(json={"tables": []})

    def databases(route):
        route.fulfill(json={"current": None, "databases": []})

    def settled(value):
        page.wait_for_function(
            "value => { const p=document.querySelector('#workspace-warehouse');"
            "return p.value===value && !p.disabled; }",
            arg=value,
        )

    routes = [
        ("**/api/dashboards/*/roles*", roles),
        ("**/api/dashboards/*/warehouses", warehouses),
        ("**/api/dashboards/*/schema?*", schema),
        ("**/api/dashboards/*/databases*", databases),
    ]
    for pattern, handler in routes:
        page.route(pattern, handler)
    row = page.locator("#workspace-warehouse-controls")
    status = row.locator(":scope > p[role=status]")
    meta = row.locator(".dd-row-meta")
    try:
        page.goto(f"{served}/d/demo/workspace", wait_until="networkidle")
        frame = page.locator("#workspace-frames iframe").first.element_handle().content_frame()
        frame.locator(".workspace-editor[data-ready=true]").wait_for()
        settled("BASE_WH")
        assert meta.inner_text() == "default wh"
        assert status.is_hidden()
        row.locator(".dd-btn").click()
        menu = page.locator(".dd-menu.dd-open")
        assert menu.locator(".dd-group").all_inner_texts() == ["All warehouses\n2"]
        page.get_by_role("searchbox", name="Switch warehouse", exact=True).fill("learning")
        page.keyboard.press("ArrowDown")
        page.keyboard.press("Enter")
        frame.wait_for_function(
            f"document.querySelector('#source-picker').value === {json.dumps(picked)}"
        )
        settled("LEARNING_WH")
        assert meta.inner_text() == "warehouse"

        page.reload(wait_until="networkidle")
        frame = page.locator("#workspace-frames iframe").first.element_handle().content_frame()
        frame.locator(".workspace-editor[data-ready=true]").wait_for()
        settled("LEARNING_WH")
        assert frame.locator("#source-picker").input_value() == picked
        assert meta.inner_text() == "warehouse"
        row.locator(".dd-btn").click()
        assert page.locator(".dd-menu.dd-open .dd-group").all_inner_texts() == [
            "Recent",
            "All warehouses\n2",
        ]
        page.keyboard.press("Escape")

        page.locator("#workspace-role").select_option("LEARNER", force=True)
        settled("LEARNER_WH")
        assert status.inner_text() == (
            "LEARNER cannot use warehouse LEARNING_WH, so queries run on LEARNER_WH."
        )
        assert status.get_attribute("data-tone") == ""
        assert frame.locator("#source-picker").input_value() == fallback

        page.locator("#workspace-role").select_option("NOTHING", force=True)
        settled("")
        warning = (
            "NOTHING cannot use warehouse BASE_WH or any warehouse it can see. Pick another role."
        )
        assert status.inner_text() == warning
        assert status.get_attribute("data-tone") == "warning"
        message = page.locator("#schema-message")
        page.wait_for_function(
            "document.querySelector('#schema-message').dataset.loading === 'false'"
        )
        assert message.inner_text() == (
            warning.removesuffix(".") + ". Use Refresh to retry; you can still write SQL."
        )
        assert "000606" not in message.inner_text()
        assert [kind for kind, _ in posts] == ["warehouse", "role", "role"]
        assert posts[0][1] == {"source": "", "warehouse": "LEARNING_WH"}
        assert posts[1][1] == {"source": picked, "role": "LEARNER"}
    finally:
        for pattern, handler in routes:
            page.unroute(pattern, handler)
        page.evaluate("localStorage.clear()")


def test_workspace_database_row_follows_role_switches(page, served):
    """A role that cannot see the source's database left the Database row with an empty
    label and the schema panel showing a raw 002043. The row now names the database the
    switch landed on with a note, and says plainly when the role can use none."""
    page.goto(served)
    page.evaluate("localStorage.clear()")
    fallback = '@context:{"source":"demo.source","role":"LEARNER","database":"LRN"}'
    stranded = '@role:["demo.source","HIDDEN"]'
    note = "LEARNER cannot use database DEV, so queries run in LRN."
    warning = "HIDDEN cannot use database DEV, and it cannot see any database. Pick another role."

    def context(source, switched=False):
        role = {fallback: "LEARNER", stranded: "HIDDEN"}.get(source, "BASE")
        extra = {}
        if switched and source == fallback:
            extra = {"database": "LRN", "database_note": note}
        if switched and source == stranded:
            extra = {"database_warning": warning}
        return {
            "source": source,
            "base_source": "",
            "canonical_source": "demo.source",
            "source_label": "default · snowflake",
            "selected": None if role == "BASE" else role,
            "current": [role],
            "switchable": True,
            "label": "Primary role",
            "note": "",
            "roles": [{"value": r, "label": r} for r in ["BASE", "LEARNER", "HIDDEN"]],
            "warehouse": "WH",
            "configured_warehouse": "WH",
            "selected_warehouse": None,
            "warehouses": [{"value": "WH", "label": "WH", "detail": "X-Small"}],
            "warning": "",
            **extra,
        }

    def roles(route):
        if route.request.method == "POST":
            role = route.request.post_data_json["role"]
            route.fulfill(json=context(fallback if role == "LEARNER" else stranded, True))
            return
        source = parse_qs(urlparse(route.request.url).query).get("source", [""])[0]
        route.fulfill(json=context(source))

    def databases(route):
        source = parse_qs(urlparse(route.request.url).query).get("source", [""])[0]
        if source == stranded:
            route.fulfill(
                json={
                    "current": None,
                    "databases": [],
                    "comments": {},
                    "warning": "HIDDEN cannot use database DEV, and it cannot see any "
                    "database. Pick another role.",
                }
            )
            return
        current = "LRN" if source == fallback else "DEV"
        route.fulfill(json={"current": current, "databases": ["DEV", "LRN"], "comments": {}})

    def schema(route):
        source = parse_qs(urlparse(route.request.url).query).get("source", [""])[0]
        if source == stranded:
            route.fulfill(
                status=422,
                json={
                    "detail": "002043 (02000): SQL compilation error: Object does not exist, "
                    "or operation cannot be performed."
                },
            )
            return
        route.fulfill(json={"tables": []})

    routes = [
        ("**/api/dashboards/*/roles*", roles),
        ("**/api/dashboards/*/databases*", databases),
        ("**/api/dashboards/*/schema?*", schema),
    ]
    for pattern, handler in routes:
        page.route(pattern, handler)
    row = page.locator("#workspace-database-controls")
    label = row.locator(".dd-label")
    status = row.locator(":scope > p[role=status]")

    def settled(role):
        page.wait_for_function(
            "role => { const r=document.querySelector('#workspace-role');"
            "return r.value===role && !r.disabled"
            " && document.querySelector('#schema-message').dataset.loading==='false'; }",
            arg=role,
        )

    try:
        page.goto(f"{served}/d/demo/workspace", wait_until="networkidle")
        frame = page.locator("#workspace-frames iframe").first.element_handle().content_frame()
        frame.locator(".workspace-editor[data-ready=true]").wait_for()
        settled("")
        assert label.inner_text() == "DEV"
        assert row.locator(".dd-row-meta").inner_text() == "default db"
        assert status.is_hidden()

        page.locator("#workspace-role").select_option("LEARNER", force=True)
        settled("LEARNER")
        page.wait_for_function(
            "document.querySelector('#workspace-database-controls .dd-label').textContent==='LRN'"
        )
        assert row.locator(".dd-row-meta").inner_text() == "database"
        assert status.inner_text() == note
        assert status.get_attribute("data-tone") == ""
        assert frame.locator("#source-picker").input_value() == fallback

        page.locator("#workspace-role").select_option("HIDDEN", force=True)
        settled("HIDDEN")
        page.wait_for_function(
            "document.querySelector('#workspace-database-controls .dd-label').textContent"
            "==='No database'"
        )
        assert row.is_visible()
        assert row.locator(".dd-row-meta").count() == 0
        assert status.inner_text() == warning
        assert status.get_attribute("data-tone") == "warning"
        message = page.locator("#schema-message").inner_text()
        assert (
            message
            == warning.removesuffix(".") + ". Use Refresh to retry; you can still write SQL."
        )
        assert "002043" not in message
    finally:
        for pattern, handler in routes:
            page.unroute(pattern, handler)
        page.evaluate("localStorage.clear()")


def test_workspace_sources_rows_ellipsize_long_names(page, served):
    """At the default sidebar width SNOWFLAKE_LEARNING_ROLE and friends were cut off
    mid-letter: the label was a flex box, where text-overflow never paints an ellipsis.
    Every Sources row now ellipsizes with the full name in a tooltip, at the default and
    minimum widths and after dragging wider, and its meta and chevron stay in the row."""
    page.goto(served)
    page.evaluate("localStorage.clear()")
    role, database, warehouse = (
        "SNOWFLAKE_LEARNING_ROLE",
        "SNOWFLAKE_LEARNING_DB_WITH_A_LONG_NAME",
        "SNOWFLAKE_LEARNING_WH",
    )

    def roles(route):
        route.fulfill(
            json={
                "source": "",
                "base_source": "",
                "canonical_source": "demo.source",
                "source_label": "default · snowflake",
                "selected": None,
                "current": [role],
                "switchable": True,
                "label": "Primary role",
                "note": "",
                "roles": [{"value": role, "label": role}],
                "warehouse": warehouse,
                "configured_warehouse": warehouse,
                "selected_warehouse": None,
                "warehouses": [{"value": warehouse, "label": warehouse, "detail": "X-Small"}],
                "warning": "",
            }
        )

    def databases(route):
        route.fulfill(json={"current": database, "databases": [database], "comments": {}})

    routes = [
        ("**/api/dashboards/*/roles*", roles),
        ("**/api/dashboards/*/databases*", databases),
        ("**/api/dashboards/*/schema?*", lambda route: route.fulfill(json={"tables": []})),
    ]
    for pattern, handler in routes:
        page.route(pattern, handler)
    measure = """() => [...document.querySelectorAll(
      '.workspace-context-rows > div:not([hidden]) .dd-btn')].map(btn => {
        const label = btn.querySelector('.dd-label'), style = getComputedStyle(label);
        const meta = btn.querySelector('.dd-row-meta'), chev = btn.querySelector('.dd-chev');
        const b = btn.getBoundingClientRect(), l = label.getBoundingClientRect();
        const m = meta.getBoundingClientRect(), c = chev.getBoundingClientRect();
        return {text: label.textContent, title: label.title,
          clipped: label.scrollWidth > label.clientWidth,
          ellipsis: style.textOverflow === 'ellipsis' && style.whiteSpace === 'nowrap'
            && !style.display.includes('flex'),
          meta: meta.textContent, metaShown: m.width > 0 && m.left >= l.right && m.right <= c.left,
          chevShown: c.width > 0 && c.right <= b.right, metaGap: Math.round(b.right - m.right)};
      })"""

    def drag(x):
        box = page.locator("#sidebar-resize").bounding_box()
        page.mouse.move(box["x"] + box["width"] / 2, box["y"] + 200)
        page.mouse.down()
        page.mouse.move(x, box["y"] + 200, steps=6)
        page.mouse.up()

    try:
        page.goto(f"{served}/d/demo/workspace", wait_until="networkidle")
        page.wait_for_function(
            "document.querySelector('#workspace-warehouse-controls .dd-label')?.textContent"
            f" === {json.dumps(warehouse)}"
        )
        seen = {}
        for name, x in [("default", None), ("minimum", 60), ("wide", 470)]:
            if x is not None:
                drag(x)
            width = round(page.locator("aside").bounding_box()["width"])
            rows = page.evaluate(measure)
            seen[name] = (width, [r["clipped"] for r in rows])
            assert [r["text"] for r in rows][1:] == [role, database, warehouse]
            assert [r["meta"] for r in rows][1:] == ["default role", "default db", "default wh"]
            for row in rows:
                assert row["title"] == row["text"], (name, row)
                assert not row["clipped"] or row["ellipsis"], (name, row)
                assert row["metaShown"], (name, row)
                assert row["chevShown"], (name, row)
            assert len({row["metaGap"] for row in rows}) == 1, (name, rows)
        assert seen["default"][0] == 258
        assert seen["minimum"][0] == 180
        assert seen["wide"][0] > 400
        assert seen["default"][1] == [False, True, True, True]
        assert seen["minimum"][1] == [False, True, True, True]
        assert seen["wide"][1] == [False, False, False, False]
    finally:
        for pattern, handler in routes:
            page.unroute(pattern, handler)
        page.evaluate("localStorage.clear()")


def test_workspace_schema_error_has_one_period(page, served):
    page.goto(served)
    page.evaluate("localStorage.clear()")

    def schema(route):
        route.fulfill(status=422, json={"detail": "Object does not exist."})

    page.route("**/api/dashboards/*/schema*", schema)
    try:
        page.goto(f"{served}/d/demo/workspace", wait_until="networkidle")
        message = page.locator("#schema-message")
        page.wait_for_function(
            "document.querySelector('#schema-message').textContent.includes('Object')"
        )
        assert message.inner_text() == (
            "Object does not exist. Use Refresh to retry; you can still write SQL."
        )
    finally:
        page.unroute("**/api/dashboards/*/schema*", schema)


def test_workspace_new_tab_runs_under_the_picked_context(page, served):
    """Adding a query tab after picking a role started it on the connection default,
    so the left picker said one role while the new tab ran under another. A new tab
    now inherits the whole picked context (role, database and warehouse) and is still
    replaced when a saved query opens over it."""
    page.goto(f"{served}/d/demo/workspace", wait_until="networkidle")
    page.evaluate("localStorage.clear()")
    picked = (
        '@context:{"source":"demo.source","role":"ANALYST","database":"SALES",'
        '"warehouse":"LEARNING_WH"}'
    )
    posted = []

    def roles(route):
        if route.request.method == "POST":
            posted.append(route.request.post_data_json)
            source = picked
        else:
            source = parse_qs(urlparse(route.request.url).query).get("source", [""])[0]
        role = "ANALYST" if source == picked else None
        route.fulfill(
            json={
                "source": source,
                "base_source": "",
                "canonical_source": "demo.source",
                "source_label": "default · snowflake",
                "selected": role,
                "database": "SALES" if role else None,
                "current": [role or "BASE"],
                "switchable": True,
                "label": "Primary role",
                "note": "",
                "roles": [{"value": r, "label": r} for r in ["BASE", "ANALYST"]],
            }
        )

    page.route("**/api/dashboards/*/roles*", roles)
    try:
        page.reload(wait_until="networkidle")
        frame = page.locator("#workspace-frames iframe").first.element_handle().content_frame()
        frame.locator(".workspace-editor[data-ready=true]").wait_for()
        frame.evaluate("ace.edit('sql-editor').setValue('SELECT 7 AS saved', -1)")
        frame.locator("#workspace-save").click()
        page.locator("#library-name").fill("Inherited context query")
        page.locator("#library-confirm").click()
        page.wait_for_function("!document.getElementById('library-dialog').open")
        page.locator("#workspace-role").select_option("ANALYST", force=True)
        frame.wait_for_function(
            f"document.querySelector('#source-picker').value === {json.dumps(picked)}"
        )
        page.locator("#new-query").click()
        page.wait_for_function(
            "() => { const p=document.querySelector('#workspace-role');"
            "return p.value==='ANALYST' && !p.disabled; }"
        )
        added = page.locator("#workspace-frames iframe").nth(1).element_handle().content_frame()
        added.locator(".workspace-editor[data-ready=true]").wait_for()
        added.wait_for_function(
            f"document.querySelector('#source-picker').value === {json.dumps(picked)}"
        )
        assert added.evaluate("ace.edit('sql-editor').getValue()") == ""
        assert posted == [{"source": "", "role": "ANALYST"}]
        page.evaluate(
            "document.querySelectorAll('#library-browser details').forEach(d => { d.open = true; })"
        )
        page.locator(".library-row .workspace-query", has_text="Inherited context query").click()
        page.wait_for_function(
            "document.querySelector('#query-tabs [aria-selected=true]')?.textContent"
            " === 'Inherited context query'"
        )
        assert page.locator("#workspace-frames iframe").count() == 2
    finally:
        page.unroute("**/api/dashboards/*/roles*", roles)
        page.evaluate("localStorage.clear()")


def test_query_editor_refresh_error_has_one_period(page, served):
    page.goto(f"{served}/d/demo/query", wait_until="load")
    page.wait_for_selector("#qb-preview", timeout=30_000)

    def dashboard(route):
        route.fulfill(status=503, json={"detail": "Dashboard file changed on disk."})

    page.route("**/api/dashboards/demo", dashboard)
    try:
        page.evaluate("document.getElementById('qb-refresh').click()")
        page.wait_for_function(
            "document.querySelector('#qb-save-status').textContent.includes('Refresh failed')"
        )
        assert page.locator("#qb-save-status").inner_text() == (
            "Refresh failed: Dashboard file changed on disk. Your work is still here."
        )
    finally:
        page.unroute("**/api/dashboards/demo", dashboard)


def test_workspace_library_save_error_has_one_period(page, served):
    page.goto(served)
    page.evaluate("localStorage.clear()")

    def library(route):
        if route.request.method != "POST":
            route.continue_()
            return
        route.fulfill(status=409, json={"detail": "A saved query already has that name."})

    page.route("**/api/dashboards/*/library", library)
    try:
        page.goto(f"{served}/d/demo/workspace", wait_until="networkidle")
        frame = page.locator("#workspace-frames iframe").first.element_handle().content_frame()
        frame.locator(".workspace-editor[data-ready=true]").wait_for()
        frame.evaluate('ace.edit("sql-editor").setValue("SELECT 1 AS n", -1)')
        frame.locator("#workspace-save").click()
        page.locator("#library-name").fill("Revenue")
        page.locator("#library-confirm").click()
        error = page.locator("#library-error")
        error.wait_for(state="visible")
        assert error.inner_text() == (
            "A saved query already has that name. Your draft is unchanged. "
            "Save a copy to keep both versions."
        )
    finally:
        page.unroute("**/api/dashboards/*/library", library)
        page.evaluate("localStorage.clear()")


def test_studio_connection_error_has_one_period(page, tmp_path, monkeypatch):
    create_demo(tmp_path)
    agent = tmp_path / "idle_agent.py"
    agent.write_text("""
import json,sys
init=json.loads(sys.stdin.readline())
print(json.dumps({'type':'control_response','response':{
 'subtype':'success','request_id':init['request_id'],'response':{}}}),flush=True)
sys.stdin.read()
""")
    monkeypatch.setattr(
        "sqldash.studio.entrypoints.entrypoints_path", lambda: tmp_path / "studio.json"
    )
    save_entrypoint(
        AgentEntrypoint(
            name="Idle test",
            protocol="claude",
            command=[sys.executable, str(agent), "{prompt}"],
        )
    )
    app = create_app(tmp_path, studio=True, allowed_hosts=["127.0.0.1"])
    server, thread, port = _start_server(app)

    def output(route):
        route.fulfill(status=502, json={"detail": "The agent output stream closed."})

    page.route("**/api/studio/sessions/*/output*", output)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/demo")
        page.evaluate("localStorage.setItem('sqldash-ai-studio-tour-v1','seen')")
        page.locator("#studio-open").click()
        page.locator("#studio-entrypoint").select_option("Idle test")
        page.locator("#studio-message").fill("Say hello")
        page.locator("#studio-send").click()
        page.wait_for_function(
            "document.querySelector('#studio-status').textContent.includes('Reopen Studio')"
        )
        assert page.locator("#studio-status").inner_text() == (
            "The agent output stream closed. Reopen Studio to retry the connection."
        )
    finally:
        page.unroute("**/api/studio/sessions/*/output*", output)
        _stop_server(server, thread, page)


def _run_windows(posted):
    """The daterange each /api/run body carried, deduped. A tile with `compare` also
    posts the previous-period window, which is the current one shifted back."""
    sent = [json.loads(body).get("params") or {} for body in posted if body]
    windows = {(p["dates_start"], p["dates_end"]) for p in sent if "dates_start" in p}
    assert windows, f"no tile run carried the daterange: {sent}"
    return windows


def _expected_windows(window):
    previous = compare_window("previous_period", *window)
    return {tuple(window), (previous["start"], previous["end"])}


def test_a_daterange_preset_uses_the_server_day_not_the_browsers_utc_day(page, tmp_path_factory):
    """#673: at 18:30 in Los Angeles the browser's UTC date is already tomorrow, so the
    filter bar used to run the demo's `last_60_days` over a window one day ahead of the
    one the API, the CLI and MCP resolve for the same dashboard."""
    root = tmp_path_factory.mktemp("tzpreset")
    create_demo(root)
    assert "default: last_60_days" in (root / ".sqldash" / "demo.yaml").read_text()
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    today = date.today()
    posted: list[str] = []
    context = page.context.browser.new_context(
        viewport={"width": 1400, "height": 1000}, timezone_id="America/Los_Angeles"
    )
    tab = context.new_page()
    tab.set_default_timeout(30_000)
    tab.set_default_navigation_timeout(30_000)
    tab.clock.set_fixed_time(f"{today.isoformat()}T18:30:00-07:00")
    tab.on(
        "request",
        lambda r: posted.append(r.post_data) if r.url.endswith("/api/run") else None,
    )
    try:
        tab.goto(f"http://127.0.0.1:{port}/d/demo", wait_until="load")
        assert (
            tab.evaluate("() => new Date().toISOString().slice(0, 10)")
            == (today + timedelta(days=1)).isoformat()
        ), "the clock has to put the browser's UTC date on the next day"
        _wait_tiles(tab)

        window = [(today - timedelta(days=60)).isoformat(), today.isoformat()]
        assert (
            tab.eval_on_selector_all("[data-daterange] .dr-date", "els => els.map(e => e.value)")
            == window
        )
        assert _run_windows(posted) == _expected_windows(window)

        posted.clear()
        picked = [(today - timedelta(days=30)).isoformat(), today.isoformat()]
        with tab.expect_request(lambda r: r.url.endswith("/api/run"), timeout=15_000):
            tab.select_option("[data-daterange] .dr-preset", "last_30_days")
        _wait_tiles(tab)
        assert (
            tab.eval_on_selector_all("[data-daterange] .dr-date", "els => els.map(e => e.value)")
            == picked
        )
        assert _run_windows(posted) == _expected_windows(picked)
    finally:
        try:
            _stop_server(server, thread, tab)
        finally:
            context.close()


def _stuck_tiles(tab, timeout=20):
    deadline = time.monotonic() + timeout
    stuck = None
    while time.monotonic() < deadline:
        stuck = tab.evaluate(
            "() => [...document.querySelectorAll('.tile')]"
            ".filter(t => t.querySelector('.tile-status .skeleton'))"
            ".map(t => t.dataset.tileId)"
        )
        if not stuck:
            return []
        time.sleep(0.2)
    return stuck


@pytest.mark.parametrize("variant", ["url", "authored_default"])
def test_a_preset_select_does_not_strand_the_tiles_it_does_not_touch(
    page, tmp_path_factory, variant
):
    """A select that starts on a non-`all` value, from the URL or its authored
    default, re-runs only its own tiles. That partial run used to invalidate the
    initial full run, so a tile that never reads the filter stayed a skeleton."""
    root = tmp_path_factory.mktemp("preset")
    create_demo(root)
    path = "/d/demo?f_region=eu"
    if variant == "authored_default":
        demo = root / ".sqldash" / "demo.yaml"
        text = demo.read_text()
        assert "    options_sql:" in text
        demo.write_text(text.replace("    options_sql:", "    default: us\n    options_sql:", 1))
        path = "/d/demo"
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    tab = page.context.new_page()
    tab.set_default_timeout(30_000)
    try:
        tab.goto(f"http://127.0.0.1:{port}{path}", wait_until="load")
        tab.wait_for_function(
            "() => document.querySelector('select[data-filter=\"region\"]').value !== 'all'"
        )
        assert _stuck_tiles(tab) == []
        assert tab.locator(".tile-status .err").count() == 0
        pie = tab.locator('.tile[data-tile-id="revenue_share_by_region"] .chart-mount canvas')
        assert pie.count() == 1
    finally:
        tab.close()
        _stop_server(server, thread, page)


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_big_integers_and_high_scale_decimals_render_exactly(page, tmp_path_factory, scheme):
    """JSON.parse rounds integers past 2^53 and Number() rounds decimals, so tables
    and big numbers showed 12345678901234567890 as ...567,000 and -1E-10 as -0."""
    root = tmp_path_factory.mktemp("exact")
    (root / "d.yaml").write_text(
        "title: N\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "queries:\n"
        "  q: |\n"
        "    SELECT * FROM (VALUES\n"
        "      ('a', 12345678901234567890::HUGEINT,"
        " 12345678901234567890.0123456789::DECIMAL(38,10)),\n"
        "      ('b', 9007199254740993::HUGEINT, '-1E-10'::DECIMAL(38,10)),\n"
        "      ('c', 9007199254740992::HUGEINT, 1.5::DECIMAL(38,10))\n"
        "    ) t(label, i, d)\n"
        "tiles:\n"
        "  - {title: T, query: q, chart: table, size: 12x4}\n"
        "  - {title: B, chart: big_number, size: 4x2,"
        ' sql: "SELECT 9007199254740993::BIGINT AS v"}\n'
        "  - {title: C, query: q, chart: {type: bar, x: label, y: [d]}, size: 8x4}\n"
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    context = page.context.browser.new_context(color_scheme=scheme)
    tab = context.new_page()
    tab.set_default_timeout(30_000)
    try:
        tab.goto(f"http://127.0.0.1:{port}/d/d", wait_until="load")
        _wait_tiles(tab)
        cells = tab.eval_on_selector_all(
            "table.results tbody tr",
            "rows => rows.map(r => [...r.children].map(c => c.textContent))",
        )
        assert cells == [
            ["a", "12,345,678,901,234,567,890", "12,345,678,901,234,567,890.0123456789"],
            ["b", "9,007,199,254,740,993", "-0.0000000001"],
            ["c", "9,007,199,254,740,992", "1.5"],
        ]
        assert tab.inner_text(".big-number .value") == "9,007,199,254,740,993"
        tab.click("table.results th:nth-child(2)")
        assert tab.eval_on_selector_all(
            "table.results tbody tr", "rows => rows.map(r => r.children[0].textContent)"
        ) == ["c", "b", "a"]
        assert tab.locator(".chart-mount canvas").count() == 1
        assert tab.locator(".tile-status .err").count() == 0
    finally:
        try:
            _stop_server(server, thread, tab)
        finally:
            context.close()


def test_non_finite_floats_json_and_binary_render_as_the_warehouse_returned_them(
    page, tmp_path_factory
):
    """NaN and ±Infinity arrived as null and showed as `null`, a chart of them
    drew empty axes with nothing saying why, and BINARY showed as base64."""
    root = tmp_path_factory.mktemp("nonfinite")
    (root / "d.yaml").write_text(
        "title: N\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "queries:\n"
        "  q: |\n"
        "    SELECT * FROM (VALUES\n"
        "      ('a', 'NaN'::DOUBLE, 'ab'::BLOB, [1, NULL]),\n"
        "      ('b', 'Infinity'::DOUBLE, NULL, NULL),\n"
        "      ('c', '-Infinity'::DOUBLE, NULL, NULL),\n"
        "      ('d', 2.5::DOUBLE, NULL, NULL),\n"
        "      ('e', NULL::DOUBLE, NULL, NULL)\n"
        "    ) t(label, f, b, l)\n"
        "  nan_line: |\n"
        "    SELECT * FROM (VALUES (DATE '2026-09-01', 'NaN'::DOUBLE),"
        " (DATE '2026-09-02', 'Infinity'::DOUBLE)) t(d, f)\n"
        "  mixed_line: |\n"
        "    SELECT * FROM (VALUES (DATE '2026-09-01', 'NaN'::DOUBLE),"
        " (DATE '2026-09-02', 1.5::DOUBLE)) t(d, f)\n"
        "tiles:\n"
        "  - {title: T, query: q, chart: table, size: 12x4}\n"
        "  - {title: Empty, query: nan_line, chart: line, size: 6x4}\n"
        "  - {title: Mixed, query: mixed_line, chart: line, size: 6x4}\n"
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    tab = page.context.new_page()
    tab.set_default_timeout(30_000)
    try:
        tab.goto(f"http://127.0.0.1:{port}/d/d", wait_until="load")
        _wait_tiles(tab)
        cells = tab.eval_on_selector_all(
            "table.results tbody tr",
            "rows => rows.map(r => [...r.children].map(c => c.textContent))",
        )
        assert cells == [
            ["a", "NaN", "6162", "[1,null]"],
            ["b", "Infinity", "null", "null"],
            ["c", "-Infinity", "null", "null"],
            ["d", "2.5", "null", "null"],
            ["e", "null", "null", "null"],
        ]
        tab.click("table.results th:nth-child(2)")
        assert tab.eval_on_selector_all(
            "table.results tbody tr", "rows => rows.map(r => r.children[0].textContent)"
        ) == ["c", "d", "b", "a", "e"]
        empty = tab.locator('.tile[data-tile-id="empty"] .tile-body')
        assert empty.locator(".chart-empty").inner_text() == "No values to plot"
        assert empty.locator(".chart-mount").is_hidden()
        mixed = tab.locator('.tile[data-tile-id="mixed"] .tile-body')
        assert mixed.locator(".chart-empty").count() == 0
        assert mixed.locator(".chart-mount canvas").is_visible()
        assert tab.locator(".tile-status .err").count() == 0
    finally:
        _stop_server(server, thread, tab)


_REFERENCES_DASHBOARD = """\
title: R
source: {type: duckdb, attach_files: true}
filters:
  - {name: dates, type: daterange, default: last_60_days}
tiles:
  - {title: Total, metric: revenue}
  - title: By category
    chart:
      type: bar
      format: currency
      references:
        - {metric: revenue, label: All revenue}
        - {y: 900000000, label: Moonshot}
        - {x: garden}
    sql: |
      SELECT category, SUM(amount) AS revenue FROM orders
      WHERE order_date BETWEEN {{ dates_start }} AND {{ dates_end }} GROUP BY 1
"""


def _reference_state(page, tile_id):
    return page.evaluate(
        """(id) => {
          const mount = document.querySelector(`.tile[data-tile-id="${id}"] .chart-mount`);
          const option = echarts.getInstanceByDom(mount).getOption();
          const lines = option.series.find((s) => s.name === '__reference_lines');
          const axis = echarts.getInstanceByDom(mount).getModel().getComponent('yAxis').axis;
          return {
            lines: (lines?.markLine?.data ?? [])
              .map((d) => ({y: d.yAxis, text: d.label.formatter})),
            legend: option.legend[0].data ?? null,
            axisMax: axis.scale.getExtent()[1],
          };
        }""",
        tile_id,
    )


def test_reference_lines_draw_a_metric_value_and_reach_past_the_data(page, tmp_path_factory):
    root = tmp_path_factory.mktemp("references")
    create_demo(root)
    (root / ".sqldash" / "r.yaml").write_text(_REFERENCES_DASHBOARD)
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    runs = []

    def note_run(request):
        if request.method == "POST" and request.url.endswith("/api/run"):
            runs.append(json.loads(request.post_data))

    page.on("request", note_run)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/r", wait_until="load")
        _wait_tiles(page)
        page.wait_for_function(
            """() => {
              const m = document.querySelector('.tile[data-tile-id="by_category"] .chart-mount');
              return m && echarts.getInstanceByDom(m);
            }"""
        )
        state = _reference_state(page, "by_category")
        total = page.locator('.tile[data-tile-id="total"] .big-number .value').inner_text()
        metric_line = next(
            line for line in state["lines"] if line["text"].startswith("All revenue")
        )
        assert metric_line["text"] == f"All revenue  {total}", (state, total)
        assert any(line["y"] == 900000000 for line in state["lines"]), state
        assert state["axisMax"] >= 900000000, state
        assert len(state["lines"]) == 2, state
        metric_runs = [r for r in runs if r.get("metric") == "revenue"]
        assert len(metric_runs) == 1, runs
    finally:
        page.remove_listener("request", note_run)
        _stop_server(server, thread, page)


def test_a_metric_reference_follows_the_filters_on_an_unfiltered_query(page, tmp_path_factory):
    root = tmp_path_factory.mktemp("staleref")
    create_demo(root)
    (root / ".sqldash" / "r.yaml").write_text(
        "title: R\n"
        "source: {type: duckdb, attach_files: true}\n"
        "filters:\n"
        "  - {name: dates, type: daterange, default: last_60_days}\n"
        "tiles:\n"
        "  - title: All time\n"
        "    chart: {type: bar, references: [{metric: revenue, label: In range}]}\n"
        "    sql: SELECT category, SUM(amount) AS revenue FROM orders GROUP BY 1\n"
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/r", wait_until="load")
        _wait_tiles(page)
        read = """() => {
          const m = document.querySelector('.tile[data-tile-id="all_time"] .chart-mount');
          const chart = m && echarts.getInstanceByDom(m);
          const refs = chart?.getOption().series.find((s) => s.name === '__reference_lines');
          return refs ? refs.markLine.data[0].label.formatter : null;
        }"""
        page.wait_for_function(read)
        before = page.evaluate(read)
        page.evaluate(
            """() => {
              const start = document.querySelector('[data-filter="dates_start"]');
              const end = new Date(document.querySelector('[data-filter="dates_end"]').value);
              start.value = new Date(end - 14 * 86400000).toISOString().slice(0, 10);
              start.dispatchEvent(new Event('change', {bubbles: true}));
            }"""
        )
        page.wait_for_function(
            f"() => {{ const now = ({read})(); return now && now !== {json.dumps(before)}; }}"
        )
        assert page.evaluate(read).startswith("In range  "), page.evaluate(read)
    finally:
        _stop_server(server, thread, page)


def test_chart_builder_adds_a_reference_and_saves_it(page, tmp_path_factory):
    root = tmp_path_factory.mktemp("builderrefs")
    (root / "d.yaml").write_text(
        "title: D\nsource: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  - title: T\n"
        "    chart: {type: bar, x: c, y: [n]}\n"
        "    sql: \"SELECT c, n FROM (VALUES ('a', 1), ('b', 3)) t(c, n)\"\n"
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/d/query?tile=t", wait_until="load")
        page.click("#run-btn")
        page.wait_for_selector("#qb-preview canvas")
        page.locator(".ref-add").click()
        page.locator('.ref-row input[aria-label="Reference value"]').fill("10")
        page.locator('.ref-row input[aria-label="Reference label"]').fill("Target")
        page.locator(".ref-add").click()
        preview = page.evaluate(
            """() => {
              const mount = document.querySelector('#qb-preview .chart-mount');
              const chart = echarts.getInstanceByDom(mount);
              const refs = chart.getOption().series.find((s) => s.name === '__reference_lines');
              return refs.markLine.data.map((d) => d.label.formatter);
            }"""
        )
        assert preview == ["Target  10"], preview
        page.locator("#qb-type .seg-btn", has_text="Pie").click()
        assert "set aside" in page.locator(".ref-parked").inner_text()
        page.locator("#qb-type .seg-btn", has_text="Line").click()
        assert page.locator(".ref-row").count() == 2
        page.click("#qb-add")
        page.wait_for_url("**/d/d?edit=1")
        text = (root / "d.yaml").read_text()
        assert (
            "    chart: {type: line, x: c, y: [n], references: [{y: 10, label: Target}]}\n" in text
        ), text
    finally:
        _stop_server(server, thread, page)


def test_a_reference_the_warehouse_refuses_is_named_on_the_tile(page, tmp_path_factory):
    root = tmp_path_factory.mktemp("refusedref")
    create_demo(root)
    metrics = root / ".sqldash" / "metrics.yaml"
    metrics.write_text(
        metrics.read_text() + "\n  trailing_revenue:\n    relation: orders\n    expr: SUM(amount)\n"
        "    window: 28 days\n    time_dimension: {name: order_date, grain: day}\n"
    )
    (root / ".sqldash" / "r.yaml").write_text(
        "title: R\n"
        "source: {type: duckdb, attach_files: true}\n"
        "filters:\n"
        "  - {name: dates, type: daterange, default: last_60_days}\n"
        "tiles:\n"
        "  - title: Bars\n"
        "    chart: {type: bar, references: [{metric: trailing_revenue}, {y: 5, label: Five}]}\n"
        "    sql: SELECT category, SUM(amount) AS revenue FROM orders GROUP BY 1\n"
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/r", wait_until="load")
        _wait_tiles(page)
        note = page.locator('.tile[data-tile-id="bars"] .reference-note')
        note.wait_for()
        assert "reference 'trailing_revenue' is not drawn" in note.inner_text()
        assert "omit start" in note.inner_text()
        lines = page.evaluate(
            """() => {
              const m = document.querySelector('.tile[data-tile-id="bars"] .chart-mount');
              const refs = echarts.getInstanceByDom(m).getOption().series
                .find((s) => s.name === '__reference_lines');
              return refs.markLine.data.map((d) => d.label.formatter);
            }"""
        )
        assert lines == ["Five  5"], lines
    finally:
        _stop_server(server, thread, page)


def test_a_reference_to_a_metric_named_proto_uses_the_chart_format(page, tmp_path_factory):
    root = tmp_path_factory.mktemp("protoref")
    create_demo(root)
    metrics = root / ".sqldash" / "metrics.yaml"
    metrics.write_text(
        metrics.read_text() + "\n  __proto__:\n    relation: orders\n    expr: SUM(amount)\n"
    )
    (root / ".sqldash" / "r.yaml").write_text(
        "title: R\n"
        "source: {type: duckdb, attach_files: true}\n"
        "tiles:\n"
        "  - title: Bars\n"
        "    chart:\n"
        "      type: bar\n"
        "      format: currency\n"
        "      references: [{metric: __proto__, label: All}]\n"
        "    sql: SELECT category, SUM(amount) AS revenue FROM orders GROUP BY 1\n"
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/r", wait_until="load")
        _wait_tiles(page)
        read = """() => {
          const m = document.querySelector('.tile[data-tile-id="bars"] .chart-mount');
          const chart = m && echarts.getInstanceByDom(m);
          const refs = chart?.getOption().series.find((s) => s.name === '__reference_lines');
          return refs ? refs.markLine.data[0].label.formatter : null;
        }"""
        page.wait_for_function(read)
        assert page.evaluate(read).startswith("All  $"), page.evaluate(read)
    finally:
        _stop_server(server, thread, page)


def _pick(page, label, value):
    page.evaluate(
        """([label, value]) => {
          const select = document.querySelector(`select[aria-label="${label}"]`);
          select.value = value;
          select.dispatchEvent(new Event('change', {bubbles: true}));
        }""",
        [label, value],
    )


def test_combo_chart_draws_two_axes_and_the_builder_saves_it(page, tmp_path_factory):
    root = tmp_path_factory.mktemp("combo")
    (root / "d.yaml").write_text(
        "title: D\nsource: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  - title: T\n"
        "    chart:\n"
        "      type: bar\n"
        "      x: w\n"
        "      y: [revenue, rate]\n"
        "      format: {revenue: currency}\n"
        "    sql: \"SELECT w, revenue, rate FROM (VALUES ('a', 50000, 0.25), ('b', 60000, NULL))"
        ' t(w, revenue, rate)"\n'
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/d/query?tile=t", wait_until="load")
        page.click("#run-btn")
        page.wait_for_selector("#qb-preview canvas")
        _pick(page, "Mark for rate", "line")
        _pick(page, "Axis for rate", "right")
        _pick(page, "Format for rate", "percent")
        page.locator('input[aria-label="Legend name for rate"]').fill("Conversion")
        page.locator('input[aria-label="Right axis title"]').fill("Rate")
        preview = page.evaluate(
            """() => {
              const mount = document.querySelector('#qb-preview .chart-mount');
              const option = echarts.getInstanceByDom(mount).getOption();
              return {
                types: option.series.map((s) => s.type),
                axes: option.series.map((s) => s.yAxisIndex),
                names: option.series.map((s) => s.name),
                yAxes: option.yAxis.map((a) => a.name || null),
              };
            }"""
        )
        assert preview == {
            "types": ["bar", "line"],
            "axes": [0, 1],
            "names": ["revenue", "Conversion"],
            "yAxes": [None, "Rate"],
        }, preview
        page.click("#qb-add")
        page.wait_for_url("**/d/d?edit=1")
        text = (root / "d.yaml").read_text()
        assert (
            "      format: {revenue: currency, rate: percent}\n"
            "      series: {rate: {type: line, axis: right, label: Conversion}}\n"
            "      axes: {right: {title: Rate}}\n"
        ) in text, text
        _wait_tiles(page)
        page.wait_for_function(
            """() => {
              const m = document.querySelector('.tile[data-tile-id="t"] .chart-mount');
              return m && echarts.getInstanceByDom(m);
            }"""
        )
        tile = page.evaluate(
            """() => {
              const m = document.querySelector('.tile[data-tile-id="t"] .chart-mount');
              const option = echarts.getInstanceByDom(m).getOption();
              return {
                yAxes: option.yAxis.length,
                right: option.yAxis[1].axisLabel.formatter(0.5),
                left: option.yAxis[0].axisLabel.formatter(50000),
              };
            }"""
        )
        assert tile == {"yAxes": 2, "right": "50%", "left": "$50K"}, tile
    finally:
        _stop_server(server, thread, page)


def test_dropping_the_last_left_series_in_the_builder_saves_a_valid_chart(page, tmp_path_factory):
    root = tmp_path_factory.mktemp("comboleft")
    (root / "d.yaml").write_text(
        "title: D\nsource: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  - title: T\n"
        "    chart:\n"
        "      type: bar\n"
        "      x: w\n"
        "      y: [revenue, rate]\n"
        "      series: {rate: {type: line, axis: right}}\n"
        "      axes: {right: {title: Rate}}\n"
        "    sql: \"SELECT w, revenue, rate FROM (VALUES ('a', 50000, 0.25), ('b', 60000, 0.3))"
        ' t(w, revenue, rate)"\n'
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/d/query?tile=t", wait_until="load")
        page.click("#run-btn")
        page.wait_for_selector("#qb-preview canvas")
        page.locator('[data-spec-y][value="revenue"]').uncheck()
        page.click("#qb-add")
        page.wait_for_url("**/d/d?edit=1")
        store = DashboardStore(root)
        dashboard, _, _ = store.load("d")
        chart = dashboard.tiles[0].chart
        assert chart.y == ["rate"], (root / "d.yaml").read_text()
        assert all(s.axis != "right" for s in chart.series.values()), (root / "d.yaml").read_text()
        assert "right" not in chart.axes, (root / "d.yaml").read_text()
    finally:
        _stop_server(server, thread, page)


def test_the_builder_keeps_a_proto_named_series_through_an_edit(page, tmp_path_factory):
    root = tmp_path_factory.mktemp("comboproto")
    (root / "d.yaml").write_text(
        "title: D\nsource: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  - title: T\n"
        "    chart:\n"
        "      type: bar\n"
        "      x: w\n"
        "      y: [revenue, __proto__]\n"
        "      format: {revenue: currency, __proto__: percent}\n"
        "      series: {__proto__: {type: line, axis: right, label: Rate}}\n"
        "    sql: \"SELECT w, revenue, r AS __proto__ FROM (VALUES ('a', 50000, 0.25),"
        " ('b', 60000, 0.3)) t(w, revenue, r)\"\n"
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/d/query?tile=t", wait_until="load")
        page.click("#run-btn")
        page.wait_for_selector("#qb-preview canvas")
        assert page.locator('select[aria-label="Format for __proto__"]').input_value() == "percent"
        page.locator('input[aria-label="Legend name for revenue"]').fill("Revenue")
        page.click("#qb-add")
        page.wait_for_url("**/d/d?edit=1")
        chart = DashboardStore(root).load("d")[0].tiles[0].chart
        text = (root / "d.yaml").read_text()
        assert chart.series["__proto__"].axis == "right", text
        assert chart.series["__proto__"].label == "Rate", text
        assert chart.series["revenue"].label == "Revenue", text
        assert chart.format == {"revenue": "currency", "__proto__": "percent"}, text
    finally:
        _stop_server(server, thread, page)


def test_after_promotion_the_builder_edits_the_promoted_axis(page, tmp_path_factory):
    root = tmp_path_factory.mktemp("combopromote")
    (root / "d.yaml").write_text(
        "title: D\nsource: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  - title: T\n"
        "    chart:\n"
        "      type: bar\n"
        "      x: w\n"
        "      y: [revenue, rate, margin]\n"
        "      format: {revenue: currency, rate: percent, margin: percent}\n"
        "      series: {rate: {type: line, axis: right}, margin: {type: line, axis: right}}\n"
        "      axes:\n"
        "        left: {title: Revenue, min: 10000, max: 100000}\n"
        "        right: {title: Rate, min: 0, max: 1}\n"
        "    sql: \"SELECT w, revenue, rate, margin FROM (VALUES ('a', 50000, 0.25, 0.4),"
        " ('b', 60000, 0.5, 0.3)) t(w, revenue, rate, margin)\"\n"
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/d/query?tile=t", wait_until="load")
        page.click("#run-btn")
        page.wait_for_selector("#qb-preview canvas")
        page.locator('[data-spec-y][value="revenue"]').uncheck()
        assert page.locator('select[aria-label="Axis for rate"]').input_value() == "left"
        assert page.locator('select[aria-label="Axis for margin"]').input_value() == "left"
        title = page.locator('input[aria-label="Left axis title"]')
        assert title.input_value() == "Rate"
        _pick(page, "Axis for rate", "left")
        page.locator('input[aria-label="Left axis title"]').fill("Share")
        extent = page.evaluate(
            """() => {
              const mount = document.querySelector('#qb-preview .chart-mount');
              const chart = echarts.getInstanceByDom(mount);
              return chart.getModel().getComponent('yAxis').axis.scale.getExtent();
            }"""
        )
        assert extent == [0, 1], extent
        page.click("#qb-add")
        page.wait_for_url("**/d/d?edit=1")
        chart = DashboardStore(root).load("d")[0].tiles[0].chart
        text = (root / "d.yaml").read_text()
        assert chart.y == ["rate", "margin"], text
        assert all(s.axis != "right" for s in chart.series.values()), text
        assert chart.axes["left"].model_dump(exclude_none=True) == {
            "title": "Share",
            "min": 0,
            "max": 1,
        }, text
        assert "right" not in chart.axes, text
    finally:
        _stop_server(server, thread, page)


def test_a_query_that_drops_the_left_column_promotes_the_builder_state(page, tmp_path_factory):
    root = tmp_path_factory.mktemp("combodrop")
    (root / "d.yaml").write_text(
        "title: D\nsource: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  - title: T\n"
        "    chart:\n"
        "      type: bar\n"
        "      x: w\n"
        "      y: [revenue, rate, margin]\n"
        "      series: {rate: {type: line, axis: right}, margin: {type: line, axis: right}}\n"
        "      axes:\n"
        "        left: {title: Revenue, min: 10000, max: 100000}\n"
        "        right: {title: Rate, min: 0, max: 1}\n"
        "    sql: \"SELECT w, revenue, rate, margin FROM (VALUES ('a', 50000, 0.25, 0.4),"
        " ('b', 60000, 0.5, 0.3)) t(w, revenue, rate, margin)\"\n"
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/d/query?tile=t", wait_until="load")
        page.click("#run-btn")
        page.wait_for_selector("#qb-preview canvas")
        page.evaluate(
            "(sql) => ace.edit('sql-editor').setValue(sql, -1)",
            "SELECT w, rate, margin FROM (VALUES ('a', 0.25, 0.4), ('b', 0.5, 0.3))"
            " t(w, rate, margin)",
        )
        page.click("#run-btn")
        page.wait_for_function(
            """() => !document.querySelector('[data-spec-y][value="revenue"]')"""
        )
        assert page.locator('select[aria-label="Axis for rate"]').input_value() == "left"
        assert page.locator('input[aria-label="Left axis title"]').input_value() == "Rate"
        _pick(page, "Axis for rate", "left")
        extent = page.evaluate(
            """() => {
              const mount = document.querySelector('#qb-preview .chart-mount');
              return echarts.getInstanceByDom(mount).getModel().getComponent('yAxis')
                .axis.scale.getExtent();
            }"""
        )
        assert extent == [0, 1], extent
    finally:
        _stop_server(server, thread, page)


def test_a_reference_outside_fixed_bounds_is_named_on_the_tile(page, tmp_path_factory):
    root = tmp_path_factory.mktemp("refbounds")
    (root / "d.yaml").write_text(
        "title: D\nsource: {type: duckdb, database: ':memory:'}\n"
        "tiles:\n"
        "  - title: T\n"
        "    chart:\n"
        "      type: bar\n"
        "      x: w\n"
        "      y: [revenue]\n"
        "      axes: {left: {min: 0, max: 100}}\n"
        "      references: [{y: 15, label: Inside}, {y: 250, label: Outside}]\n"
        "    sql: \"SELECT w, revenue FROM (VALUES ('a', 10), ('b', 20)) t(w, revenue)\"\n"
    )
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        page.goto(f"http://127.0.0.1:{port}/d/d", wait_until="load")
        _wait_tiles(page)
        note = page.locator('.tile[data-tile-id="t"] .reference-note')
        note.wait_for()
        assert note.inner_text() == (
            "reference 'Outside' is not drawn: 250 is outside the axis bounds (0 to 100)"
        )
        extent = page.evaluate(
            """() => {
              const m = document.querySelector('.tile[data-tile-id="t"] .chart-mount');
              return echarts.getInstanceByDom(m).getModel().getComponent('yAxis')
                .axis.scale.getExtent();
            }"""
        )
        assert extent == [0, 100], extent
    finally:
        _stop_server(server, thread, page)
