"""Drill-down and cross-filter in a real browser. A bar and a table cell open the
destination dashboard filtered to what was clicked, the date range comes along,
the breadcrumb goes back to the filters the overview had, and a broken link or a
value the destination cannot show says so instead of opening it unfiltered. A
cross-filter click sets this dashboard's filter, re-queries the tiles that read
it, dims the marks it left out, and the same click or the chip clears it."""

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
    pie = "  - title: Revenue share by region\n    chart: pie\n"
    text = text.replace(pie, pie + "    cross_filter: {region: region}\n")
    text += (
        "\n  - title: Region table\n"
        "    chart: table\n"
        "    cross_filter: {region: region}\n"
        '    sql: "SELECT region, COUNT(*) AS orders FROM orders GROUP BY 1 ORDER BY 1"\n'
        "\n  - title: Muted regions\n"
        "    chart: bar\n"
        "    cross_filter: false\n"
        '    sql: "SELECT region, COUNT(*) AS orders FROM orders GROUP BY 1 ORDER BY 1"\n'
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


EDGES = {
    "ctor": """title: Ctor
source: {type: duckdb, attach_files: true}
tiles:
  - {title: Constructor, chart: table, sql: "SELECT 1 AS n"}
""",
    "daily": """title: Daily
source: {type: duckdb, attach_files: true}
filters:
  - {name: dates, type: daterange, default: last_30_days}
tiles:
  - title: Daily revenue
    chart: line
    metric: {name: revenue, grain: day, compare: previous_period}
    drill: {dashboard: day_detail, filters: {day: order_date}}
""",
    "day_detail": """title: Day detail
source: {type: duckdb, attach_files: true}
filters:
  - {name: day, type: date}
tiles:
  - {title: Rows, sql: "SELECT 1 AS n"}
""",
    "dup": """title: Dup
source: {type: duckdb, attach_files: true}
filters:
  - {name: n, type: number, default: 1}
tiles:
  - title: q
    chart: table
    sql: "SELECT 'x' AS k, {{ n }} AS n"
    drill: {dashboard: dest_a, filters: {k: k}}
  - title: q
    chart: table
    sql: "SELECT 'y' AS k, {{ n }} AS n"
    drill: {dashboard: dest_b, filters: {k: k}}
""",
    "stale": """title: Stale
source: {type: duckdb, attach_files: true}
filters:
  - {name: region, type: select, options: [all, us, eu, apac], default: us}
tiles:
  - title: Keys
    chart: table
    sql: "SELECT 'x' AS k, 1 AS n"
    drill: {dashboard: stale_dest, filters: {k: k, region: {filter: region}}}
""",
    "stale_dest": """title: Stale dest
source: {type: duckdb, attach_files: true}
filters:
  - {name: k, type: text}
  - {name: region, type: select, options: [all, us, eu]}
tiles:
  - {title: Rows, sql: "SELECT 1 AS n"}
""",
    "typed": """title: Typed
source: {type: duckdb, attach_files: true}
tiles:
  - title: Typed
    chart: table
    sql: "SELECT 1.0::DOUBLE AS rate, TRUE AS active"
    drill: {dashboard: flags, filters: {rate: rate, active: active}}
""",
    "flags": """title: Flags
source: {type: duckdb, attach_files: true}
filters:
  - {name: active, type: select, options: [true, false], default: false}
  - {name: rate, type: select, options: [1.0, 2.0]}
tiles:
  - title: Bound
    chart: table
    sql: "SELECT {{ active }} AS got_active{% if rate %}, {{ rate }} AS got_rate{% endif %}"
""",
    "codes": """title: Codes
source: {type: duckdb, attach_files: true}
tiles:
  - title: Codes
    chart: table
    sql: "SELECT '00100' AS code"
    drill: {dashboard: code_dest, filters: {code: code}}
""",
    "code_dest": """title: Code dest
source: {type: duckdb, attach_files: true}
filters:
  - {name: code, type: select, options: [all, '100']}
tiles:
  - {title: Rows, sql: "SELECT 1 AS n"}
""",
    "carry": """title: Carry
source: {type: duckdb, attach_files: true}
filters:
  - name: rate
    type: select
    options_sql: "SELECT 1.00::DECIMAL(5,2) AS rate"
tiles:
  - title: Carry
    chart: table
    sql: "SELECT 'x' AS k"
    drill: {dashboard: carry_dest, filters: {k: k, rate: {filter: rate}}}
""",
    "carry_dest": """title: Carry dest
source: {type: duckdb, attach_files: true}
filters:
  - {name: k, type: text}
  - {name: rate, type: select, options: [all, 1.0, 2.0]}
tiles:
  - {title: Rows, sql: "SELECT 1 AS n"}
""",
    "many": """title: Many
source: {type: duckdb, attach_files: true}
tiles:
  - title: Many
    chart: table
    sql: "SELECT 'k' || i AS k, i AS n FROM range(100) t(i)"
    drill: {dashboard: dest_a, filters: {k: k}}
""",
    "xf": """title: XF
source: {type: duckdb, attach_files: true}
filters:
  - {name: region, type: select, options: [all, us, eu]}
  - {name: channel, type: select, options: [all, web]}
  - {name: minimum, type: number, default: 0}
  - {name: day, type: date}
  - {name: cat, type: text}
tiles:
  - title: Categories
    chart: {type: line, x: cat, y: [n]}
    cross_filter: {cat: cat}
    sql: "SELECT 'category_' || i AS cat, i AS n FROM range(100) t(i) ORDER BY i"
  - title: Pair
    chart: table
    cross_filter: {region: region, channel: channel}
    sql: "SELECT 'eu' AS region, 'store' AS channel, 1 AS n"
  - title: Floor
    chart: table
    cross_filter: {region: region, minimum: minimum}
    sql: "SELECT 'eu' AS region, 0 AS minimum, 1 AS n"
  - title: Headline
    chart: big_number
    cross_filter: {region: region}
    sql: "SELECT 'eu' AS region, 10 AS revenue"
  - title: Trend
    chart: {type: line, x: day, y: [n]}
    cross_filter: {day: day}
    sql: "SELECT DATE '2026-01-01' + CAST(i AS INTEGER) AS day, i AS n FROM range(50) t(i)"
""",
    "fragile": """title: Fragile
source: {type: duckdb, attach_files: true}
filters:
  - {name: region, type: select, options: [all, us, eu]}
  - {name: s, type: text, default: "1"}
tiles:
  - title: Fragile
    chart: table
    cross_filter: {region: region}
    sql: "SELECT 'eu' AS region, CAST({{ s }} AS INTEGER) AS n"
""",
    "dest_a": "title: Dest A\nsource: {type: duckdb, attach_files: true}\n"
    "filters:\n  - {name: k, type: text}\ntiles:\n  - {title: A, sql: 'SELECT 1 AS n'}\n",
    "dest_b": "title: Dest B\nsource: {type: duckdb, attach_files: true}\n"
    "filters:\n  - {name: k, type: text}\ntiles:\n  - {title: B, sql: 'SELECT 1 AS n'}\n",
}


@pytest.fixture(scope="module")
def edges(tmp_path_factory):
    root = tmp_path_factory.mktemp("drill-edges")
    create_demo(root)
    for name, text in EDGES.items():
        (root / ".sqldash" / f"{name}.yaml").write_text(text)
    app = create_app(root, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    yield f"http://127.0.0.1:{port}"
    _stop_server(server, thread)


def test_a_tile_named_like_an_object_property_is_not_a_drill(page, edges):
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{edges}/d/ctor")
    _wait_tiles(page)
    tile = page.locator('.tile[data-tile-id="constructor"]')
    assert tile.locator("td").first.text_content() == "1"
    assert tile.locator(".tile-drill").count() == 0
    assert errors == []


def test_a_previous_period_point_drills_into_the_day_it_came_from(page, edges):
    page.goto(f"{edges}/d/daily")
    _wait_tiles(page)
    mount = '.tile[data-tile-id="daily_revenue"] .chart-mount'
    page.wait_for_function(
        f"() => {{ const m = document.querySelector('{mount}'); "
        "return Boolean(m && echarts.getInstanceByDom(m)); }"
    )
    start = page.eval_on_selector_all(".dr-date", "els => els.map(e => e.value)")[0]
    point = page.evaluate(
        """(mount) => {
          const el = document.querySelector(mount);
          const chart = echarts.getInstanceByDom(el);
          const series = chart.getOption().series;
          const at = series.findIndex((s) => s.name === "previous");
          const datum = series[at].data[0];
          const [x, y] = chart.convertToPixel({seriesIndex: at}, datum);
          const r = el.getBoundingClientRect();
          return {x: r.x + x, y: r.y + y, plotted: String(datum[0]).slice(0, 10)};
        }""",
        mount,
    )
    assert point["plotted"] >= start
    page.mouse.click(point["x"], point["y"])
    page.wait_for_url("**/d/day_detail?**")
    assert _query(page)["f_day"] < start


def test_deleting_a_tile_leaves_the_survivor_its_own_id_and_drill(page, edges):
    page.on("dialog", lambda d: d.accept())
    page.goto(f"{edges}/d/dup?edit=1")
    _wait_tiles(page)
    first = page.locator('.tile[data-tile-id="q"]')
    first.hover()
    first.locator('.wa-btn[data-action="delete"]').click()
    page.wait_for_function("() => document.querySelectorAll('.tile').length === 1")
    assert page.eval_on_selector(".tile", "e => e.dataset.tileId") == "q_2"
    page.fill('[data-filter="n"]', "2")
    page.keyboard.press("Enter")
    page.wait_for_function(
        "() => [...document.querySelectorAll('.tile td')].some(td => td.textContent === '2')"
    )
    link = page.locator(".tile a.cell-link").first
    assert link.text_content() == "y"
    assert urlparse(link.get_attribute("href")).path == "/d/dest_b"


def test_a_table_link_the_current_filters_make_invalid_does_not_open_its_old_href(page, edges):
    page.goto(f"{edges}/d/stale")
    _wait_tiles(page)
    link = page.locator('.tile[data-tile-id="keys"] a.cell-link').first
    assert parse_qs(urlparse(link.get_attribute("href")).query)["f_region"] == ["us"]
    page.select_option('select[data-filter="region"]', "apac")
    page.wait_for_function("() => location.search.includes('f_region=apac')")
    page.wait_for_function(
        """() => !document.querySelector('.tile[data-tile-id="keys"] a.cell-link')
            .hasAttribute('href')"""
    )
    link.hover()
    link.click()
    page.wait_for_selector(".toast-error")
    assert "'apac' is not one of the options" in page.locator(".toast-error").first.text_content()
    page.wait_for_timeout(300)
    assert "/d/stale" in page.url


def test_a_table_link_invalid_on_load_works_once_the_filters_make_it_valid(page, edges):
    page.goto(f"{edges}/d/stale?f_region=apac")
    _wait_tiles(page)
    link = page.locator('.tile[data-tile-id="keys"] a.cell-link').first
    assert link.get_attribute("href") is None
    assert "'apac' is not one of the options" in link.evaluate("a => a.closest('td').title")
    page.select_option('select[data-filter="region"]', "eu")
    page.wait_for_function(
        """() => (document.querySelector('.tile[data-tile-id="keys"] a.cell-link')
            .getAttribute('href') || '').includes('f_region=eu')"""
    )
    link.focus()
    page.keyboard.press("Enter")
    page.wait_for_url("**/d/stale_dest?**")
    assert _query(page)["f_region"] == "eu"


def _bound(page):
    page.wait_for_function(
        """() => document.querySelectorAll('.tile[data-tile-id="bound"] td').length > 0"""
    )
    return page.eval_on_selector_all(
        '.tile[data-tile-id="bound"] td', "cells => cells.map(c => c.textContent)"
    )


def test_numeric_and_boolean_cells_drill_into_static_options(page, edges):
    page.goto(f"{edges}/d/typed")
    _wait_tiles(page)
    page.locator('.tile[data-tile-id="typed"] a.cell-link').first.click()
    page.wait_for_url("**/d/flags?**")
    _wait_tiles(page)
    assert _query(page)["f_rate"] == "1.0"
    assert _query(page)["f_active"] == "True"
    assert page.eval_on_selector('select[data-filter="rate"]', "e => e.value") == "1.0"
    assert page.eval_on_selector('select[data-filter="active"]', "e => e.value") == "True"
    assert _bound(page) == ["True", "1.0"]
    assert page.locator(".toast-error").count() == 0


def test_any_url_spelling_binds_the_option_as_the_file_wrote_it(page, edges):
    page.goto(f"{edges}/d/flags?f_active=true&f_rate=2")
    _wait_tiles(page)
    assert page.eval_on_selector('select[data-filter="active"]', "e => e.value") == "True"
    assert page.eval_on_selector('select[data-filter="rate"]', "e => e.value") == "2.0"
    assert _bound(page) == ["True", "2.0"]
    page.goto(f"{edges}/d/flags?f_active=False&f_rate=2.0")
    _wait_tiles(page)
    assert _bound(page) == ["False", "2.0"]
    assert page.locator(".toast-error").count() == 0


def test_a_text_code_never_drills_into_a_differently_spelled_option(page, edges):
    page.goto(f"{edges}/d/codes")
    _wait_tiles(page)
    page.locator('.tile[data-tile-id="codes"] a.cell-link').first.click()
    page.wait_for_selector(".toast-error")
    assert "'00100' is not one of the options" in page.locator(".toast-error").first.text_content()
    assert "/d/codes" in page.url


def test_a_carried_decimal_select_drills_into_a_numeric_option(page, edges):
    page.goto(f"{edges}/d/carry?f_rate=1.00")
    _wait_tiles(page)
    page.wait_for_function(
        "() => document.querySelector('select[data-filter=\"rate\"]').value === '1.00'"
    )
    page.locator('.tile[data-tile-id="carry"] a.cell-link').first.click()
    page.wait_for_url("**/d/carry_dest?**")
    assert _query(page)["f_rate"] == "1.0"


def test_sorting_a_drill_table_keeps_only_its_visible_links(page, edges):
    page.goto(f"{edges}/d/many")
    _wait_tiles(page)
    header = page.locator('.tile[data-tile-id="many"] th').nth(1)
    for _ in range(20):
        header.click()
    count = page.evaluate("() => import('/static/js/drill.js').then((m) => m.drillLinkCount())")
    links = page.locator('.tile[data-tile-id="many"] a.cell-link').count()
    assert links == 100
    assert count == 100

    assert _query(page)["f_rate"] == "1"


def _slice(page, name):
    page.locator('.tile[data-tile-id="revenue_share_by_region"]').scroll_into_view_if_needed()
    page.wait_for_function(
        """() => {
            const m = document.querySelector(
                '.tile[data-tile-id="revenue_share_by_region"] .chart-mount');
            return Boolean(m && echarts.getInstanceByDom(m));
        }"""
    )
    return page.evaluate(
        """(name) => {
          const mount = document.querySelector(
              '.tile[data-tile-id="revenue_share_by_region"] .chart-mount');
          const chart = echarts.getInstanceByDom(mount);
          const data = chart.getModel().getSeriesByIndex(0).getData();
          const layout = data.getItemLayout(data.indexOfName(name));
          const mid = (layout.startAngle + layout.endAngle) / 2;
          const radius = (layout.r0 + layout.r) / 2;
          const r = mount.getBoundingClientRect();
          return {
            x: r.x + layout.cx + radius * Math.cos(mid),
            y: r.y + layout.cy + radius * Math.sin(mid),
          };
        }""",
        name,
    )


def _region(page):
    return page.eval_on_selector('select[data-filter="region"]', "e => e.value")


def _recent_regions(page):
    return page.eval_on_selector_all(
        '.tile[data-tile-id="recent_orders"] tbody tr td:nth-child(2)',
        "cells => [...new Set(cells.map(c => c.textContent))]",
    )


def test_a_slice_cross_filters_the_dashboard_and_the_same_click_clears_it(page, served):
    page.goto(f"{served}/d/demo")
    _wait_tiles(page)
    assert len(_recent_regions(page)) > 1
    chip = page.locator('.tile[data-tile-id="revenue_share_by_region"] .tile-xf')
    assert chip.text_content() == "Region"
    point = _slice(page, "eu")
    page.mouse.click(point["x"], point["y"])
    page.wait_for_function("() => location.search.includes('f_region=eu')")
    page.wait_for_function(
        """() => [...document.querySelectorAll(
            '.tile[data-tile-id="recent_orders"] tbody tr td:nth-child(2)')]
            .every(c => c.textContent === 'eu')"""
    )
    assert _region(page) == "eu"
    opacities = page.evaluate(
        """() => {
          const m = document.querySelector(
              '.tile[data-tile-id="revenue_share_by_region"] .chart-mount');
          return Object.fromEntries(echarts.getInstanceByDom(m).getOption().series[0].data
              .map(d => [d.name, d.itemStyle?.opacity ?? 1]));
        }"""
    )
    assert opacities["eu"] == 1
    assert all(v < 1 for k, v in opacities.items() if k != "eu"), opacities
    assert (
        page.locator('.tile[data-tile-id="revenue_share_by_region"] button.tile-xf').text_content()
        == "eu"
    )
    page.mouse.click(point["x"], point["y"])
    page.wait_for_function("() => location.search.includes('f_region=all')")
    page.wait_for_function(
        """() => new Set([...document.querySelectorAll(
            '.tile[data-tile-id="recent_orders"] tbody tr td:nth-child(2)')]
            .map(c => c.textContent)).size > 1"""
    )
    assert chip.text_content() == "Region"
    page.mouse.click(point["x"], point["y"])
    page.wait_for_function("() => location.search.includes('f_region=eu')")
    page.click('.tile[data-tile-id="revenue_share_by_region"] button.tile-xf')
    page.wait_for_function("() => location.search.includes('f_region=all')")


def test_a_table_cell_toggles_the_filter_and_marks_its_row(page, served):
    page.goto(f"{served}/d/demo")
    _wait_tiles(page)
    cell = page.locator('.tile[data-tile-id="region_table"] button.cell-filter', has_text="us")
    cell.focus()
    page.keyboard.press("Enter")
    page.wait_for_function("() => location.search.includes('f_region=us')")
    picked = page.locator('.tile[data-tile-id="region_table"] td.is-picked')
    picked.wait_for()
    assert picked.text_content() == "us"
    assert page.locator(".cell-pop").count() == 0
    page.locator('.tile[data-tile-id="region_table"] button.cell-filter', has_text="us").click()
    page.wait_for_function("() => location.search.includes('f_region=all')")
    page.wait_for_function(
        """() => !document.querySelector('.tile[data-tile-id="region_table"] td.is-picked')"""
    )


def test_cross_filter_false_turns_the_same_name_click_off(page, served):
    page.goto(f"{served}/d/demo")
    _wait_tiles(page)
    bar = _bar(page, "muted_regions", 0)
    page.mouse.click(bar["x"], bar["y"])
    page.wait_for_timeout(500)
    assert _region(page) == "all"
    assert page.locator('.tile[data-tile-id="muted_regions"] .tile-xf').count() == 0


def _xf_tile(page, tile):
    return page.locator(f'.tile[data-tile-id="{tile}"]')


def test_a_click_that_one_filter_cannot_take_changes_no_filter(page, edges):
    page.goto(f"{edges}/d/xf")
    _wait_tiles(page)
    _xf_tile(page, "pair").locator("button.cell-filter").click()
    page.wait_for_selector(".toast-error")
    assert "has no 'store' to filter to" in page.locator(".toast-error").first.text_content()
    page.wait_for_timeout(300)
    assert _region(page) == "all"
    assert "f_region" not in page.url


def test_a_picked_value_equal_to_a_default_keeps_the_selection_and_its_chip(page, edges):
    page.goto(f"{edges}/d/xf")
    _wait_tiles(page)
    floor = _xf_tile(page, "floor")
    floor.locator("button.cell-filter").click()
    page.wait_for_function("() => location.search.includes('f_region=eu')")
    chip = floor.locator("button.tile-xf")
    chip.wait_for()
    assert chip.text_content() == "eu · 0"
    floor.locator("td.is-picked").wait_for()
    chip.click()
    page.wait_for_function("() => location.search.includes('f_region=all')")
    assert floor.locator("button.tile-xf").count() == 0


def test_a_big_number_cross_filters_on_click_and_keyboard(page, edges):
    page.goto(f"{edges}/d/xf")
    _wait_tiles(page)
    number = _xf_tile(page, "headline").locator(".big-number")
    assert number.get_attribute("role") == "button"
    number.click()
    page.wait_for_function("() => location.search.includes('f_region=eu')")
    number = _xf_tile(page, "headline").locator(".big-number")
    number.focus()
    page.keyboard.press("Enter")
    page.wait_for_function("() => location.search.includes('f_region=all')")


def test_a_line_selection_fades_the_stroke_and_marks_the_picked_point(page, edges):
    page.goto(f"{edges}/d/xf?f_day=2026-01-10")
    _wait_tiles(page)
    mount = '.tile[data-tile-id="trend"] .chart-mount'
    page.wait_for_function(
        f"() => {{ const m = document.querySelector('{mount}'); "
        "return Boolean(m && echarts.getInstanceByDom(m)); }"
    )
    series = page.evaluate(
        """(mount) => {
          const s = echarts.getInstanceByDom(document.querySelector(mount)).getOption().series[0];
          return {
            line: s.lineStyle?.opacity ?? 1,
            show: s.showSymbol,
            picked: s.data[9].itemStyle?.opacity ?? 1,
            other: s.data[0].itemStyle?.opacity ?? 1,
          };
        }""",
        mount,
    )
    assert series["line"] < 1, series
    assert series["show"] is True, series
    assert series["picked"] == 1, series
    assert series["other"] == 0, series


def test_another_point_on_a_long_selected_line_can_still_be_picked(page, edges):
    page.goto(f"{edges}/d/xf?f_day=2026-01-10")
    _wait_tiles(page)
    mount = '.tile[data-tile-id="trend"] .chart-mount'
    page.locator('.tile[data-tile-id="trend"]').scroll_into_view_if_needed()
    page.wait_for_function(
        f"() => {{ const m = document.querySelector('{mount}'); "
        "return Boolean(m && echarts.getInstanceByDom(m)); }"
    )
    point = page.evaluate(
        """(mount) => {
          const el = document.querySelector(mount);
          const chart = echarts.getInstanceByDom(el);
          const datum = chart.getOption().series[0].data[19];
          const [x, y] = chart.convertToPixel({seriesIndex: 0}, datum.value ?? datum);
          const r = el.getBoundingClientRect();
          return {x: r.x + x, y: r.y + y};
        }""",
        mount,
    )
    page.mouse.move(point["x"], point["y"])
    page.wait_for_timeout(200)
    page.mouse.click(point["x"], point["y"])
    page.wait_for_function("() => location.search.includes('f_day=2026-01-20')")


def test_a_cross_filter_change_elsewhere_keeps_a_failed_tile_showing_its_error(page, edges):
    page.goto(f"{edges}/d/fragile")
    _wait_tiles(page)
    tile = _xf_tile(page, "fragile")
    assert tile.locator("td").nth(1).text_content() == "1"
    page.fill('[data-filter="s"]', "x")
    page.keyboard.press("Enter")
    tile.locator(".tile-status .err").wait_for()
    page.select_option('select[data-filter="region"]', "eu")
    page.wait_for_function("() => location.search.includes('f_region=eu')")
    page.wait_for_timeout(500)
    assert tile.locator(".tile-status .err").count() == 1


def test_a_picked_category_keeps_its_marker_on_a_dense_line(page, edges):
    page.goto(f"{edges}/d/xf?f_cat=category_1")
    _wait_tiles(page)
    mount = '.tile[data-tile-id="categories"] .chart-mount'
    page.wait_for_function(
        f"() => {{ const m = document.querySelector('{mount}'); "
        "return Boolean(m && echarts.getInstanceByDom(m)); }"
    )
    page.wait_for_timeout(300)
    drawn = page.evaluate(
        """(mount) => {
          const chart = echarts.getInstanceByDom(document.querySelector(mount));
          const data = chart.getModel().getSeriesByIndex(0).getData();
          const el = data.getItemGraphicEl(1);
          return Boolean(el) && !el.invisible;
        }""",
        mount,
    )
    assert drawn
