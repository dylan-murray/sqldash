"""Drill-down in a real browser: a bar and a table cell open the destination
dashboard filtered to what was clicked, the date range comes along, the
breadcrumb goes back to the filters the overview had, and a broken link or a
value the destination cannot show says so instead of opening it unfiltered."""

from urllib.parse import parse_qs, urlparse

import pytest

from sqldash.scaffold import create_demo
from sqldash.server import create_app
from sqldash.snapshot import _start_server
from tests.test_browser_smoke import _browser_available, _stop_server, _wait_tiles

pytestmark = pytest.mark.skipif(not _browser_available(), reason="playwright browser not installed")

DETAIL = """title: Category detail
source: {type: duckdb, attach_files: true}
filters:
  - {name: dates, type: daterange, default: last_60_days}
  - name: category
    type: select
    options_sql: "SELECT DISTINCT category FROM orders ORDER BY category"
tiles:
  - title: Category revenue
    chart: big_number
    sql: |
      SELECT ROUND(SUM(amount), 2) AS revenue FROM orders
      WHERE order_date BETWEEN {{ dates_start }} AND {{ dates_end }}
        {% if category %}AND category = {{ category }}{% endif %}
"""

DRILL = """    drill:
      dashboard: category_detail
      filters:
        category: category
        dates: {filter: dates}
"""


@pytest.fixture(scope="module")
def served(tmp_path_factory):
    root = tmp_path_factory.mktemp("drill")
    create_demo(root)
    demo = root / ".sqldash" / "demo.yaml"
    text = demo.read_text()
    bar = "  - title: Revenue by category\n    chart: bar\n"
    text = text.replace(bar, bar + DRILL)
    table = "  - title: Recent orders\n    chart: table\n"
    linked = DRILL.replace("      filters:", "      column: category\n      filters:")
    text = text.replace(table, table + linked)
    text += (
        "\n  - title: Broken drill\n"
        "    chart: bar\n"
        "    drill: {dashboard: category_detail, filters: {categry: category}}\n"
        "    sql: \"SELECT 'x' AS category, 1 AS n\"\n"
    )
    demo.write_text(text)
    (root / ".sqldash" / "category_detail.yaml").write_text(DETAIL)
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    yield f"http://127.0.0.1:{port}"
    _stop_server(server, thread)


@pytest.fixture
def page(served):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(timeout=30_000)
        page = browser.new_page(viewport={"width": 1400, "height": 1000})
        page.set_default_timeout(30_000)
        yield page
        browser.close()


def _bar(page, tile, index):
    page.locator(f'.tile[data-tile-id="{tile}"]').scroll_into_view_if_needed()
    page.wait_for_function(
        f"""() => {{
            const m = document.querySelector('.tile[data-tile-id="{tile}"] .chart-mount');
            return Boolean(m && echarts.getInstanceByDom(m));
        }}"""
    )
    return page.evaluate(
        """([tile, index]) => {
          const mount = document.querySelector(`.tile[data-tile-id="${tile}"] .chart-mount`);
          const chart = echarts.getInstanceByDom(mount);
          const datum = chart.getOption().series[0].data[index];
          const [x, top] = chart.convertToPixel({seriesIndex: 0}, datum);
          const [, bottom] = chart.convertToPixel({seriesIndex: 0}, [datum[0], 0]);
          const r = mount.getBoundingClientRect();
          return {x: r.x + x, y: r.y + (top + bottom) / 2, name: datum[0], value: datum[1]};
        }""",
        [tile, index],
    )


def _query(page):
    return {k: v[0] for k, v in parse_qs(urlparse(page.url).query).items()}


def test_a_bar_opens_the_detail_filtered_and_the_crumb_goes_back(page, served):
    page.goto(f"{served}/d/demo")
    _wait_tiles(page)
    sixty = _bar(page, "revenue_by_category", 1)["value"]
    page.select_option(".dr-preset", "last_30_days")
    page.wait_for_function("() => location.search.includes('f_dates_start')")
    page.wait_for_function(
        """(before) => {
            const m = document.querySelector(
                '.tile[data-tile-id="revenue_by_category"] .chart-mount');
            return echarts.getInstanceByDom(m).getOption().series[0].data[1][1] !== before;
        }""",
        arg=sixty,
    )
    overview = page.url
    dates = page.eval_on_selector_all(".dr-date", "els => els.map(e => e.value)")
    bar = _bar(page, "revenue_by_category", 1)
    page.mouse.click(bar["x"], bar["y"])
    page.wait_for_url("**/d/category_detail?**")
    _wait_tiles(page)
    query = _query(page)
    assert query["f_category"] == bar["name"]
    assert [query["f_dates_start"], query["f_dates_end"]] == dates
    assert query["from"] == "demo"
    assert page.eval_on_selector('select[data-filter="category"]', "e => e.value") == bar["name"]
    assert page.eval_on_selector(".dr-preset", "e => e.value") == "last_30_days"
    page.wait_for_function(
        """(want) => {
            const text = document.querySelector('.big-number .value')?.textContent ?? '';
            return Math.abs(Number(text.replace(/[^0-9.]/g, '')) - want) < 0.5;
        }""",
        arg=bar["value"],
    )
    page.reload()
    _wait_tiles(page)
    assert page.eval_on_selector('select[data-filter="category"]', "e => e.value") == bar["name"]
    assert page.locator("#drill-back").text_content().strip() == "Order Analytics"
    page.go_back()
    page.wait_for_url("**/d/demo?**")
    page.go_forward()
    page.wait_for_url("**/d/category_detail?**")
    page.click("#drill-back")
    page.wait_for_url("**/d/demo?**")
    _wait_tiles(page)
    assert page.url == overview
    assert page.eval_on_selector_all(".dr-date", "els => els.map(e => e.value)") == dates


def test_a_table_cell_is_a_keyboard_link_and_can_open_a_new_tab(page, served):
    page.goto(f"{served}/d/demo")
    _wait_tiles(page)
    link = page.locator('.tile[data-tile-id="recent_orders"] a.cell-link').first
    category = link.text_content()
    href = link.get_attribute("href")
    assert parse_qs(urlparse(href).query)["f_category"] == [category]
    with page.context.expect_page() as opened:
        link.click(modifiers=["ControlOrMeta"])
    tab = opened.value
    tab.wait_for_url("**/d/category_detail?**")
    tab.close()
    assert "/d/demo" in page.url
    assert page.locator(".cell-pop").count() == 0
    link.focus()
    page.keyboard.press("Enter")
    page.wait_for_url("**/d/category_detail?**")
    assert _query(page)["f_category"] == category


def test_arrow_keys_pick_a_bar_and_enter_drills(page, served):
    page.goto(f"{served}/d/demo")
    _wait_tiles(page)
    bar = _bar(page, "revenue_by_category", 2)
    mount = page.locator('.tile[data-tile-id="revenue_by_category"] .chart-mount')
    assert "Enter opens Category detail" in mount.get_attribute("aria-label")
    mount.focus()
    for _ in range(3):
        page.keyboard.press("ArrowRight")
    page.keyboard.press("Enter")
    page.wait_for_url("**/d/category_detail?**")
    assert _query(page)["f_category"] == bar["name"]


def test_a_broken_drill_says_why_and_stays_put(page, served):
    page.goto(f"{served}/d/demo")
    _wait_tiles(page)
    hint = page.locator('.tile[data-tile-id="broken_drill"] .tile-drill')
    assert hint.text_content() == "Drill unavailable"
    assert "'categry' is not a filter" in hint.get_attribute("title")
    bar = _bar(page, "broken_drill", 0)
    page.mouse.click(bar["x"], bar["y"])
    page.wait_for_selector(".toast-error")
    assert "'categry' is not a filter" in page.locator(".toast-error").first.text_content()
    assert "/d/demo" in page.url


def test_a_value_the_destination_cannot_show_is_named(page, served):
    page.goto(f"{served}/d/category_detail?f_category=%3Cb%3Enope%3C%2Fb%3E&from=demo")
    _wait_tiles(page)
    page.wait_for_selector(".toast-error")
    assert "has no '<b>nope</b>' to filter to" in page.locator(".toast-error").text_content()
    assert page.locator(".toast-error b").count() == 0
    assert page.eval_on_selector('select[data-filter="category"]', "e => e.value") == "all"
