"""Explicit cross-filters: the `cross_filter:` mapping and what lint says about it."""

import pytest

from sqldash.execution import ExecutionRegistry
from sqldash.lint import lint_dashboard, validate_dashboard
from sqldash.project.store import DashboardStore, InvalidDashboardError, parse_dashboard
from sqldash.semantics import SemanticLayer

HEAD = (
    "title: Overview\n"
    "source: {type: duckdb, database: ':memory:'}\n"
    "filters:\n"
    "  - {name: dates, type: daterange, default: last_30_days}\n"
    "  - {name: region, type: select, options: [all, us, eu]}\n"
    "  - {name: minimum, type: number}\n"
)


def _dashboard(cross_filter: str, sql: str = "SELECT 'us' AS region, 10 AS revenue") -> str:
    return (
        HEAD + "tiles:\n"
        "  - title: By region\n"
        "    chart: pie\n"
        f'    sql: "{sql}"\n'
        f"    cross_filter: {cross_filter}\n"
    )


def _findings(text: str, level: str) -> list[str]:
    dashboard = parse_dashboard(text)
    return [f.message for f in lint_dashboard(dashboard, "d.yaml") if f.level == level]


def test_a_mapping_and_false_both_parse():
    assert parse_dashboard(_dashboard("{region: region}")).tiles[0].cross_filter == {
        "region": "region"
    }
    assert parse_dashboard(_dashboard("false")).tiles[0].cross_filter is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("{}", "maps no filters"),
        ("true", "cross_filter"),
        ("{region: ''}", "empty column"),
        ("[region]", "cross_filter"),
    ],
)
def test_a_malformed_cross_filter_is_refused(value, expected):
    with pytest.raises(InvalidDashboardError, match=expected):
        parse_dashboard(_dashboard(value))


def test_a_click_cannot_both_drill_and_cross_filter():
    text = _dashboard("{region: region}") + "    drill: detail\n"
    with pytest.raises(InvalidDashboardError, match="either drills or cross-filters"):
        parse_dashboard(text)


def test_a_text_tile_cannot_cross_filter():
    text = HEAD + "tiles:\n  - {markdown: hi, cross_filter: {region: region}}\n"
    with pytest.raises(InvalidDashboardError, match="needs a chart or table tile"):
        parse_dashboard(text)


def test_lint_passes_a_mapping_onto_a_scalar_filter():
    text = _dashboard("{region: region, minimum: revenue}")
    assert _findings(text, "error") == []
    assert not [w for w in _findings(text, "warning") if "cross_filter" in w]


def test_lint_names_an_unknown_filter_and_a_date_range():
    errors = _findings(_dashboard("{regoin: region, dates: region}"), "error")
    assert any("cross_filter 'regoin' is not a filter" in e and "region" in e for e in errors)
    assert any("cross_filter 'dates' is a date range" in e for e in errors)


def test_lint_warns_when_the_tile_filters_itself():
    sql = "SELECT region, 10 AS revenue FROM t WHERE region = {{ region }}"
    warnings = _findings(_dashboard("{region: region}", sql), "warning")
    assert any("also read by this tile's own query" in w for w in warnings), warnings


def test_validate_dashboard_names_a_cross_filter_column_the_query_does_not_return(tmp_path):
    store = DashboardStore(tmp_path)
    registry = ExecutionRegistry(max_workers=1)
    try:
        payload = validate_dashboard(
            _dashboard("{region: area}"),
            store=store,
            layer=SemanticLayer(store),
            registry=registry,
            name="overview",
        )
    finally:
        registry.shutdown()
    assert payload["valid"] is False
    assert any("cross_filter reads column 'area'" in e for e in payload["errors"]), payload
