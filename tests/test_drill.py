"""Drill-down links: the `drill:` contract, how it resolves against the project,
what lint reports about it, and what the dashboard page hands the browser."""

import pytest
from fastapi.testclient import TestClient

from sqldash.execution import ExecutionRegistry
from sqldash.lint import lint_project, validate_dashboard
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
