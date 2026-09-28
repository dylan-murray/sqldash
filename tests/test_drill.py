"""Drill-down links: the `drill:` contract, how it resolves against the project,
what lint reports about it, and what the dashboard page hands the browser."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import NoSuchModuleError

from sqldash.execution import ExecutionRegistry
from sqldash.lint import lint_project, validate_dashboard
from sqldash.params import option_value
from sqldash.project.drill import plan_drill, plan_drills
from sqldash.project.store import (
    DashboardStore,
    InvalidDashboardError,
    WorkspaceStore,
    parse_dashboard,
)
from sqldash.semantics import SemanticLayer
from sqldash.server import create_app

SOURCE = "source: {type: duckdb, database: ':memory:'}\n"

OVERVIEW = (
    "title: Overview\n" + SOURCE + "filters:\n"
    "  - {name: dates, type: daterange, default: last_30_days}\n"
    "  - {name: region, type: select, options: [all, us, eu]}\n"
    "  - {name: minimum, type: number}\n"
    "tiles:\n"
    "  - title: By customer\n"
    "    chart: bar\n"
    "    sql: \"SELECT 'acme' AS customer, 7 AS customer_id, 10 AS revenue\"\n"
    "    drill:\n"
    "      dashboard: detail\n"
    "      filters:\n"
    "        customer: customer\n"
    "        period: {filter: dates}\n"
)

DETAIL = (
    "title: Customer detail\n" + SOURCE + "filters:\n"
    "  - {name: period, type: daterange, default: last_7_days}\n"
    "  - {name: customer, type: text}\n"
    "  - {name: customer_id, type: number}\n"
    "  - {name: tier, type: select, options: [all, gold, silver]}\n"
    "tiles:\n"
    "  - {title: Orders, sql: 'SELECT 1 AS n'}\n"
)


def _project(tmp_path, **files):
    for name, text in files.items():
        (tmp_path / f"{name}.yaml").write_text(text)
    return DashboardStore(tmp_path)


def _with_drill(drill: str, filters: str = "") -> str:
    return (
        "title: Overview\n" + SOURCE + "filters:\n"
        "  - {name: dates, type: daterange, default: last_30_days}\n"
        "  - {name: region, type: select, options: [all, us, eu]}\n"
        "  - {name: minimum, type: number}\n" + filters + "tiles:\n"
        "  - title: By customer\n"
        "    chart: bar\n"
        "    sql: \"SELECT 'acme' AS customer, 7 AS customer_id, 10 AS revenue\"\n"
        f"    drill: {drill}\n"
    )


def _errors(store) -> list[str]:
    return [f.message for f in lint_project(store, SemanticLayer(store)) if f.level == "error"]


def test_a_bare_string_is_a_link_to_that_dashboard():
    dashboard = parse_dashboard(_with_drill("detail"))
    assert dashboard.tiles[0].drill.dashboard == "detail"
    assert dashboard.tiles[0].drill.filters == {}


@pytest.mark.parametrize("name", ["", "a/b/c", "../detail", "a//b", "./x"])
def test_a_drill_dashboard_is_a_name_not_a_path(name):
    with pytest.raises(InvalidDashboardError, match="drill dashboard"):
        parse_dashboard(_with_drill(f"{{dashboard: '{name}'}}"))


def test_a_text_tile_cannot_drill():
    text = "title: T\n" + SOURCE + "tiles:\n  - {markdown: hi, drill: detail}\n"
    with pytest.raises(InvalidDashboardError, match="'drill' needs a chart or table tile"):
        parse_dashboard(text)


def test_an_unknown_drill_key_is_refused():
    with pytest.raises(InvalidDashboardError, match="target"):
        parse_dashboard(_with_drill("{dashboard: detail, target: _blank}"))


def test_the_plan_names_every_param_the_link_sets(tmp_path):
    store = _project(tmp_path, overview=OVERVIEW, detail=DETAIL)
    dashboard, _, _ = store.load("overview")
    plan = plan_drill(store, "overview", dashboard, dashboard.tiles[0])
    assert plan["errors"] == []
    assert plan["href"] == "/d/detail"
    assert plan["title"] == "Customer detail"
    assert plan["column"] == "customer"
    assert plan["params"] == [
        {"param": "customer", "type": "text", "column": "customer"},
        {"param": "period_start", "type": "date", "current": "dates_start"},
        {"param": "period_end", "type": "date", "current": "dates_end"},
    ]


def test_lint_passes_a_resolvable_drill(tmp_path):
    assert _errors(_project(tmp_path, overview=OVERVIEW, detail=DETAIL)) == []


@pytest.mark.parametrize(
    ("drill", "expected"),
    [
        ("{dashboard: detial, filters: {customer: customer}}", "'detial' does not exist"),
        ("{dashboard: detail, filters: {custmer: customer}}", "'custmer' is not a filter on"),
        ("{dashboard: detail, filters: {period: customer}}", "is a date range"),
        ("{dashboard: detail, filters: {customer: {filter: nope}}}", "not a filter on this"),
        ("{dashboard: detail, filters: {customer: {filter: dates}}}", "only carries into"),
        ("{dashboard: detail, filters: {period: {filter: region}}}", "only carries into"),
        ("{dashboard: detail, filters: {customer_id: {filter: region}}}", "would not fit"),
        ("{dashboard: other/detail, filters: {customer: customer}}", "not a workspace"),
    ],
)
def test_lint_names_each_broken_mapping(tmp_path, drill, expected):
    store = _project(tmp_path, overview=_with_drill(drill), detail=DETAIL)
    errors = _errors(store)
    assert any(expected in e and "tile 'by_customer'" in e for e in errors), errors


def test_a_number_filter_takes_a_number_filter_and_a_select_takes_anything(tmp_path):
    drill = "{dashboard: detail, filters: {customer_id: {filter: minimum}, tier: {filter: region}}}"
    assert _errors(_project(tmp_path, overview=_with_drill(drill), detail=DETAIL)) == []


def test_a_destination_that_does_not_parse_is_an_error_not_a_crash(tmp_path):
    store = _project(tmp_path, overview=OVERVIEW, detail="title: [unclosed\n")
    errors = _errors(store)
    assert any("drill dashboard 'detail' does not load" in e for e in errors), errors


def test_a_drill_that_maps_nothing_warns(tmp_path):
    store = _project(tmp_path, overview=_with_drill("detail"), detail=DETAIL)
    warnings = [
        f.message for f in lint_project(store, SemanticLayer(store)) if f.level == "warning"
    ]
    assert any("maps no filters" in w for w in warnings), warnings


def test_leaving_out_the_dashboard_drills_into_the_same_one(tmp_path):
    drill = "{filters: {region: customer}}"
    store = _project(tmp_path, overview=_with_drill(drill))
    dashboard, _, _ = store.load("overview")
    plan = plan_drill(store, "overview", dashboard, dashboard.tiles[0])
    assert plan["errors"] == []
    assert plan["target"] == "overview"
    assert plan["params"][0]["options"] == ["all", "us", "eu"]


def _point(sales, target):
    (sales / "overview.yaml").write_text(
        OVERVIEW.replace("dashboard: detail", f"dashboard: {target}")
    )


def _workspace(tmp_path):
    sales = tmp_path / "sales"
    finance = tmp_path / "finance"
    sales.mkdir()
    finance.mkdir()
    (sales / "overview.yaml").write_text(OVERVIEW)
    (sales / "detail.yaml").write_text(DETAIL)
    (finance / "detail.yaml").write_text(DETAIL.replace("Customer detail", "Finance detail"))
    return sales, WorkspaceStore(
        {"sales": DashboardStore(sales), "finance": DashboardStore(finance)}
    )


def test_in_a_workspace_a_bare_name_stays_in_its_repo_and_repo_name_crosses(tmp_path):
    sales, store = _workspace(tmp_path)
    for target, href, title in [
        ("detail", "/d/sales/detail", "Customer detail"),
        ("finance/detail", "/d/finance/detail", "Finance detail"),
    ]:
        _point(sales, target)
        dashboard, _, _ = store.load("sales/overview")
        plan = plan_drills(store, "sales/overview", dashboard)["by_customer"]
        assert (plan["href"], plan["title"], plan["errors"]) == (href, title, [])


def test_workspace_lint_resolves_other_repos(tmp_path):
    sales, store = _workspace(tmp_path)
    _point(sales, "finance/detail")
    repo_store = store.repos["sales"]
    layer = SemanticLayer(repo_store)
    alone = [f.message for f in lint_project(repo_store, layer) if f.level == "error"]
    assert any("names a repo" in e for e in alone), alone
    together = lint_project(repo_store, layer, workspace=store, repo="sales")
    assert [f for f in together if f.level == "error"] == []
    _point(sales, "finance/nope")
    errors = [f.message for f in lint_project(repo_store, layer, workspace=store, repo="sales")]
    assert any("'finance/nope' does not exist" in e for e in errors), errors


def test_validate_dashboard_reports_a_drill_column_the_query_does_not_return(tmp_path):
    store = _project(tmp_path, detail=DETAIL)
    text = _with_drill("{dashboard: detail, column: name, filters: {customer: client}}")
    registry = ExecutionRegistry(max_workers=1)
    try:
        payload = validate_dashboard(
            text, store=store, layer=SemanticLayer(store), registry=registry, name="overview"
        )
    finally:
        registry.shutdown()
    assert payload["valid"] is False
    missing = [e for e in payload["errors"] if "drill reads column" in e]
    assert len(missing) == 2, payload["errors"]
    assert "'client'" in missing[0]
    assert "customer, customer_id, revenue" in missing[0]


def test_validate_dashboard_checks_a_self_drill_against_the_candidate(tmp_path):
    store = _project(tmp_path)
    text = _with_drill("{filters: {tier: customer}}")
    payload = validate_dashboard(
        text, store=store, layer=SemanticLayer(store), registry=None, check_sql=False
    )
    assert any("'tier' is not a filter on 'this dashboard'" in e for e in payload["errors"])


@pytest.fixture
def client(tmp_path):
    _project(tmp_path, overview=OVERVIEW, detail=DETAIL)
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as c:
        c.headers["X-Sqldash-Token"] = app.state.api_token
        yield c


def test_the_page_and_api_carry_the_plan(client):
    page = client.get("/d/overview")
    assert page.status_code == 200
    assert '"drills": {"by_customer"' in page.text
    api = client.get("/api/dashboards/overview").json()
    assert api["drills"]["by_customer"]["href"] == "/d/detail"
    assert "drill" not in api["drills"]["by_customer"]


def test_the_breadcrumb_only_names_a_dashboard_the_store_serves(client):
    page = client.get("/d/detail?from=overview")
    assert 'id="drill-back" href="/d/overview"' in page.text
    assert "Back to Overview" in page.text
    for origin in ["nope", "javascript:alert(1)", "<script>x</script>", "detail", "../overview"]:
        page = client.get("/d/detail", params={"from": origin})
        assert page.status_code == 200
        assert 'id="drill-back"' not in page.text, origin


def test_editing_a_drill_tile_in_place_keeps_its_drill_block(tmp_path):
    store = _project(tmp_path, overview=OVERVIEW, detail=DETAIL)
    etag = store.load("overview")[2]
    store.upsert_tile(
        "overview",
        {
            "id": "by_customer",
            "title": "By customer",
            "query": "by_customer",
            "chart": {"type": "bar"},
        },
        "SELECT 'acme' AS customer, 7 AS customer_id, 20 AS revenue",
        etag,
    )
    text = (tmp_path / "overview.yaml").read_text()
    assert "20 AS revenue" in text
    assert "    drill:\n      dashboard: detail\n" in text
    assert store.load("overview")[0].tiles[0].drill.filters["customer"] == "customer"


def test_a_select_without_a_default_offers_all_to_the_link_as_its_filter_bar_does(tmp_path):
    detail = DETAIL.replace("options: [all, gold, silver]", "options: [gold, silver]")
    store = _project(
        tmp_path,
        overview=_with_drill("{dashboard: detail, filters: {tier: customer}}"),
        detail=detail,
    )
    dashboard, _, _ = store.load("overview")
    plan = plan_drill(store, "overview", dashboard, dashboard.tiles[0])
    assert plan["params"][0]["options"] == ["all", "gold", "silver"]


def test_a_carried_filter_value_is_checked_against_the_destination_options(tmp_path):
    detail = DETAIL.replace("options: [all, gold, silver]", "options: [all, eu, apac]")
    drill = "{dashboard: detail, filters: {tier: {filter: region}}}"
    store = _project(tmp_path, overview=_with_drill(drill), detail=detail)
    dashboard, _, _ = store.load("overview")
    plan = plan_drill(store, "overview", dashboard, dashboard.tiles[0])
    assert plan["params"] == [
        {
            "param": "tier",
            "type": "select",
            "current": "region",
            "kind": "string",
            "options": ["all", "eu", "apac"],
            "option_kinds": ["string", "string", "string"],
        }
    ]


def test_validate_dashboard_reports_a_drill_column_a_metric_tile_does_not_return(tmp_path):
    (tmp_path / "metrics.yaml").write_text(
        "source: {type: duckdb, database: ':memory:'}\n"
        "relations:\n"
        "  orders: {sql: \"SELECT 1 AS amount, 'us' AS region\"}\n"
        "metrics:\n"
        "  revenue: {relation: orders, expr: SUM(amount), dimensions: [{name: region}]}\n"
    )
    store = _project(tmp_path, detail=DETAIL)
    text = (
        "title: Overview\n" + SOURCE + "tiles:\n"
        "  - title: By region\n"
        "    chart: bar\n"
        "    metric: {name: revenue, dimensions: [region]}\n"
        "    drill: {dashboard: detail, filters: {customer: nonexistent}}\n"
        "  - title: By region again\n"
        "    chart: bar\n"
        "    metric: {name: revenue, dimensions: [region]}\n"
        "    drill: {dashboard: detail, filters: {customer: region}}\n"
    )
    registry = ExecutionRegistry(max_workers=1)
    try:
        payload = validate_dashboard(
            text, store=store, layer=SemanticLayer(store), registry=registry, name="overview"
        )
    finally:
        registry.shutdown()
    missing = [e for e in payload["errors"] if "drill reads column" in e]
    assert len(missing) == 1, payload["errors"]
    assert "tile 'by_region'" in missing[0]
    assert "'nonexistent'" in missing[0]
    assert "region, revenue" in missing[0]


def test_validate_dashboard_reports_a_drill_column_an_inline_metric_tile_does_not_return(tmp_path):
    store = _project(tmp_path, detail=DETAIL)
    text = (
        "title: Overview\n" + SOURCE + "relations:\n"
        "  orders: {sql: \"SELECT 1 AS amount, 'us' AS region\"}\n"
        "metrics:\n"
        "  revenue: {relation: orders, expr: SUM(amount), dimensions: [{name: region}]}\n"
        "tiles:\n"
        "  - title: By region\n"
        "    chart: bar\n"
        "    metric: {name: revenue, dimensions: [region]}\n"
        "    drill: {dashboard: detail, filters: {customer: nonexistent}}\n"
    )
    registry = ExecutionRegistry(max_workers=1)
    try:
        payload = validate_dashboard(
            text, store=store, layer=SemanticLayer(store), registry=registry, name="overview"
        )
    finally:
        registry.shutdown()
    missing = [e for e in payload["errors"] if "drill reads column" in e]
    assert len(missing) == 1, payload
    assert "'nonexistent'" in missing[0]
    assert "region, revenue" in missing[0]


def _tile_write(client, method, path, tile):
    etag = client.get("/api/dashboards/overview").json()["etag"]
    res = client.request(method, path, json={"tile": tile}, headers={"If-Match": etag})
    assert res.status_code == 200, res.text
    return client.get("/api/dashboards/overview").json()["dashboard"]["tiles"]


def test_the_tile_api_writes_a_new_drill_and_replaces_or_removes_one(client, tmp_path):
    tiles = _tile_write(
        client,
        "POST",
        "/api/dashboards/overview/tiles",
        {
            "title": "Linked",
            "chart": "bar",
            "query": "by_customer",
            "drill": {"dashboard": "detail", "filters": {"customer": "customer"}},
        },
    )
    linked = next(t for t in tiles if t["id"] == "linked")
    assert linked["drill"]["filters"] == {"customer": "customer"}
    tiles = _tile_write(
        client,
        "PUT",
        "/api/dashboards/overview/tiles/linked",
        {
            "id": "linked",
            "title": "Linked",
            "chart": "bar",
            "query": "by_customer",
            "drill": "overview",
        },
    )
    assert next(t for t in tiles if t["id"] == "linked")["drill"]["dashboard"] == "overview"
    tiles = _tile_write(
        client,
        "PUT",
        "/api/dashboards/overview/tiles/linked",
        {"id": "linked", "title": "Linked", "chart": "bar", "query": "by_customer", "drill": None},
    )
    assert next(t for t in tiles if t["id"] == "linked")["drill"] is None
    text = (tmp_path / "overview.yaml").read_text()
    assert "    drill:\n      dashboard: detail\n" in text


OLD_DRILLS = {
    "block": "    drill:\n      dashboard: detail\n      filters:\n        customer: customer\n",
    "block_flow_leaf": (
        "    drill:\n      dashboard: detail\n      filters:\n        period: {filter: dates}\n"
    ),
    "flow": "    drill: {dashboard: detail, filters: {customer: customer}}\n",
    "scalar": "    drill: detail\n",
}
NEW_DRILLS = {
    "block": (
        {"dashboard": "detail", "filters": {"customer_id": "customer_id"}},
        "    drill:\n      dashboard: detail\n      filters:\n        customer_id: customer_id\n",
    ),
    "flow_leaf": (
        {"dashboard": "detail", "filters": {"period": {"filter": "dates"}}},
        "    drill:\n      dashboard: detail\n      filters:\n        period: {filter: dates}\n",
    ),
    "scalar": ("overview", "    drill: overview\n"),
    "removed": (None, ""),
}


def _drill_file(drill: str, where: str) -> str:
    sql = "    sql: \"SELECT 'acme' AS customer, 7 AS customer_id, 10 AS revenue\"\n"
    note = "\n    # This note describes the chart\n"
    if where == "middle":
        body = "  - title: By customer\n" + drill + note + "    chart: bar\n" + sql
    else:
        body = "  - title: By customer\n    chart: bar\n" + sql + drill
    head = OVERVIEW[: OVERVIEW.index("tiles:\n") + len("tiles:\n")]
    return (
        head
        + body
        + "\n  # This note describes the next tile\n  - {title: Next, sql: 'SELECT 1 AS n'}\n"
    )


@pytest.mark.parametrize("where", ["middle", "last"])
@pytest.mark.parametrize("new", list(NEW_DRILLS))
@pytest.mark.parametrize("old", list(OLD_DRILLS))
def test_rewriting_a_drill_changes_only_the_drill_lines(tmp_path, old, new, where):
    before = _drill_file(OLD_DRILLS[old], where)
    value, lines = NEW_DRILLS[new]
    store = _project(tmp_path, overview=before, detail=DETAIL)
    tile = {"id": "by_customer", "title": "By customer", "query": "by_customer", "chart": "bar"}
    store.upsert_tile("overview", {**tile, "drill": value}, None, store.load("overview")[2])
    assert (tmp_path / "overview.yaml").read_text() == _drill_file(lines, where)


def test_reordering_a_drills_filters_is_saved_since_the_first_one_holds_the_link(tmp_path):
    store = _project(tmp_path, overview=OVERVIEW, detail=DETAIL)
    tile = {"id": "by_customer", "title": "By customer", "query": "by_customer", "chart": "bar"}
    drill = {
        "dashboard": "detail",
        "filters": {"period": {"filter": "dates"}, "customer": "customer"},
    }
    store.upsert_tile("overview", {**tile, "drill": drill}, None, store.load("overview")[2])
    saved = store.load("overview")[0].tiles[0].drill
    assert list(saved.filters) == ["period", "customer"]


def test_an_inline_metric_tile_on_a_missing_dialect_still_gets_a_report(tmp_path, monkeypatch):
    def missing(url, *args, **kwargs):
        raise NoSuchModuleError(f"Can't load plugin: sqlalchemy.dialects:{str(url).split(':')[0]}")

    monkeypatch.setattr("sqldash.lint.create_engine", missing)
    monkeypatch.setattr("sqldash.connectors.engine.create_engine", missing)
    store = _project(tmp_path)
    text = (
        "title: Lake\nsource: {type: trino, host: h, database: hive}\n"
        "relations:\n  orders: {table: orders}\n"
        "metrics:\n  revenue: {relation: orders, expr: SUM(amount)}\n"
        "tiles:\n  - {title: Revenue, metric: revenue}\n"
    )
    registry = ExecutionRegistry(max_workers=1)
    try:
        payload = validate_dashboard(
            text, store=store, layer=SemanticLayer(store), registry=registry, name="lake"
        )
    finally:
        registry.shutdown()
    assert payload["sql_checked"] is True, payload
    assert any("dialect package installed" in w for w in payload["lint"]), payload


@pytest.mark.parametrize(
    ("options", "expected", "kind"),
    [
        ("[true, false]", ["all", "true", "false"], "boolean"),
        ("[1.0, 2.0]", ["all", "1", "2"], "number"),
        ("[1e-06, 0.5]", ["all", "0.000001", "0.5"], "number"),
        ("['100', '00100']", ["all", "100", "00100"], "string"),
    ],
)
def test_plan_options_are_spelled_the_way_the_browser_spells_a_cell(
    tmp_path, options, expected, kind
):
    detail = DETAIL.replace("options: [all, gold, silver]", f"options: {options}")
    store = _project(
        tmp_path,
        overview=_with_drill("{dashboard: detail, filters: {tier: customer}}"),
        detail=detail,
    )
    dashboard, _, _ = store.load("overview")
    plan = plan_drill(store, "overview", dashboard, dashboard.tiles[0])
    assert plan["params"][0]["options"] == expected
    assert plan["params"][0]["option_kinds"] == ["string", *[kind] * (len(expected) - 1)]


def test_the_filter_bar_values_use_that_spelling_and_keep_their_labels(tmp_path):
    detail = DETAIL.replace(
        "  - {name: tier, type: select, options: [all, gold, silver]}\n",
        "  - {name: tier, type: select, options: [true, false], default: true}\n"
        "  - {name: rate, type: select, options: [1.0, 1e-06]}\n",
    )
    _project(tmp_path, overview=OVERVIEW, detail=detail)
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as client:
        page = client.get("/d/detail").text
    assert '<option value="true" selected data-kind="boolean">True</option>' in page
    assert '<option value="false" data-kind="boolean">False</option>' in page
    assert '<option value="1" data-kind="number">1.0</option>' in page
    assert '<option value="0.000001" data-kind="number">1e-06</option>' in page


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1.0, "1"),
        (1e-06, "0.000001"),
        (1e-07, "1e-7"),
        (1.5e21, "1.5e+21"),
        (1e21, "1e+21"),
        (1e20, "100000000000000000000"),
        (0.1 + 0.2, "0.30000000000000004"),
        (-0.000001234, "-0.000001234"),
        (5e-324, "5e-324"),
        (True, "true"),
        (7, "7"),
        ("True", "True"),
    ],
)
def test_option_value_matches_javascript_string(value, expected):
    assert option_value(value) == expected


def test_turning_a_drill_tile_into_text_drops_its_drill(tmp_path):
    store = _project(tmp_path, overview=OVERVIEW, detail=DETAIL)
    tile = {"id": "by_customer", "type": "text", "title": "By customer", "markdown": "Notes"}
    store.upsert_tile("overview", tile, None, store.load("overview")[2])
    saved = store.load("overview")[0].tiles[0]
    assert saved.type == "text"
    assert saved.drill is None
