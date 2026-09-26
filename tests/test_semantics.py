import os
import sqlite3
from datetime import date, timedelta

import duckdb
import pytest
from pydantic import ValidationError

from sqldash.connectors.engine import paramstyle_for
from sqldash.execution import ExecutionRegistry
from sqldash.lint import lint_project, validate_metrics
from sqldash.models.semantics import MetricDef
from sqldash.models.source import Source
from sqldash.params import ParamError
from sqldash.project.catalog import metric_detail
from sqldash.project.store import DashboardStore
from sqldash.scaffold import create_demo
from sqldash.semantics.bind import bind_named_query
from sqldash.semantics.compiler import MetricQuery, compile_metric
from sqldash.semantics.layer import (
    MetricNotFoundError,
    SemanticError,
    SemanticLayer,
    parse_metrics_file,
)
from sqldash.sqlguard import read_only_violation

METRICS_YAML = """
source: {type: duckdb, database: ':memory:', attach_files: true}
relations:
  orders: {table: orders}
metrics:
  revenue:
    title: Revenue
    description: Total order revenue
    relation: orders
    expr: SUM(amount)
    format: currency
    synonyms: [sales]
    time_dimension: {name: order_date, grain: day}
    dimensions:
      - {name: region, description: Sales region}
      - {name: category}
  weird_revenue:
    table: orders
    expr: SUM(amount)
    filters: ["amount > 0"]
    dimensions: [{name: region}]
"""

DASHBOARD_YAML = """
title: Inline
source: {type: duckdb, database: ':memory:', attach_files: true}
metrics:
  revenue:
    table: orders
    expr: SUM(amount * 2)
    dimensions: [{name: region}]
  dashboard_only:
    table: orders
    expr: COUNT(*)
queries: {q: 'SELECT 1 AS n'}
tiles:
  - {id: w1, metric: revenue}
  - {id: w2, query: q}
"""


@pytest.fixture
def layer(tmp_path):
    (tmp_path / "metrics.yaml").write_text(METRICS_YAML)
    (tmp_path / "dash.yaml").write_text(DASHBOARD_YAML)
    (tmp_path / "orders.csv").write_text(
        "order_date,region,category,amount\n"
        "2026-01-01,us,tools,10.0\n"
        "2026-01-01,eu,tools,20.0\n"
        "2026-01-02,us,toys,30.0\n"
    )
    return SemanticLayer(DashboardStore(tmp_path))


def rev(layer):
    return layer.resolve("revenue")


def test_metrics_yaml_not_a_dashboard(layer):
    assert list(layer.store.discover()) == ["dash"]


def test_parse_rejects_bad_names():
    with pytest.raises(SemanticError, match="must match"):
        parse_metrics_file(
            "source: {type: duckdb}\nmetrics:\n  'bad name': {table: t, expr: COUNT(*)}\n"
        )


def test_parse_rejects_multiple_bases():
    with pytest.raises(SemanticError, match="exactly one"):
        parse_metrics_file(
            "source: {type: duckdb}\nmetrics:\n  m: {table: t, sql: SELECT 1, expr: COUNT(*)}\n"
        )


def test_project_metrics_resolve(layer):
    resolved = rev(layer)
    assert resolved.origin == "project"
    assert resolved.relation.table == "orders"


def test_inline_overrides_project_within_dashboard(layer):
    resolved = layer.resolve("revenue", dashboard="dash")
    assert resolved.origin == "dashboard"
    assert resolved.definition.expr == "SUM(amount * 2)"


def test_inline_visible_globally_when_not_colliding(layer):
    names = {m.name: m.origin for m in layer.all_metrics()}
    assert names["revenue"] == "project"
    assert names["dashboard_only"] == "dashboard"


def test_unknown_metric_lists_available(layer):
    with pytest.raises(MetricNotFoundError, match="revenue"):
        layer.resolve("nope")


def test_compile_full_query(layer):
    sql, bind = compile_metric(
        rev(layer),
        MetricQuery(dimensions=("region",), grain="day", filters={"region": "us"}, limit=1000),
        "qmark",
    )
    assert sql.splitlines() == [
        'SELECT DATE_TRUNC(\'day\', order_date) AS "order_date", region AS "region", '
        'SUM(amount) AS "revenue"',
        "FROM orders",
        "WHERE region = ?",
        "GROUP BY 1, 2",
        "ORDER BY 1, 2",
        "LIMIT 1000",
    ]
    assert bind == ["us"]


def test_compile_pyformat_and_in_list(layer):
    sql, bind = compile_metric(
        rev(layer),
        MetricQuery(filters={"region": ["us", "eu"]}),
        "pyformat",
    )
    assert "region IN (%s, %s)" in sql
    assert bind == ["us", "eu"]


def test_compile_time_range_without_grain(layer):
    sql, bind = compile_metric(
        rev(layer),
        MetricQuery(time_range=("2026-01-01", "2026-01-31")),
        "qmark",
    )
    assert "DATE_TRUNC" not in sql
    assert "order_date >= ?" in sql
    assert "order_date < ?" in sql
    assert bind == ["2026-01-01", "2026-02-01"]


def test_compile_default_filters_and_ops(layer):
    sql, bind = compile_metric(
        layer.resolve("weird_revenue"),
        MetricQuery(filters={"region": {"op": "!=", "value": "apac"}}),
        "qmark",
    )
    assert "(amount > 0)" in sql
    assert "region != ?" in sql
    assert bind == ["apac"]


def test_injection_via_dimension_name_rejected(layer):
    with pytest.raises(SemanticError, match="valid dimensions"):
        compile_metric(rev(layer), MetricQuery(dimensions=("region; DROP TABLE x",)), "qmark")


def test_injection_via_filter_name_rejected(layer):
    with pytest.raises(SemanticError, match="valid dimensions"):
        compile_metric(rev(layer), MetricQuery(filters={"1=1 OR region": "x"}), "qmark")


def test_injection_via_op_rejected(layer):
    with pytest.raises(SemanticError, match="valid ops"):
        compile_metric(
            rev(layer),
            MetricQuery(filters={"region": {"op": "= '' OR 1=1 --", "value": "x"}}),
            "qmark",
        )


def test_injection_via_limit_rejected(layer):
    with pytest.raises(SemanticError, match="integer"):
        compile_metric(rev(layer), MetricQuery(limit="10; DELETE FROM x"), "qmark")


LIMIT_FORMS = {
    **dict.fromkeys(
        (
            "duckdb",
            "sqlite",
            "postgres",
            "mysql",
            "mariadb",
            "redshift",
            "snowflake",
            "bigquery",
            "databricks",
            "athena",
            "awsathena",
            "trino",
            "clickhouse",
        ),
        "LIMIT 11",
    ),
    "oracle": "FETCH FIRST 11 ROWS ONLY",
    "mssql": None,
}


@pytest.mark.parametrize(("dialect", "form"), LIMIT_FORMS.items())
@pytest.mark.parametrize("dimensions", [(), ("region",)])
def test_limit_compiles_in_each_dialects_own_form(layer, dialect, form, dimensions):
    sql, _ = compile_metric(
        rev(layer), MetricQuery(dimensions=dimensions, limit=11), "qmark", dialect=dialect
    )
    last = sql.splitlines()[-1]
    if form is None:
        assert not any(word in sql for word in ("LIMIT", "FETCH", "TOP"))
        assert last in ("FROM orders", "ORDER BY 1")
    else:
        assert last == form
        assert sql.count("LIMIT") + sql.count("FETCH FIRST") == 1


def test_limit_still_validated_where_no_clause_compiles(layer):
    with pytest.raises(SemanticError, match="integer"):
        compile_metric(rev(layer), MetricQuery(limit="10; DELETE FROM x"), "qmark", dialect="mssql")


def test_the_fetch_cap_limits_a_dialect_that_compiles_no_limit(layer, tmp_path):
    sql, bind = compile_metric(
        rev(layer),
        MetricQuery(dimensions=("region",), grain="day", limit=3),
        "qmark",
        dialect="mssql",
    )
    assert "LIMIT" not in sql
    registry = ExecutionRegistry(max_workers=1)
    try:
        result = registry.run_sync(Source(type="duckdb", attach_files=True), tmp_path, sql, bind, 2)
    finally:
        registry.shutdown()
    assert len(result.rows) == 2
    assert result.truncated


def test_grain_requires_time_dimension(layer):
    with pytest.raises(SemanticError, match="time_dimension"):
        compile_metric(layer.resolve("weird_revenue"), MetricQuery(grain="day"), "qmark")


def test_compile_and_execute_against_duckdb(layer, tmp_path):
    resolved = rev(layer)
    sql, bind = compile_metric(
        resolved,
        MetricQuery(dimensions=("region",), filters={"category": "tools"}),
        "qmark",
    )
    registry = ExecutionRegistry(max_workers=1)
    try:
        result = registry.run_sync(
            Source(type="duckdb", attach_files=True), tmp_path, sql, bind, 100
        )
    finally:
        registry.shutdown()
    rows = {row[0]: float(row[1]) for row in result.rows}
    assert rows == {"us": 10.0, "eu": 20.0}


def test_derived_metric_expands_and_runs(tmp_path):
    create_demo(tmp_path)
    layer = SemanticLayer(DashboardStore(tmp_path / ".sqldash"))
    resolved = layer.resolve("avg_order_value")
    assert "(SUM(amount))" in resolved.definition.expr
    assert "(COUNT(*))" in resolved.definition.expr
    sql, bind = compile_metric(resolved, MetricQuery(dimensions=("region",)), "qmark")
    registry = ExecutionRegistry(max_workers=1)
    try:
        result = registry.run_sync(resolved.source, resolved.base_dir, sql, bind, 100)
        assert len(result.rows) == 3
        assert all(row[1] is not None for row in result.rows)
    finally:
        registry.shutdown()


def test_derived_unknown_ref():
    from sqldash.models.semantics import MetricDef
    from sqldash.semantics.layer import expand_derived

    peers = {"revenue": MetricDef(table="orders", expr="SUM(amount)")}
    bad = MetricDef(derived="{revenue} / {nope}")
    with pytest.raises(SemanticError, match="unknown metric '\\{nope\\}'"):
        expand_derived("ratio", bad, {**peers, "ratio": bad}, {})


def test_derived_of_derived_rejected():
    from sqldash.models.semantics import MetricDef
    from sqldash.semantics.layer import expand_derived

    base = MetricDef(table="orders", expr="SUM(amount)")
    d1 = MetricDef(derived="{revenue} * 2")
    d2 = MetricDef(derived="{d1} * 3")
    peers = {"revenue": base, "d1": d1, "d2": d2}
    with pytest.raises(SemanticError, match="may only reference plain metrics"):
        expand_derived("d2", d2, peers, {})


def test_derived_mixed_relations_rejected():
    from sqldash.models.semantics import MetricDef
    from sqldash.semantics.layer import expand_derived

    a = MetricDef(table="orders", expr="SUM(amount)")
    b = MetricDef(table="refunds", expr="COUNT(*)")
    d = MetricDef(derived="{a} / {b}")
    with pytest.raises(SemanticError, match="different relations"):
        expand_derived("d", d, {"a": a, "b": b, "d": d}, {})


def test_derived_requires_no_relation():
    from sqldash.models.semantics import MetricDef

    with pytest.raises(ValidationError):
        MetricDef(table="orders", derived="{a} / {b}")
    with pytest.raises(ValidationError):
        MetricDef(table="orders")


CUMULATIVE_YAML = """
source: {type: duckdb, database: ':memory:'}
relations:
  events:
    sql: "SELECT DATE '2026-01-01' + CAST(i AS INT) AS day,
      ['us','eu'][1 + i % 2] AS region, (i + 1) * 10 AS amount FROM range(6) t(i)"
metrics:
  running_revenue:
    relation: events
    expr: SUM(amount)
    cumulative: true
    time_dimension: {name: day, grain: day}
    dimensions: [{name: region}]
"""


def _cumulative_layer(tmp_path):
    (tmp_path / "metrics.yaml").write_text(CUMULATIVE_YAML)
    (tmp_path / "empty.yaml").write_text(
        "title: E\nsource: {type: duckdb, database: ':memory:'}\ntiles: []\n"
    )
    return SemanticLayer(DashboardStore(tmp_path))


def test_cumulative_compiles_to_window(tmp_path):
    layer = _cumulative_layer(tmp_path)
    resolved = layer.resolve("running_revenue")
    sql, _ = compile_metric(resolved, MetricQuery(grain="day"), "qmark")
    assert "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW" in sql
    assert "sqldash_buckets" in sql


def test_cumulative_runs_and_accumulates(tmp_path):
    layer = _cumulative_layer(tmp_path)
    resolved = layer.resolve("running_revenue")
    sql, bind = compile_metric(resolved, MetricQuery(grain="day"), "qmark")
    registry = ExecutionRegistry(max_workers=1)
    try:
        result = registry.run_sync(resolved.source, resolved.base_dir, sql, bind, 100)
        values = [row[1] for row in result.rows]
        assert values == [10, 30, 60, 100, 150, 210]

        sql, bind = compile_metric(
            resolved, MetricQuery(grain="day", dimensions=("region",)), "qmark"
        )
        result = registry.run_sync(resolved.source, resolved.base_dir, sql, bind, 100)
        by_region = {}
        for _day, region, value in result.rows:
            by_region.setdefault(region, []).append(value)
        assert by_region["us"] == [10, 40, 90]
        assert by_region["eu"] == [20, 60, 120]
    finally:
        registry.shutdown()


def test_cumulative_without_grain_is_plain_total(tmp_path):
    layer = _cumulative_layer(tmp_path)
    resolved = layer.resolve("running_revenue")
    sql, bind = compile_metric(resolved, MetricQuery(), "qmark")
    assert "OVER" not in sql
    registry = ExecutionRegistry(max_workers=1)
    try:
        result = registry.run_sync(resolved.source, resolved.base_dir, sql, bind, 100)
        assert result.rows == [[210]]
    finally:
        registry.shutdown()


def test_cumulative_requires_time_dimension():
    from sqldash.models.semantics import MetricDef

    with pytest.raises(ValidationError, match="need a time_dimension"):
        MetricDef(table="orders", expr="SUM(amount)", cumulative=True)


TRAILING_YAML = """
source: {type: duckdb, database: ':memory:'}
relations:
  sparse:
    sql: "SELECT * FROM (VALUES
      (DATE '2026-01-01', 'us', 10),
      (DATE '2026-01-01', 'eu', 5),
      (DATE '2026-01-05', 'us', 20)
    ) t(day, region, amount)"
metrics:
  trailing_3d:
    relation: sparse
    expr: SUM(amount)
    window: 3 days
    time_dimension: {name: day, grain: day}
    dimensions: [{name: region}]
  trailing_5d:
    relation: sparse
    expr: SUM(amount)
    window: 5 days
    time_dimension: {name: day, grain: day}
    dimensions: [{name: region}]
"""


def _trailing_layer(tmp_path):
    (tmp_path / "metrics.yaml").write_text(TRAILING_YAML)
    (tmp_path / "empty.yaml").write_text(
        "title: E\nsource: {type: duckdb, database: ':memory:'}\ntiles: []\n"
    )
    return SemanticLayer(DashboardStore(tmp_path))


def test_parse_window_accepts_count_and_unit():
    from sqldash.models.semantics import parse_window

    assert parse_window("28 days") == (28, "day")
    assert parse_window("4w") == (4, "week")
    assert parse_window("12 hours") == (12, "hour")
    assert parse_window("3mo") == (3, "month")


def test_window_rejects_a_bare_number():
    from sqldash.models.semantics import MetricDef

    with pytest.raises(ValidationError, match="28 days"):
        MetricDef(table="orders", expr="SUM(amount)", window=28, time_dimension={"name": "d"})


def test_window_cannot_combine_with_cumulative():
    from sqldash.models.semantics import MetricDef

    with pytest.raises(ValidationError, match="cannot be combined"):
        MetricDef(
            table="orders",
            expr="SUM(amount)",
            cumulative=True,
            window="28 days",
            time_dimension={"name": "d"},
        )


def test_window_requires_time_dimension():
    from sqldash.models.semantics import MetricDef

    with pytest.raises(ValidationError, match="need a time_dimension"):
        MetricDef(table="orders", expr="SUM(amount)", window="28 days")


def test_trailing_window_fills_missing_days(tmp_path):
    """ROWS without a spine would count Jan 1 as still inside a 3-day frame on Jan 5."""
    layer = _trailing_layer(tmp_path)
    resolved = layer.resolve("trailing_3d")
    sql, bind = compile_metric(resolved, MetricQuery(grain="day"), "qmark")
    assert "generate_series" in sql
    assert "ROWS BETWEEN 2 PRECEDING AND CURRENT ROW" in sql
    assert "sqldash_spine" in sql
    registry = ExecutionRegistry(max_workers=1)
    try:
        result = registry.run_sync(resolved.source, resolved.base_dir, sql, bind, 100)
        by_day = {str(day)[:10]: value for day, value in result.rows}
        assert by_day["2026-01-01"] == 15
        assert by_day["2026-01-04"] == 0
        assert by_day["2026-01-05"] == 20
    finally:
        registry.shutdown()


def test_trailing_window_without_grain_is_the_last_n_days(tmp_path):
    layer = _trailing_layer(tmp_path)
    resolved = layer.resolve("trailing_3d")
    sql, bind = compile_metric(resolved, MetricQuery(), "qmark")
    assert "OVER" not in sql
    assert "generate_series" not in sql
    registry = ExecutionRegistry(max_workers=1)
    try:
        result = registry.run_sync(resolved.source, resolved.base_dir, sql, bind, 100)
        assert result.rows == [[20]]
    finally:
        registry.shutdown()


def test_trailing_window_mid_unit_start_still_has_a_full_first_bucket(tmp_path):
    """A mid-month start used to cut February in half. March must still be Feb+Mar."""
    (tmp_path / "metrics.yaml").write_text(
        "source: {type: duckdb, database: ':memory:'}\n"
        "relations:\n"
        "  ev:\n"
        '    sql: "SELECT * FROM (VALUES\n'
        "      (DATE '2026-01-15', 10),\n"
        "      (DATE '2026-02-10', 5),\n"
        "      (DATE '2026-02-20', 7),\n"
        "      (DATE '2026-03-05', 20)\n"
        '    ) t(day, amount)"\n'
        "metrics:\n"
        "  win2mo:\n"
        "    relation: ev\n"
        "    expr: SUM(amount)\n"
        "    window: 2 months\n"
        "    time_dimension: {name: day, grain: month}\n"
    )
    (tmp_path / "empty.yaml").write_text(
        "title: E\nsource: {type: duckdb, database: ':memory:'}\ntiles: []\n"
    )
    layer = SemanticLayer(DashboardStore(tmp_path))
    resolved = layer.resolve("win2mo")
    registry = ExecutionRegistry(max_workers=1)
    try:
        full_sql, full_bind = compile_metric(resolved, MetricQuery(grain="month"), "qmark")
        full = registry.run_sync(resolved.source, resolved.base_dir, full_sql, full_bind, 100)
        by_month = {str(day)[:7]: value for day, value in full.rows}
        assert by_month["2026-03"] == 32
        mid_sql, mid_bind = compile_metric(
            resolved, MetricQuery(grain="month", time_range=("2026-03-15", None)), "qmark"
        )
        mid = registry.run_sync(resolved.source, resolved.base_dir, mid_sql, mid_bind, 100)
        assert [str(day)[:7] for day, _ in mid.rows] == ["2026-03"]
        assert mid.rows[0][1] == 32
    finally:
        registry.shutdown()


def test_trailing_window_start_still_looks_back(tmp_path):
    """Jan 5's 5-day frame includes Jan 1. Without lookback the answer is 20."""
    layer = _trailing_layer(tmp_path)
    resolved = layer.resolve("trailing_5d")
    sql, bind = compile_metric(
        resolved, MetricQuery(grain="day", time_range=("2026-01-05", "2026-01-05")), "qmark"
    )
    registry = ExecutionRegistry(max_workers=1)
    try:
        result = registry.run_sync(resolved.source, resolved.base_dir, sql, bind, 100)
        assert [str(day)[:10] for day, _ in result.rows] == ["2026-01-05"]
        assert result.rows[0][1] == 35
    finally:
        registry.shutdown()


def test_trailing_window_dimension_filter_binds_once(tmp_path):
    layer = _trailing_layer(tmp_path)
    resolved = layer.resolve("trailing_3d")
    cases = (
        MetricQuery(grain="day", filters={"region": "us"}),
        MetricQuery(grain="day", time_range=("2026-01-05", None)),
        MetricQuery(grain="day", time_range=(None, "2026-01-05")),
        MetricQuery(
            grain="day",
            filters={"region": "us"},
            time_range=("2026-01-01", "2026-01-05"),
        ),
        MetricQuery(filters={"region": "us"}),
        MetricQuery(time_range=(None, "2026-01-05")),
    )
    registry = ExecutionRegistry(max_workers=1)
    try:
        for query in cases:
            sql, bind = compile_metric(resolved, query, "qmark")
            assert sql.count("?") == len(bind), (query, sql, bind)
            result = registry.run_sync(resolved.source, resolved.base_dir, sql, bind, 100)
            assert result.rows is not None
    finally:
        registry.shutdown()


def test_grainless_trailing_rejects_a_start_bound(tmp_path):
    layer = _trailing_layer(tmp_path)
    resolved = layer.resolve("trailing_3d")
    with pytest.raises(SemanticError, match="omit start"):
        compile_metric(resolved, MetricQuery(time_range=("2026-01-01", "2026-01-05")), "qmark")


def test_trailing_window_rejects_a_finer_grain(tmp_path):
    layer = _trailing_layer(tmp_path)
    resolved = layer.resolve("trailing_3d")
    with pytest.raises(SemanticError, match="finer"):
        compile_metric(resolved, MetricQuery(grain="hour"), "qmark")


def test_trailing_window_spine_is_dialect_specific(tmp_path):
    layer = _trailing_layer(tmp_path)
    resolved = layer.resolve("trailing_3d")
    snow, _ = compile_metric(resolved, MetricQuery(grain="day"), "qmark", dialect="snowflake")
    assert "WITH RECURSIVE" in snow
    assert "DATEADD" in snow
    assert "GENERATOR" not in snow
    bq, _ = compile_metric(resolved, MetricQuery(grain="day"), "qmark", dialect="bigquery")
    assert "GENERATE_DATE_ARRAY" in bq
    assert "TIMESTAMP_TRUNC" in bq
    assert "DATE_TRUNC('day'" not in bq
    with pytest.raises(SemanticError, match="date spine"):
        compile_metric(resolved, MetricQuery(grain="day"), "qmark", dialect="sqlite")


def _two_dashboards_sharing_an_inline_name(root, multiplier_a=2, multiplier_b=100):
    for name, mult in (("a", multiplier_a), ("b", multiplier_b)):
        (root / f"{name}.yaml").write_text(
            f"title: Dash {name}\n"
            "source: {type: duckdb, database: ':memory:'}\n"
            "metrics:\n"
            f"  inline_rev: {{sql: 'SELECT 1 AS amount', expr: 'SUM(amount) * {mult}'}}\n"
            "queries: {q: 'SELECT 1'}\n"
            "tiles: [{id: w, metric: inline_rev}]\n"
        )


def test_inline_metric_defined_by_two_dashboards_is_ambiguous(tmp_path):
    """Regression for #87 — first-wins returned another dashboard's number silently.

    WorkspaceLayer already refuses the equivalent collision across repos; a
    project with two dashboards must not quietly answer with one of them.
    """
    from sqldash.project.store import DashboardStore
    from sqldash.semantics import MetricNotFoundError
    from sqldash.semantics.layer import SemanticLayer

    _two_dashboards_sharing_an_inline_name(tmp_path)
    layer = SemanticLayer(DashboardStore(tmp_path))

    with pytest.raises(MetricNotFoundError) as exc:
        layer.resolve("inline_rev")
    message = str(exc.value)
    assert "more than one dashboard" in message
    # It has to name both offenders, and a way out that is true on every
    # surface — MCP has no --dashboard flag, so the message must not name one.
    assert "a" in message
    assert "b" in message
    assert "metrics.yaml" in message
    assert "--dashboard" not in message


def test_dashboard_scope_resolves_each_definition(tmp_path):
    """The escape hatch the error names must actually work, and pick the right one."""
    from sqldash.project.store import DashboardStore
    from sqldash.semantics.layer import SemanticLayer

    _two_dashboards_sharing_an_inline_name(tmp_path)
    layer = SemanticLayer(DashboardStore(tmp_path))
    assert "* 2" in layer.resolve("inline_rev", "a").definition.expr
    assert "* 100" in layer.resolve("inline_rev", "b").definition.expr


def test_metrics_yaml_definition_is_canonical_not_a_collision(tmp_path):
    """metrics.yaml wins by design, so it is not ambiguity — it is the answer."""
    from sqldash.project.store import DashboardStore
    from sqldash.semantics.layer import SemanticLayer

    _two_dashboards_sharing_an_inline_name(tmp_path)
    (tmp_path / "metrics.yaml").write_text(
        "source: {type: duckdb, database: ':memory:'}\n"
        "relations: {r: {sql: 'SELECT 1 AS amount'}}\n"
        "metrics:\n"
        "  inline_rev: {relation: r, expr: 'SUM(amount) * 7'}\n"
    )
    layer = SemanticLayer(DashboardStore(tmp_path))
    assert "* 7" in layer.resolve("inline_rev").definition.expr


def test_one_dashboard_defining_a_name_is_not_ambiguous(tmp_path):
    """Guard against over-firing: a single inline definition still resolves."""
    from sqldash.project.store import DashboardStore
    from sqldash.semantics.layer import SemanticLayer

    (tmp_path / "only.yaml").write_text(
        "title: Only\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "metrics:\n"
        "  inline_rev: {sql: 'SELECT 1 AS amount', expr: 'SUM(amount) * 3'}\n"
        "queries: {q: 'SELECT 1'}\n"
        "tiles: [{id: w, metric: inline_rev}]\n"
    )
    layer = SemanticLayer(DashboardStore(tmp_path))
    assert "* 3" in layer.resolve("inline_rev").definition.expr


def _workspace_with_a_colliding_repo(root):
    """repo1 has two dashboards defining the same inline name; repo2 has neither."""
    (root / "repo1").mkdir()
    (root / "repo2").mkdir()
    for name, mult in (("a", 2), ("b", 100)):
        (root / "repo1" / f"{name}.yaml").write_text(
            f"title: Dash {name}\n"
            "source: {type: duckdb, database: ':memory:'}\n"
            "metrics:\n"
            f"  inline_rev: {{sql: 'SELECT 1 AS amount', expr: 'SUM(amount) * {mult}'}}\n"
            "queries: {q: 'SELECT 1'}\n"
            "tiles: [{id: w, metric: inline_rev}]\n"
        )
    (root / "repo1" / "c.yaml").write_text(
        "title: Dash c\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "metrics:\n  only_in_c: {sql: 'SELECT 1 AS amount', expr: 'SUM(amount)'}\n"
        "queries: {q: 'SELECT 1'}\n"
        "tiles: [{id: w, metric: only_in_c}]\n"
    )
    (root / "repo2" / "only.yaml").write_text(
        "title: Only\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "metrics:\n  other: {sql: 'SELECT 1 AS amount', expr: 'SUM(amount)'}\n"
        "queries: {q: 'SELECT 1'}\n"
        "tiles: [{id: w, metric: other}]\n"
    )
    from sqldash.project.store import DashboardStore
    from sqldash.semantics.layer import SemanticLayer, WorkspaceLayer

    return WorkspaceLayer(
        {d.name: SemanticLayer(DashboardStore(d)) for d in sorted(root.iterdir()) if d.is_dir()}
    )


@pytest.mark.parametrize("name", ["inline_rev", "repo1/inline_rev"])
def test_workspace_refuses_a_name_two_dashboards_of_one_repo_define(tmp_path, name):
    """The bare-name path went through all_metrics(), which is already first-wins
    per repo — so #87 survived here after being fixed one level down, and the
    first review caught it. Pinned in both forms: bare and repo-prefixed.
    """
    from sqldash.semantics import MetricNotFoundError

    workspace = _workspace_with_a_colliding_repo(tmp_path)
    with pytest.raises(MetricNotFoundError, match="more than one dashboard"):
        workspace.resolve(name)


def test_workspace_scoped_resolve_still_picks_the_right_definition(tmp_path):
    workspace = _workspace_with_a_colliding_repo(tmp_path)
    assert "* 2" in workspace.resolve("inline_rev", "repo1/a").definition.expr
    assert "* 100" in workspace.resolve("inline_rev", "repo1/b").definition.expr


def test_workspace_unambiguous_names_still_resolve(tmp_path):
    """Guard against the refusal over-firing across repos."""
    workspace = _workspace_with_a_colliding_repo(tmp_path)
    assert workspace.resolve("other").name == "repo2/other"


def test_workspace_takes_the_prefixed_name_listings_print(tmp_path):
    """A workspace prints metrics as 'repo/metric', so the natural scoped call
    pairs that name with a 'repo/dashboard' — which used to report the metric
    as nonexistent, because the inner layer never sees either prefix.
    """
    workspace = _workspace_with_a_colliding_repo(tmp_path)
    resolved = workspace.resolve("repo1/inline_rev", "repo1/a")
    assert "* 2" in resolved.definition.expr
    assert resolved.name == "repo1/inline_rev"


def test_workspace_refuses_a_metric_and_dashboard_in_different_repos(tmp_path):
    """Stripping the prefix must not mean ignoring it."""
    from sqldash.semantics import MetricNotFoundError

    workspace = _workspace_with_a_colliding_repo(tmp_path)
    with pytest.raises(MetricNotFoundError, match="not in repo 'repo1'"):
        workspace.resolve("repo2/other", "repo1/a")


def test_workspace_ambiguity_names_dashboards_the_caller_can_actually_pass(tmp_path):
    """The message tells you to resolve against one of the named dashboards, so
    the names in it have to be accepted by that same call — bare 'a' is not.
    """
    from sqldash.semantics import AmbiguousMetricError

    workspace = _workspace_with_a_colliding_repo(tmp_path)
    with pytest.raises(AmbiguousMetricError) as excinfo:
        workspace.resolve("repo1/inline_rev")
    assert excinfo.value.dashboards == ("repo1/a", "repo1/b")
    for named in excinfo.value.dashboards:
        assert workspace.resolve("repo1/inline_rev", named).definition.expr


def test_scoping_to_a_dashboard_without_the_name_still_names_the_ones_with_it(tmp_path):
    """The ambiguity refusal tells you to scope to a dashboard, so scoping to the
    wrong one is the natural next step. Answering "no such metric" there would
    contradict the message that sent the caller here and strand them.
    """
    from sqldash.semantics import MetricNotInDashboardError

    workspace = _workspace_with_a_colliding_repo(tmp_path)
    with pytest.raises(MetricNotInDashboardError) as excinfo:
        workspace.resolve("repo1/inline_rev", "repo1/c")
    assert excinfo.value.dashboards == ("repo1/a", "repo1/b")
    assert "repo1/a, repo1/b" in str(excinfo.value)
    for named in excinfo.value.dashboards:
        assert workspace.resolve("repo1/inline_rev", named).definition.expr


def _available_metric_names(exc):
    _, _, rest = str(exc).partition("available metrics: ")
    return [part.strip() for part in rest.split(",") if part.strip()]


def test_a_genuinely_unknown_name_still_lists_what_is_available(tmp_path):
    """The pointer must not swallow the plain not-found case. The names it
    lists have to be the ones that resolve from the workspace (prefixed);
    inner SemanticLayer's bare names do not (#463).
    """
    from sqldash.semantics import MetricNotFoundError

    workspace = _workspace_with_a_colliding_repo(tmp_path)
    for args in (
        ("repo1/no_such_metric", "repo1/c"),
        ("repo1/no_such_metric",),
        ("no_such_metric",),
    ):
        with pytest.raises(MetricNotFoundError, match="available metrics") as excinfo:
            workspace.resolve(*args)
        names = _available_metric_names(excinfo.value)
        assert "repo1/only_in_c" in names, args
        assert "repo2/other" in names, args
        assert "only_in_c" not in names, args
        assert all("/" in name for name in names), names


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("-30d", lambda t: (t - timedelta(days=30)).isoformat()),
        ("last_30_days", lambda t: (t - timedelta(days=30)).isoformat()),
        ("mtd", lambda t: t.replace(day=1).isoformat()),
        ("ytd", lambda t: t.replace(month=1, day=1).isoformat()),
        ("today", lambda t: t.isoformat()),
        ("2026-01-01", lambda t: "2026-01-01"),
    ],
)
def test_relative_date_tokens_resolve_to_the_right_date(token, expected):
    """Not merely 'does not error' — the resolved date has to be correct."""
    from sqldash.params import resolve_date_token

    assert resolve_date_token(token) == expected(date.today())


def test_a_window_token_ends_today():
    """`-30d` names a window: it starts 30 days ago and ends now."""
    from sqldash.params import resolve_date_token

    assert resolve_date_token("-30d", window_end=True) == date.today().isoformat()


def test_non_string_values_pass_through():
    from sqldash.params import resolve_date_token

    assert resolve_date_token(None) is None
    assert resolve_date_token(7) == 7


def test_an_unrecognized_date_token_names_the_accepted_forms():
    """A typo used to pass through to the warehouse, which said 'use
    YYYY-MM-DD' and never mentioned that relative tokens exist."""
    from sqldash.params import ParamError, resolve_date_token

    with pytest.raises(ParamError, match="last_30_days") as excinfo:
        resolve_date_token("last_30_day")
    assert "YYYY-MM-DD" in str(excinfo.value)
    assert resolve_date_token("") == ""
    assert resolve_date_token("2026-01-01T00:00:00") == "2026-01-01T00:00:00"
    assert resolve_date_token("2026-01-01T00:00:00Z") == "2026-01-01T00:00:00Z"
    assert resolve_date_token("2026-01-01T00:00:00+00:00") == "2026-01-01T00:00:00+00:00"
    assert resolve_date_token("2026-1-1") == "2026-1-1"


def test_metric_query_cli_accepts_relative_dates(tmp_path):
    """Regression for #89: the metric path bypassed token resolution entirely, so
    `--start -30d` reached the warehouse as the literal string '-30d'."""
    from typer.testing import CliRunner

    from sqldash.cli import app
    from sqldash.scaffold import create_demo

    create_demo(tmp_path)
    runner = CliRunner()
    for token in ("-30d", "mtd", "ytd", "today"):
        result = runner.invoke(
            app,
            [
                "metric",
                "query",
                "-t",
                str(tmp_path / ".sqldash"),
                "revenue",
                "--start",
                token,
                "--format",
                "csv",
            ],
        )
        assert result.exit_code == 0, f"{token}: {result.output}"
        assert "invalid date" not in result.output.lower(), f"{token}: {result.output}"


def test_metric_query_cli_rejects_a_typoed_date_token(tmp_path):
    from typer.testing import CliRunner

    from sqldash.cli import app
    from sqldash.scaffold import create_demo

    create_demo(tmp_path)
    result = CliRunner().invoke(
        app,
        [
            "metric",
            "query",
            "-t",
            str(tmp_path / ".sqldash"),
            "revenue",
            "--start",
            "last_30_day",
        ],
    )
    assert result.exit_code == 1, result.output
    assert "Traceback" not in result.output, result.output
    assert "Conversion Error" not in result.output, result.output
    assert "unrecognized date" in result.output, result.output
    assert "last_30_days" in result.output, result.output


def test_query_cli_rejects_a_typoed_date_token(tmp_path):
    """`query` is the documented headless path; only `metric query` was catching
    ParamError, so the same typo became a typer traceback."""
    from typer.testing import CliRunner

    from sqldash.cli import app
    from sqldash.scaffold import create_demo

    create_demo(tmp_path)
    result = CliRunner().invoke(
        app,
        ["query", str(tmp_path), "revenue", "--start", "last_30_day"],
    )
    assert result.exit_code == 1, result.output
    assert "Traceback" not in result.output, result.output
    assert "Conversion Error" not in result.output, result.output
    assert "last_30_days" in result.output, result.output


def test_a_yaml_int_date_default_is_a_param_error(tmp_path):
    """Unquoted 20260101 used to bind as an int and become a warehouse
    Conversion Error. Name the filter instead. #197."""
    from sqldash.params import ParamError, param_values
    from sqldash.project.store import DashboardStore

    (tmp_path / "d.yaml").write_text(
        "title: D\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "filters:\n"
        "  - {name: day, type: date, default: 20260101}\n"
        "queries:\n"
        "  q: SELECT 1 WHERE d = {{ day }}\n"
        "tiles: [{id: w, query: q, position: {x: 0, y: 0, w: 6, h: 4}}]\n"
    )
    dash, _, _ = DashboardStore(tmp_path).load("d")
    with pytest.raises(ParamError, match="20260101"):
        param_values(dash, ["day"], {})


def test_a_yaml_int_daterange_scalar_default_is_a_param_error(tmp_path):
    """Scalar int used to fall through as missing start/end. #197."""
    from sqldash.params import ParamError, param_values
    from sqldash.project.store import DashboardStore

    (tmp_path / "d.yaml").write_text(
        "title: D\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "filters:\n"
        "  - {name: dates, type: daterange, default: 20260101}\n"
        "queries:\n"
        "  q: SELECT 1 WHERE d >= {{ dates_start }} AND d < {{ dates_end }}\n"
        "tiles: [{id: w, query: q, position: {x: 0, y: 0, w: 6, h: 4}}]\n"
    )
    dash, _, _ = DashboardStore(tmp_path).load("d")
    with pytest.raises(ParamError, match="20260101"):
        param_values(dash, ["dates_start", "dates_end"], {})


def test_a_typoed_daterange_preset_is_not_a_missing_range(tmp_path):
    """A date filter with default last_30_day raises; the same typo on a
    daterange used to drop the range and query the full table."""
    from sqldash.params import ParamError, param_values
    from sqldash.project.store import DashboardStore

    (tmp_path / "d.yaml").write_text(
        "title: D\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "filters:\n"
        "  - {name: dates, type: daterange, default: last_30_day}\n"
        "queries:\n"
        "  q: SELECT 1 WHERE d >= {{ dates_start }} AND d < {{ dates_end }}\n"
        "tiles: [{id: w, query: q, position: {x: 0, y: 0, w: 6, h: 4}}]\n"
    )
    dash, _, _ = DashboardStore(tmp_path).load("d")
    with pytest.raises(ParamError, match="last_30_days"):
        param_values(dash, ["dates_start", "dates_end"], {})


def test_a_daterange_default_that_is_not_a_preset_is_missing_not_bound(tmp_path):
    """`default: today` is a valid date token but not a daterange preset.
    Binding it and then continue-ing left the param absent and export
    cortex KeyError'd. Fall through to missing, as before this PR."""
    from sqldash.params import param_values
    from sqldash.project.store import DashboardStore

    (tmp_path / "d.yaml").write_text(
        "title: D\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "filters:\n"
        "  - {name: dates, type: daterange, default: today}\n"
        "queries:\n"
        "  q: SELECT 1 WHERE d >= {{ dates_start }}\n"
        "tiles: [{id: w, query: q, position: {x: 0, y: 0, w: 6, h: 4}}]\n"
    )
    dash, _, _ = DashboardStore(tmp_path).load("d")
    values, missing = param_values(dash, ["dates_start", "dates_end"], {})
    assert values == {}
    assert missing == ["dates_start", "dates_end"]
    values, missing = param_values(
        dash,
        ["dates_start", "dates_end"],
        {"dates_start": "2026-01-01", "dates_end": "2026-01-31"},
    )
    assert missing == []
    assert values == {"dates_start": "2026-01-01", "dates_end": "2026-01-31"}


def test_missing_params_message_names_a_single_date_daterange_default(tmp_path):
    """The 422 used to say 'declare filter defaults' after they already had. #274."""
    from sqldash.params import ParamError
    from sqldash.project.store import DashboardStore
    from sqldash.semantics.bind import bind_named_query

    (tmp_path / "d.yaml").write_text(
        "title: D\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "filters:\n"
        "  - {name: dates, type: daterange, default: today}\n"
        "queries:\n"
        "  q: SELECT 1 WHERE d >= {{ dates_start }} AND d < {{ dates_end }}\n"
        "tiles: [{id: w, query: q, position: {x: 0, y: 0, w: 6, h: 4}}]\n"
    )
    dash, _, _ = DashboardStore(tmp_path).load("d")
    with pytest.raises(ParamError, match="single date") as excinfo:
        bind_named_query(dash, dash.queries["q"])
    assert "declare filter defaults" not in str(excinfo.value)
    assert "{start, end}" in str(excinfo.value)


def test_compact_iso_dates_are_not_passed_through():
    """fromisoformat accepts YYYYMMDD and YYYYMMDDTHHMMSS; DuckDB then
    raises Conversion Error. Dashed ISO (Z, offsets, 24:00:00, non-padded)
    must still pass."""
    from sqldash.params import ParamError, resolve_date_token

    with pytest.raises(ParamError, match="YYYY-MM-DD"):
        resolve_date_token("20260101")
    with pytest.raises(ParamError, match="YYYY-MM-DD"):
        resolve_date_token("20260101T000000")
    with pytest.raises(ParamError, match="YYYY-MM-DD"):
        resolve_date_token("   ")
    assert resolve_date_token("2026-01-01T00:00:00") == "2026-01-01T00:00:00"
    assert resolve_date_token("2026-01-01T00:00:00Z") == "2026-01-01T00:00:00Z"
    assert resolve_date_token("2026-01-01T00:00:00+00:00") == "2026-01-01T00:00:00+00:00"
    assert resolve_date_token("2026-01-01T24:00:00") == "2026-01-01T24:00:00"
    assert resolve_date_token("2026-1-1") == "2026-1-1"
    assert resolve_date_token(" 2026-01-01 ") == "2026-01-01"
    assert resolve_date_token("0000-01-01") == "0000-01-01"


def test_slash_dates_are_rewritten_not_rejected():
    """DuckDB accepts YYYY/MM/DD, so rejecting it turned working input into a
    ParamError on every surface. It is rewritten to dashed rather than passed
    through, because not every warehouse takes slashes."""
    from sqldash.params import ParamError, resolve_date_token

    assert resolve_date_token("2026/01/01") == "2026-01-01"
    assert resolve_date_token("2026/1/1") == "2026-1-1"
    assert resolve_date_token(" 2026/01/01 ") == "2026-01-01"
    for rejected in ("2026.01.01", "01/01/2026", "Jan 1 2026", "2026/13/01", "2026/01"):
        with pytest.raises(ParamError, match="YYYY-MM-DD"):
            resolve_date_token(rejected)


ISO_FORMS_VS_DUCKDB = [
    ("2026-04-28", True),
    ("2026-4-8", True),
    ("2026-04-28T10:30", True),
    ("2026-04-28 10:30:00", True),
    ("2026-04-28  10:30", True),
    ("2026-04-28\t10:30", True),
    ("2026-04-28 \t 10:30", True),
    ("2026-04-28\n10:30", True),
    ("2026-04-28T 10:30", True),
    ("2026-04-28 1:30", True),
    ("2026-04-28T1:3", True),
    ("2026-04-28T1:30:5", True),
    ("2026-4-28 1:3:5", True),
    ("2026-04-28T24:00", True),
    ("2026-04-28  24:00:00", True),
    ("2026-04-28T24:00:00", True),
    ("2026-04-28T24:00:00.0", True),
    ("2026-04-28T24:00:00.0000001", True),
    ("2026-04-28T24:00:00Z", True),
    ("2026-04-28T24:00:00+05:30", True),
    ("9999-12-31T24:00:00", True),
    ("0000-01-01", True),
    ("0000-01-01T00:00:00", True),
    ("0000-02-29", True),
    ("2024-02-29", True),
    ("2000-02-29", True),
    ("2026-04-28T10:30:00Z", True),
    ("2026-04-28T10:30:00+00:00", True),
    ("2026-04-28T10:30:00+0530", True),
    ("2026-04-28T10:30:00+05", True),
    ("2026-04-28 10:30:00-05:00", True),
    ("2026-04-28T10:30:00.5", True),
    ("2026-04-28T10:30:00.5Z", True),
    ("2026-04-28T10:30:00.", True),
    ("2026-04-28T10:30:00.123456789", True),
    ("2026-04-28T10:30:00 +00:00", False),
    ("2026-04-28T10:30:00 +05", False),
    ("2026-04-28 10:30:00 Z", False),
    ("2026-04-28 10:30:00 -05:00", False),
    ("2026-04-28 24:00:00 +00:00", False),
    ("2026-04-28T10:30Z", False),
    ("2026-04-28T10:30+05", False),
    ("2026-04-28 10:30+05:30", False),
    ("2026-04-28T10:30:00z", False),
    ("2026-04-28T10:30:00+5", False),
    ("2026-04-28T10:30:00+5:30", False),
    ("2026-04-28T10:30:00+05:3", False),
    ("2026-04-28T10:30:00+053", False),
    ("2026-04-28T10:30:00+05300", False),
    ("2026-04-28T10:30:00Z+05", False),
    ("2026-04-28T24:00:00.5", False),
    ("2026-04-28T24:00:01", False),
    ("2026-04-28T24:01", False),
    ("2026-04-28T25:00", False),
    ("2026-04-28T10:60", False),
    ("2026-04-28T10:30:60", False),
    ("2026-04-28T10", False),
    ("2026-04-28T10+05:30", False),
    ("2026-04-28T10:30:00,5", False),
    ("2026-04-28TT10:30", False),
    ("2026-04-28 T10:30", False),
    ("2026-04-28t10:30", False),
    ("2026-04-28 1 :30", False),
    ("2026-04-28T10:030", False),
    ("2026-13-01", False),
    ("2026-02-30", False),
    ("2025-02-29", False),
    ("1900-02-29", False),
    ("0000-00-00", False),
    ("2026-004-28", False),
    ("2026-04-028", False),
    ("2026-W01-1", False),
    ("2026-W01", False),
    ("2026-001", False),
    ("20260428", False),
    ("20260428T000000", False),
    ("2026.04.28", False),
    ("Apr 28 2026", False),
    ("2026-04-28 10:30 BC", False),
    ("+2026-04-28", False),
]

DUCKDB_ONLY_FORMS = [
    "2026-04-28T10:30:00 UTC",
    "2026-4-28 001:30",
    "2026-04-28T10:30:00+05:",
    "2026-04-28T10:30:00+05:30:00",
    "10000-01-01",
    "02026-04-28",
    "1-1-1",
    "-2026-04-28",
    "infinity",
    "epoch",
]


@pytest.fixture(scope="module")
def duckdb_timestamp_cast():
    import duckdb

    con = duckdb.connect()

    def takes(value: str) -> bool:
        try:
            con.execute("SELECT CAST(? AS TIMESTAMP)", [value]).fetchone()
            return True
        except duckdb.Error:
            return False

    return takes


@pytest.mark.parametrize(("value", "accepted"), ISO_FORMS_VS_DUCKDB)
def test_iso_forms_track_duckdbs_timestamp_cast(value, accepted, duckdb_timestamp_cast):
    """#358: each row is the verdict of a live `CAST(? AS TIMESTAMP)` — the
    documented contract — asserted on DuckDB first so a row that drifts
    fails here as a DuckDB change, then on `resolve_date_token`. Python's
    ISO vocabulary is not the warehouse's in either direction; the DATE cast
    is looser still (it takes `10:30:00 +00:00`), which is why the old test
    that fell back to it blessed forms the TIMESTAMP cast refuses."""
    from sqldash.params import ParamError, resolve_date_token

    assert duckdb_timestamp_cast(value) is accepted, f"DuckDB verdict moved for {value!r}"
    if accepted:
        assert resolve_date_token(value) == value
    else:
        with pytest.raises(ParamError, match="YYYY-MM-DD"):
            resolve_date_token(value)


@pytest.mark.parametrize("value", DUCKDB_ONLY_FORMS)
def test_non_iso_forms_duckdb_takes_stay_rejected(value, duckdb_timestamp_cast):
    """DuckDB's parser is wider than ISO — zone names, five-digit and negative
    years, `infinity` — and Postgres/Snowflake do not agree with it. The
    documented shape is a dashed ISO date, so these stay a named error
    rather than a per-warehouse surprise. The DuckDB assertion pins that
    the rejection is a choice, not the cast's verdict."""
    from sqldash.params import ParamError, resolve_date_token

    assert duckdb_timestamp_cast(value) is True, f"DuckDB verdict moved for {value!r}"
    with pytest.raises(ParamError, match="YYYY-MM-DD"):
        resolve_date_token(value)


def test_a_slashed_date_with_a_time_is_rewritten_then_checked():
    from sqldash.params import ParamError, resolve_date_token

    assert resolve_date_token("2026/01/01T00:00:00") == "2026-01-01T00:00:00"
    assert resolve_date_token("2026/1/1 1:30") == "2026-1-1 1:30"
    with pytest.raises(ParamError, match="YYYY-MM-DD"):
        resolve_date_token("2026/01/01T10:30:00 +00:00")


def test_compare_window_names_a_bound_python_cannot_shift():
    """`0000-01-01` binds fine (DuckDB takes year 0) but has no prior period
    in Python's calendar; that used to escape as a bare ValueError."""
    from sqldash.params import ParamError
    from sqldash.period import compare_window

    with pytest.raises(ParamError, match="cannot shift the window '0000-01-01'"):
        compare_window("previous_period", "0000-01-01", "2026-01-01")
    with pytest.raises(ParamError, match="cannot shift the window '0001-01-01'"):
        compare_window("previous_period", "0001-01-01", "0001-01-02")
    assert compare_window("previous_period", "2026-01-01", "2026-01-02") == {
        "start": "2025-12-30",
        "end": "2025-12-31",
        "label": "previous period",
    }


def _project_with_a_collision(root):
    for n in ("a", "b"):
        (root / f"{n}.yaml").write_text(
            f"title: Dash {n}\n"
            "source: {type: duckdb, database: ':memory:'}\n"
            "metrics:\n  inline_rev: {sql: 'SELECT 1 AS amount', expr: 'SUM(amount)'}\n"
            "queries: {q: 'SELECT 1'}\n"
            "tiles: [{id: w, metric: inline_rev}]\n"
        )
    return SemanticLayer(DashboardStore(root))


def test_listings_mark_a_name_no_resolve_path_will_answer_to(tmp_path):
    """all_metrics() was first-wins, so every listing surface presented an
    ambiguous name as an ordinary metric while resolve() refused it — the
    "two surfaces disagreeing" shape. The fact rides on the metric itself so
    the six surfaces that list metrics cannot each forget it.
    """
    layer = _project_with_a_collision(tmp_path)
    (found,) = [m for m in layer.all_metrics() if m.name == "inline_rev"]
    assert found.ambiguous_with == ("a", "b")


def test_a_canonical_metrics_yaml_name_is_not_ambiguous(tmp_path):
    """metrics.yaml wins by design, so inline definitions of the same name are
    overrides within their dashboard, not a collision."""
    layer = _project_with_a_collision(tmp_path)
    (tmp_path / "metrics.yaml").write_text(
        "source: {type: duckdb, database: ':memory:'}\n"
        "metrics:\n  inline_rev: {sql: 'SELECT 7 AS amount', expr: 'SUM(amount)'}\n"
    )
    (found,) = [m for m in layer.all_metrics() if m.name == "inline_rev"]
    assert found.ambiguous_with == ()
    assert found.origin == "project"


def test_an_ordinary_metric_is_never_marked(tmp_path):
    layer = _project_with_a_collision(tmp_path)
    (tmp_path / "c.yaml").write_text(
        "title: C\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "metrics:\n  only_here: {sql: 'SELECT 1 AS amount', expr: 'SUM(amount)'}\n"
        "tiles: [{id: w, metric: only_here}]\n"
    )
    (found,) = [m for m in layer.all_metrics() if m.name == "only_here"]
    assert found.ambiguous_with == ()


def test_workspace_listings_repo_qualify_the_ambiguity(tmp_path):
    """Same rule as the error messages: a name a caller cannot pass back is not
    an answer."""
    workspace = _workspace_with_a_colliding_repo(tmp_path)
    (found,) = [m for m in workspace.all_metrics() if m.name.endswith("/inline_rev")]
    assert found.ambiguous_with == ("repo1/a", "repo1/b")


@pytest.mark.parametrize(
    "value",
    [None, "", "   ", {"op": ">=", "value": None}, {"op": "=", "value": ""}],
)
def test_a_filter_with_no_value_is_refused_rather_than_bound(layer, value):
    """`x = NULL` is never true, so binding it returns the aggregate over zero
    rows — a confident NULL with no error, which is the worst shape here. The
    dashboard path drops an empty filter before the compiler sees it, so a value
    that arrives was passed deliberately and the caller can omit it instead.
    """
    with pytest.raises(SemanticError, match="has no value"):
        compile_metric(rev(layer), MetricQuery(filters={"region": value}), "qmark")


@pytest.mark.parametrize(
    "value",
    [
        {"$gt": "a"},
        {},
        {"op": "=", "value": "eu", "unit": "day"},
    ],
)
def test_a_filter_dict_that_is_not_op_value_is_refused_by_shape(layer, value):
    """#658: a dict with neither key fell through to `value.get("value")` → None and
    was reported as "has no value", which is false (it has one, the compiler does not
    understand it) and whose advice — omit it — would silently drop a filter the
    caller meant to apply."""
    with pytest.raises(SemanticError) as exc:
        compile_metric(rev(layer), MetricQuery(filters={"region": value}), "qmark")
    assert "has no value" not in str(exc.value)
    assert "not a filter value" in str(exc.value)
    assert "accepted shapes" in str(exc.value)


@pytest.mark.parametrize(
    "value",
    [
        {"op": "in", "value": {"a": 1}},
        {"op": "=", "value": {"a": 1}},
        {"op": "!=", "value": {"a": 1}},
        {"op": ">", "value": {"a": 1}},
        {"op": ">=", "value": {"a": 1}},
        {"op": "<", "value": {"a": 1}},
        {"op": "<=", "value": {"a": 1}},
        {"op": "=", "value": {"op": "=", "value": "eu"}},
        {"op": "in", "value": [{"a": 1}]},
        ["eu", {"a": 1}],
        ["eu", ["us"]],
    ],
)
def test_a_value_the_compiler_cannot_bind_is_refused_before_the_query_runs(layer, value):
    """#658: a dict under `value:` was bound into `IN (?)` and came back as a
    warehouse cast error. It is a bind parameter, so it was never an injection —
    but the boundary refuses a caller's bad input by name rather than deferring it
    to the engine, and the engine's message names neither the filter nor the fix."""
    with pytest.raises(SemanticError) as exc:
        compile_metric(rev(layer), MetricQuery(filters={"region": value}), "qmark")
    assert "region" in str(exc.value)
    assert "where a value was expected" in str(exc.value)
    assert "accepted shapes" in str(exc.value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("eu", "region = ?"),
        (["eu", "us"], "region IN (?, ?)"),
        (("eu", "us"), "region IN (?, ?)"),
        ({"value": "eu"}, "region = ?"),
        ({"op": "!=", "value": "eu"}, "region != ?"),
        ({"op": ">=", "value": 3}, "region >= ?"),
        ({"op": "in", "value": "eu"}, "region IN (?)"),
        ({"op": "in", "value": ["eu", "us"]}, "region IN (?, ?)"),
    ],
)
def test_the_accepted_filter_shapes_still_compile(layer, value, expected):
    """The refusal is about shapes the compiler cannot bind; every shape it can
    still binds, and still as parameters."""
    sql, bind = compile_metric(rev(layer), MetricQuery(filters={"region": value}), "qmark")
    assert expected in sql
    assert bind
    assert all(isinstance(b, (str, int)) for b in bind)


@pytest.mark.parametrize("op", ["!=", ">", ">=", "<", "<="])
@pytest.mark.parametrize("values", [["eu", "us"], ("eu", "us"), ["eu"], []])
def test_an_explicit_op_with_a_list_is_refused_not_turned_into_in(layer, op, values):
    """#668: `{op: ">", value: [...]}` compiled to `region IN (?, ?)`. The op was
    dropped without a word and the caller got rows for a filter they did not ask
    for. `!=` was the worst of them: it answered with exactly the rows it was
    asked to exclude. A NOT IN reading was left out on purpose, since a NULL in
    the list makes NOT IN match nothing and that would be a new silent answer."""
    with pytest.raises(SemanticError) as exc:
        compile_metric(
            rev(layer), MetricQuery(filters={"region": {"op": op, "value": values}}), "qmark"
        )
    message = str(exc.value)
    assert f"op '{op}' with a list value" in message
    assert "region" in message
    assert "{op: in, value: [...]}" in message


@pytest.mark.parametrize(
    "value",
    [
        ["eu", "us"],
        ("eu", "us"),
        {"value": ["eu", "us"]},
        {"op": "=", "value": ["eu", "us"]},
        {"op": "=", "value": ("eu", "us")},
        {"op": "in", "value": ["eu", "us"]},
    ],
)
def test_a_list_with_a_membership_op_still_means_in(layer, value):
    """#668: the refusal is about ops that are not membership. A bare list, a dict
    with no `op`, and a spelled `=` all mean "equals one of these", so spelling
    `op: "="` must not change the answer versus omitting it."""
    sql, bind = compile_metric(rev(layer), MetricQuery(filters={"region": value}), "qmark")
    assert "region IN (?, ?)" in sql
    assert bind[-2:] == ["eu", "us"]
    assert "'eu'" not in sql


def test_an_uppercase_in_op_with_a_list_still_compiles(layer):
    sql, _ = compile_metric(
        rev(layer), MetricQuery(filters={"region": {"op": "IN", "value": ["eu"]}}), "qmark"
    )
    assert "region IN (?)" in sql


def test_the_dashboard_path_still_treats_an_empty_filter_as_inactive(tmp_path):
    """The two surfaces have to agree that neither produces a wrong number —
    the dashboard drops it, the explicit caller hears about it. If the drop ever
    stopped happening, every dashboard with a cleared filter would start
    erroring instead."""
    from sqldash.scaffold import create_demo
    from sqldash.semantics.bind import bind_metric

    create_demo(tmp_path)
    store = DashboardStore(tmp_path / ".sqldash")
    layer = SemanticLayer(store)
    dashboard, _, _ = store.load("demo")
    for params in ({"region": ""}, {"region": None}, {"region": "all"}):
        bound = bind_metric(layer, "revenue", dash=dashboard, params=params, limit=100)
        assert "us" not in bound.bind, (params, bound.sql, bound.bind)
    bound = bind_metric(layer, "revenue", dash=dashboard, params={"region": "us"}, limit=100)
    assert "us" in bound.bind


def test_dashboard_all_is_off_for_metrics_even_when_not_listed(tmp_path):
    """#216 dropped the `all in options` gate in inactive_params; the metric
    path in _query_from_dash still had it, so a listed-options select with
    default: all bound region='all' and returned 0 rows."""
    from sqldash.scaffold import create_demo
    from sqldash.semantics.bind import bind_metric

    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "allbug.yaml").write_text(
        "title: All-sentinel\n"
        "source: {type: duckdb, attach_files: true}\n"
        "filters:\n"
        "  - {name: region, type: select, default: all, options: [us, eu, apac]}\n"
        "tiles:\n"
        "  - {title: M, metric: order_count}\n"
    )
    store = DashboardStore(tmp_path / ".sqldash")
    layer = SemanticLayer(store)
    dashboard, _, _ = store.load("allbug")
    off = bind_metric(layer, "order_count", dash=dashboard, params={}, limit=100)
    assert "all" not in off.bind, (off.sql, off.bind)
    on = bind_metric(layer, "order_count", dash=dashboard, params={"region": "us"}, limit=100)
    assert "us" in on.bind


def test_a_windowed_cumulative_still_accumulates_from_the_beginning(tmp_path):
    """`WHERE` runs before window functions, so filtering the start bound with
    the rest of the predicates took the earlier buckets out of the frame and the
    running total restarted at the window edge. The same metric then meant
    something different filtered than unfiltered — a window-local running total
    — and nothing said so.
    """
    layer = _cumulative_layer(tmp_path)
    resolved = layer.resolve("running_revenue")
    registry = ExecutionRegistry(max_workers=1)
    try:
        sql, bind = compile_metric(resolved, MetricQuery(grain="day"), "qmark")
        full = {
            str(row[0])[:10]: row[1]
            for row in registry.run_sync(resolved.source, resolved.base_dir, sql, bind, 100).rows
        }

        sql, bind = compile_metric(
            resolved,
            MetricQuery(grain="day", time_range=("2026-01-04", "2026-01-06")),
            "qmark",
        )
        windowed = registry.run_sync(resolved.source, resolved.base_dir, sql, bind, 100)
        rows = {str(row[0])[:10]: row[1] for row in windowed.rows}

        assert sorted(rows) == ["2026-01-04", "2026-01-05", "2026-01-06"], rows
        for day, value in rows.items():
            assert value == full[day], (day, value, full[day])
        # 10+20+30+40 — the days before the window are still in the total.
        assert rows["2026-01-04"] == 100
    finally:
        registry.shutdown()


def test_a_cumulative_window_end_still_excludes_later_rows(tmp_path):
    """The end bound must keep filtering inside the window: rows after it are
    not part of the running total, they are simply not yet counted."""
    layer = _cumulative_layer(tmp_path)
    resolved = layer.resolve("running_revenue")
    registry = ExecutionRegistry(max_workers=1)
    try:
        sql, bind = compile_metric(
            resolved, MetricQuery(grain="day", time_range=(None, "2026-01-03")), "qmark"
        )
        rows = registry.run_sync(resolved.source, resolved.base_dir, sql, bind, 100).rows
        assert [row[1] for row in rows] == [10, 30, 60], rows
    finally:
        registry.shutdown()


def test_a_windowed_cumulative_uses_sibling_ctes_not_a_nested_with(tmp_path):
    """Snowflake rejects a `WITH` inside a CTE body, so the running-total level
    is a sibling of the bucket level rather than wrapped around it."""
    layer = _cumulative_layer(tmp_path)
    resolved = layer.resolve("running_revenue")
    sql, bind = compile_metric(
        resolved, MetricQuery(grain="day", time_range=("2026-01-04", None)), "qmark"
    )
    assert sql.count("WITH") == 1, sql
    assert "sqldash_running" in sql, sql
    # The start bound binds last, after the inner predicates it no longer sits with.
    assert bind == ["2026-01-04"], bind


def test_a_cumulative_start_includes_the_overlapping_bucket(tmp_path):
    """The start is compared to the bucket, not the raw timestamp. A mid-week
    start used to drop the week that contains it (`week_start >= midweek` is
    false) while still folding that week's rows into later totals — a missing
    row, not a wrong number.
    """
    layer = _cumulative_layer(tmp_path)
    resolved = layer.resolve("running_revenue")
    registry = ExecutionRegistry(max_workers=1)
    try:
        sql, bind = compile_metric(
            resolved, MetricQuery(grain="week", time_range=("2026-01-04", None)), "qmark"
        )
        assert "DATE_TRUNC('week'" in sql, sql
        windowed = {
            str(row[0])[:10]: row[1]
            for row in registry.run_sync(resolved.source, resolved.base_dir, sql, bind, 100).rows
        }
        full_sql, full_bind = compile_metric(resolved, MetricQuery(grain="week"), "qmark")
        full = {
            str(row[0])[:10]: row[1]
            for row in registry.run_sync(
                resolved.source, resolved.base_dir, full_sql, full_bind, 100
            ).rows
        }
        assert windowed, "overlapping week was dropped from the output"
        first = min(windowed)
        assert first <= "2026-01-04", windowed
        assert windowed[first] == full[first], (first, windowed, full)
    finally:
        registry.shutdown()


DERIVED_OVER_FILTERED = """
source: {type: duckdb, database: ':memory:'}
relations:
  events:
    sql: "SELECT 'paid' AS status, 100 AS amount UNION ALL SELECT 'refunded', -40"
metrics:
  gross:
    relation: events
    expr: SUM(amount)
    filters: ["status = 'paid'"]
  refunds:
    relation: events
    expr: ABS(SUM(amount))
    filters: ["status = 'refunded'"]
  refund_rate:
    derived: "{refunds} / NULLIF({gross}, 0)"
"""


def test_a_derived_metric_over_a_filtered_component_is_refused(tmp_path):
    """Expansion inlines a component's `expr` and nothing else, so its
    `filters` were dropped — and the answer stayed plausible while being wrong.
    A rate over two differently-filtered components compiled to
    ABS(SUM(x)) / SUM(x) over every row and reported exactly 1.0.
    """
    (tmp_path / "metrics.yaml").write_text(DERIVED_OVER_FILTERED)
    (tmp_path / "empty.yaml").write_text(
        "title: E\nsource: {type: duckdb, database: ':memory:'}\ntiles: []\n"
    )
    layer = SemanticLayer(DashboardStore(tmp_path))
    with pytest.raises(SemanticError) as exc:
        layer.resolve("refund_rate")
    message = str(exc.value)
    assert "carries filters" in message, message
    assert "status = 'refunded'" in message, message
    assert "CASE WHEN" in message, message


def test_a_derived_metric_over_unfiltered_components_still_works(tmp_path):
    """The guard must not refuse the ordinary case, nor a filter on the derived
    metric itself — only a filter on a component being inlined."""
    (tmp_path / "metrics.yaml").write_text(
        """
source: {type: duckdb, database: ':memory:'}
relations:
  events:
    sql: "SELECT 'paid' AS status, 100 AS amount UNION ALL SELECT 'refunded', -40"
metrics:
  gross:
    relation: events
    expr: SUM(CASE WHEN status = 'paid' THEN amount END)
  n:
    relation: events
    expr: COUNT(*)
  avg_amount:
    derived: "{gross} / NULLIF({n}, 0)"
    filters: ["status IS NOT NULL"]
"""
    )
    (tmp_path / "empty.yaml").write_text(
        "title: E\nsource: {type: duckdb, database: ':memory:'}\ntiles: []\n"
    )
    layer = SemanticLayer(DashboardStore(tmp_path))
    resolved = layer.resolve("avg_amount")
    sql, _ = compile_metric(resolved, MetricQuery(), "qmark")
    # The derived metric's own filter survives; the components carried none.
    assert "status IS NOT NULL" in sql, sql
    assert "CASE WHEN status = 'paid'" in sql, sql


@pytest.mark.parametrize(
    ("derived", "carried"),
    [
        ("ROUND({trailing_28d_revenue} / NULLIF({revenue}, 0), 4)", "window: 28 days"),
        ("{cumulative_revenue} + {revenue}", "cumulative: true"),
    ],
)
def test_a_derived_metric_over_a_window_or_cumulative_component_is_refused(
    tmp_path, derived, carried
):
    """Expansion inlines the ref's `expr` only, so a trailing window or running
    total became the plain per-bucket aggregate: trailing/revenue reported
    exactly 1.0 and cumulative + revenue was 2x revenue per bucket, lint green
    (#604). Load, lint and MCP validate_metrics must all refuse it."""
    create_demo(tmp_path)
    metrics_path = tmp_path / ".sqldash" / "metrics.yaml"
    text = metrics_path.read_text() + f'\n  bad_mix:\n    derived: "{derived}"\n'
    metrics_path.write_text(text)

    store = DashboardStore(tmp_path)
    with pytest.raises(SemanticError) as exc:
        SemanticLayer(store).resolve("bad_mix")
    assert carried in str(exc.value), exc.value

    findings = lint_project(store, SemanticLayer(store))
    assert any(f.level == "error" and carried in f.message for f in findings), findings

    verdict = validate_metrics(
        text, store=store, layer=SemanticLayer(store), registry=None, check_schema=False
    )
    assert verdict["valid"] is False, verdict
    assert any("bad_mix" in e and carried in e for e in verdict["errors"]), verdict


RESERVED_NAME_CASES = [
    ("metric name 'trailing'", "  trailing: {table: orders, expr: SUM(amount)}\n"),
    (
        "dimension name 'order'",
        "  m: {table: orders, expr: SUM(amount), dimensions: [{name: order}]}\n",
    ),
    (
        "time dimension name 'current_timestamp'",
        "  m: {table: orders, expr: SUM(amount), "
        "time_dimension: {name: current_timestamp, grain: day}}\n",
    ),
    ("metric name 'USER'", "  USER: {table: orders, expr: COUNT(*)}\n"),
]


@pytest.mark.parametrize(("expected", "body"), RESERVED_NAME_CASES)
def test_a_reserved_word_name_is_a_parse_error_that_names_it(expected, body):
    """#291: `trailing` lint-cleaned and then failed as `SUM(trailing) OVER`, and
    `current_timestamp` as a time dimension is the session clock on Postgres, not
    a column. The name is a static property of the file, so every surface that
    parses it refuses it, and the message points at the YAML name."""
    with pytest.raises(SemanticError) as exc:
        parse_metrics_file("source: {type: duckdb}\nmetrics:\n" + body)
    assert f"{expected} is a SQL reserved word; rename it" in str(exc.value), str(exc.value)
    assert "Value error" not in str(exc.value)


def test_a_reserved_dimension_name_says_where_the_column_goes():
    with pytest.raises(SemanticError, match="rename it and point expr at the column"):
        parse_metrics_file(
            "source: {type: duckdb}\nmetrics:\n"
            "  m: {table: orders, expr: SUM(amount), dimensions: [{name: user}]}\n"
        )


def test_an_inline_metric_named_a_reserved_word_is_a_dashboard_error():
    from sqldash.project.store import InvalidDashboardError, parse_dashboard

    with pytest.raises(InvalidDashboardError, match="metric name 'order' is a SQL reserved word"):
        parse_dashboard(
            "title: T\nsource: {type: duckdb}\n"
            "metrics:\n  order: {table: orders, expr: COUNT(*)}\ntiles: []\n"
        )


@pytest.mark.parametrize(
    "name", ["date", "year", "month", "day", "value", "count", "status", "account", "start"]
)
def test_ordinary_column_words_are_still_legal_names(name):
    """The list is what DuckDB/Postgres actually reject or silently rebind, not
    every word the SQL standard reserves: `date` and `year` are column names in
    every warehouse and a hard error on them would break real files."""
    mf = parse_metrics_file(
        "source: {type: duckdb}\nmetrics:\n"
        f"  {name}: {{table: orders, expr: SUM(amount), dimensions: [{{name: {name}}}], "
        f"time_dimension: {{name: {name}_at, grain: day}}}}\n"
    )
    assert name in mf.metrics


def test_the_compiler_quotes_the_aliases_it_invents_and_nothing_else(layer):
    """Only aliases sqldash names are quoted. Author column exprs stay bare: on
    Snowflake an unquoted name folds to upper case, so quoting one would change
    which column it resolves to."""
    sql, _ = compile_metric(rev(layer), MetricQuery(dimensions=("region",), grain="day"), "qmark")
    assert "DATE_TRUNC('day', order_date) AS \"order_date\"" in sql
    assert 'region AS "region"' in sql
    assert 'SUM(amount) AS "revenue"' in sql
    bq, _ = compile_metric(
        rev(layer), MetricQuery(dimensions=("region",), grain="day"), "qmark", dialect="bigquery"
    )
    assert "AS `order_date`, region AS `region`, SUM(amount) AS `revenue`" in bq


def _reserved_named_metric(resolved, *, cumulative=False):
    """A definition the models would refuse, built past validation: proves the
    compiler alone keeps a reserved alias an identifier."""
    from dataclasses import replace

    from sqldash.models.semantics import DimensionDef, MetricDef, TimeDimensionDef

    definition = MetricDef.model_construct(
        relation="sparse",
        expr="SUM(amount)",
        window=None if cumulative else "3 days",
        cumulative=cumulative,
        time_dimension=TimeDimensionDef.model_construct(name="order", expr="day", grain="day"),
        dimensions=[DimensionDef.model_construct(name="select", expr="region", synonyms=[])],
        filters=[],
    )
    return replace(resolved, name="trailing", definition=definition)


def test_a_reserved_alias_that_reaches_the_compiler_still_runs(tmp_path):
    layer = _trailing_layer(tmp_path)
    resolved = _reserved_named_metric(layer.resolve("trailing_3d"))
    sql, bind = compile_metric(resolved, MetricQuery(grain="week", dimensions=("select",)), "qmark")
    assert 'SUM("trailing") OVER (PARTITION BY "select" ORDER BY "order"' in sql
    assert 'DATE_TRUNC(\'week\', "order") AS "order"' in sql
    registry = ExecutionRegistry(max_workers=1)
    try:
        result = registry.run_sync(resolved.source, resolved.base_dir, sql, bind, 100)
        assert [c.name for c in result.columns] == ["order", "select", "trailing"]
        by_region = {region: value for _week, region, value in result.rows}
        assert by_region == {"us": 20, "eu": 0}

        running = _reserved_named_metric(layer.resolve("trailing_3d"), cumulative=True)
        sql, bind = compile_metric(
            running, MetricQuery(grain="day", time_range=("2026-01-02", None)), "qmark"
        )
        assert 'SUM("trailing") OVER (ORDER BY "order"' in sql
        result = registry.run_sync(running.source, running.base_dir, sql, bind, 100)
        assert [c.name for c in result.columns] == ["order", "trailing"]
        assert [value for _day, value in result.rows] == [35]
    finally:
        registry.shutdown()


def test_a_grainless_window_groups_by_the_dimension_expr_not_its_name(tmp_path):
    """The last-N path spliced the dimension *name* as a column of the base
    relation; a dimension whose expr is not its own name had no such column."""
    (tmp_path / "metrics.yaml").write_text(
        TRAILING_YAML.replace(
            "dimensions: [{name: region}]", "dimensions: [{name: market, expr: UPPER(region)}]"
        )
    )
    (tmp_path / "empty.yaml").write_text(
        "title: E\nsource: {type: duckdb, database: ':memory:'}\ntiles: []\n"
    )
    resolved = SemanticLayer(DashboardStore(tmp_path)).resolve("trailing_3d")
    sql, bind = compile_metric(resolved, MetricQuery(dimensions=("market",)), "qmark")
    assert 'UPPER(region) AS "market"' in sql
    registry = ExecutionRegistry(max_workers=1)
    try:
        result = registry.run_sync(resolved.source, resolved.base_dir, sql, bind, 100)
        assert result.rows == [["US", 20]]
    finally:
        registry.shutdown()


def test_bind_metric_refuses_an_inverted_window_on_both_input_shapes(tmp_path):
    """#361: the headless start/end shape and the dashboard filter-bar shape
    both reach compile_metric with (later, earlier) and returned NULL. The
    compare path shifts the same window, so it is covered by the first check."""
    from sqldash.params import ParamError
    from sqldash.scaffold import create_demo
    from sqldash.semantics.bind import bind_metric

    create_demo(tmp_path)
    store = DashboardStore(tmp_path / ".sqldash")
    layer = SemanticLayer(store)
    dashboard, _, _ = store.load("demo")
    with pytest.raises(ParamError, match="start '2026-09-01' is after end '2026-01-01'"):
        bind_metric(layer, "revenue", start="2026-09-01", end="2026-01-01")
    with pytest.raises(ParamError, match=r"\(from '-30d' and '2026-01-01'\)"):
        bind_metric(layer, "revenue", start="-30d", end="2026-01-01")
    with pytest.raises(ParamError, match="dates_start '2026-09-01' is after dates_end"):
        bind_metric(
            layer,
            "revenue",
            dash=dashboard,
            params={"dates_start": "2026-09-01", "dates_end": "2026-01-01"},
        )
    with pytest.raises(ParamError, match="inverted"):
        bind_metric(layer, "revenue", dash=dashboard, start="2026-09-01", end="2026-01-01")
    one_day = bind_metric(layer, "revenue", start="2026-01-01", end="2026-01-01")
    assert one_day.bind == ["2026-01-01", "2026-01-02"]
    assert bind_metric(layer, "revenue", start="2026-01-01").bind == ["2026-01-01"]


def test_compare_metric_end_only_window_does_not_blame_daterange(tmp_path):
    """A dashboard daterange still yields (None, end) on a grainless windowed
    metric, so compare has no range to shift. Do not recommend adding a
    daterange filter the author already has. #479."""
    from sqldash.params import ParamError
    from sqldash.semantics.bind import bind_metric
    from sqldash.semantics.compare import compare_metric

    create_demo(tmp_path)
    store = DashboardStore(tmp_path / ".sqldash")
    layer = SemanticLayer(store)
    dashboard, _, _ = store.load("demo")
    bound = bind_metric(layer, "trailing_28d_revenue", dash=dashboard)
    assert bound.time_range is not None
    start, end = bound.time_range
    assert start is None
    assert end is not None
    with pytest.raises(ParamError) as exc:
        compare_metric(
            "previous_period",
            bound,
            current=None,
            rebind=lambda _start, _end: bound,
            run=lambda _bound: None,
        )
    message = str(exc.value)
    assert "dashboard with a daterange filter" not in message
    assert "grain" in message or "window" in message


def test_compare_metric_without_a_range_still_asks_for_start_end(tmp_path):
    from sqldash.params import ParamError
    from sqldash.semantics.bind import bind_metric
    from sqldash.semantics.compare import compare_metric

    create_demo(tmp_path)
    store = DashboardStore(tmp_path / ".sqldash")
    layer = SemanticLayer(store)
    bound = bind_metric(layer, "revenue")
    assert bound.time_range is None
    with pytest.raises(ParamError, match="pass start/end") as exc:
        compare_metric(
            "yoy",
            bound,
            current=None,
            rebind=lambda _start, _end: bound,
            run=lambda _bound: None,
        )
    assert "dashboard with a daterange filter" in str(exc.value)


@pytest.mark.parametrize(
    ("filters", "expected"),
    [
        ("", "the dashboard has no daterange filter, so pass start/end or add one"),
        (
            "filters:\n  - {name: dates, type: daterange}\n",
            "daterange filter 'dates' has no value, so pass start/end or give it a default",
        ),
    ],
)
def test_compare_metric_scoped_to_a_dashboard_names_what_it_is_missing(tmp_path, filters, expected):
    """Scoped to a dashboard, "run inside a dashboard with a daterange filter"
    told the author to go where they already were. #516."""
    from sqldash.params import ParamError
    from sqldash.semantics.bind import bind_metric
    from sqldash.semantics.compare import compare_metric

    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "bad.yaml").write_text(
        f"title: Bad\nsource: {{type: duckdb, attach_files: true}}\n{filters}"
        "tiles:\n  - title: T\n    metric: {name: revenue, compare: previous_period}\n"
    )
    store = DashboardStore(tmp_path / ".sqldash")
    layer = SemanticLayer(store)
    dashboard, _, _ = store.load("bad")
    bound = bind_metric(layer, "revenue", dash=dashboard)
    assert bound.time_range is None
    with pytest.raises(ParamError) as exc:
        compare_metric(
            "previous_period",
            bound,
            current=None,
            rebind=lambda _start, _end: bound,
            run=lambda _bound: None,
            dash=dashboard,
        )
    assert str(exc.value) == f"compare 'previous_period' needs a time range — {expected}"


def _grain_layers(tmp_path):
    """The same 120 days of orders in a SQLite file and a DuckDB file, one metric each."""
    rows = [
        ((date(2026, 1, 1) + timedelta(days=i)).isoformat(), "eu" if i % 2 else "us", 10.0 + i)
        for i in range(120)
    ]
    con = sqlite3.connect(tmp_path / "app.db")
    con.execute("CREATE TABLE orders (order_date TEXT, region TEXT, amount REAL)")
    con.executemany("INSERT INTO orders VALUES (?, ?, ?)", rows)
    con.commit()
    con.close()
    duck = duckdb.connect(str(tmp_path / "app.duckdb"))
    duck.execute("CREATE TABLE orders (order_date DATE, region TEXT, amount DOUBLE)")
    duck.executemany("INSERT INTO orders VALUES (?, ?, ?)", rows)
    duck.close()
    layers = {}
    for kind, database in (("sqlite", "app.db"), ("duckdb", "app.duckdb")):
        root = tmp_path / kind
        root.mkdir()
        (root / "metrics.yaml").write_text(
            f"source: {{type: {kind}, database: '{tmp_path / database}'}}\n"
            "metrics:\n"
            "  revenue:\n"
            "    table: orders\n"
            "    expr: SUM(amount)\n"
            "    dimensions: [{name: region}]\n"
            "    time_dimension: {name: order_date, grain: day}\n"
            "  running:\n"
            "    table: orders\n"
            "    expr: SUM(amount)\n"
            "    cumulative: true\n"
            "    time_dimension: {name: order_date, grain: day}\n"
        )
        layers[kind] = SemanticLayer(DashboardStore(root))
    return layers


@pytest.mark.parametrize("grain", ["hour", "day", "week", "month", "quarter", "year"])
def test_sqlite_time_buckets_match_duckdb(tmp_path, grain):
    """SQLite has no DATE_TRUNC, and the plain-grain path sent it anyway, so every
    `-g` query on a SQLite source failed with `no such function` (#583)."""
    layers = _grain_layers(tmp_path)
    registry = ExecutionRegistry(max_workers=1)
    try:
        answers = {}
        for kind, layer in layers.items():
            resolved = layer.resolve("revenue")
            sql, bind = compile_metric(
                resolved, MetricQuery(grain=grain, dimensions=("region",)), "qmark"
            )
            result = registry.run_sync(resolved.source, resolved.base_dir, sql, bind, 1000)
            answers[kind] = [(str(r[0])[:10], r[1], r[2]) for r in result.rows]
    finally:
        registry.shutdown()
    assert answers["sqlite"], answers
    assert answers["sqlite"] == answers["duckdb"]


@pytest.mark.parametrize("grain", ["hour", "day", "week", "month", "quarter", "year"])
def test_sqlite_cumulative_start_keeps_the_overlapping_bucket(tmp_path, grain):
    """The cumulative start bound was `DATE_TRUNC(grain, CAST(? AS TIMESTAMP))`;
    on SQLite that CAST takes numeric affinity and '2026-02-15' becomes 2026."""
    layers = _grain_layers(tmp_path)
    registry = ExecutionRegistry(max_workers=1)
    try:
        answers = {}
        for kind, layer in layers.items():
            resolved = layer.resolve("running")
            query = MetricQuery(grain=grain, time_range=("2026-02-15", None))
            sql, bind = compile_metric(resolved, query, "qmark")
            result = registry.run_sync(resolved.source, resolved.base_dir, sql, bind, 100)
            answers[kind] = [(str(r[0])[:10], r[1]) for r in result.rows]
    finally:
        registry.shutdown()
    if grain == "month":
        assert answers["sqlite"][0] == ("2026-02-01", 2301.0), answers
    assert answers["sqlite"], answers
    assert answers["sqlite"] == answers["duckdb"]


def test_bigquery_time_buckets_use_timestamp_trunc(tmp_path):
    """BigQuery's DATE_TRUNC is argument-reversed and date-only, and its plain WEEK
    starts on Sunday where the other dialects start on Monday. SQL-string level
    only: nothing here reaches BigQuery."""
    layer = _grain_layers(tmp_path)["duckdb"]
    resolved = layer.resolve("revenue")
    month, _ = compile_metric(resolved, MetricQuery(grain="month"), "qmark", dialect="bigquery")
    assert "TIMESTAMP_TRUNC(order_date, MONTH) AS `order_date`" in month, month
    assert "DATE_TRUNC" not in month, month
    week, _ = compile_metric(resolved, MetricQuery(grain="week"), "qmark", dialect="bigquery")
    assert "TIMESTAMP_TRUNC(order_date, ISOWEEK)" in week, week
    running = layer.resolve("running")
    query = MetricQuery(grain="month", time_range=("2026-02-15", None))
    since, _ = compile_metric(running, query, "qmark", dialect="bigquery")
    assert "TIMESTAMP_TRUNC(CAST(? AS TIMESTAMP), MONTH)" in since, since
    assert "DATE_TRUNC" not in since, since


_MYSQL_FAMILY = {
    "mysql": ("SQLDASH_TEST_MYSQL", "MYSQL_HOST", "MYSQL_PORT", "MYSQL_PASSWORD"),
    "mariadb": ("SQLDASH_TEST_MARIADB", "MARIADB_HOST", "MARIADB_PORT", "MARIADB_PASSWORD"),
}


@pytest.mark.parametrize("dialect", ["mysql", "mariadb"])
@pytest.mark.parametrize("grain", ["hour", "day", "week", "month", "quarter", "year"])
def test_mysql_family_time_buckets_are_percent_free(tmp_path, dialect, grain):
    """pymysql is pyformat: a literal `%` (DATE_FORMAT patterns) is read as a
    placeholder once parameters are bound, so buckets are built without one."""
    resolved = _grain_layers(tmp_path)["duckdb"].resolve("revenue")
    sql, _ = compile_metric(resolved, MetricQuery(grain=grain), "pyformat", dialect=dialect)
    assert "DATE_TRUNC" not in sql, sql
    assert "%" not in sql.replace("%s", ""), sql


@pytest.mark.parametrize("dialect", ["mysql", "mariadb"])
def test_validating_a_mysql_family_metric_offline_does_not_ask_for_a_grain(tmp_path, dialect):
    """lint compiles at the declared grain (default day), so refusing the dialect's
    truncation failed validate_metrics for every metric with a time dimension."""
    from sqldash.lint import validate_metrics

    text = (
        f"source: {{type: {dialect}, host: localhost, database: app, username: u}}\n"
        "metrics:\n"
        "  revenue:\n"
        "    table: orders\n"
        "    expr: SUM(amount)\n"
        "    time_dimension: {name: order_date}\n"
    )
    registry = ExecutionRegistry(max_workers=1)
    try:
        store = DashboardStore(tmp_path)
        verdict = validate_metrics(
            text,
            store=store,
            layer=SemanticLayer(store),
            registry=registry,
            check_schema=False,
        )
    finally:
        registry.shutdown()
    assert verdict["valid"], verdict


@pytest.mark.parametrize("dialect", ["mysql", "mariadb"])
@pytest.mark.parametrize("grain", ["hour", "day", "week", "month", "quarter", "year"])
def test_mysql_family_time_buckets_match_duckdb(tmp_path, dialect, grain):
    flag, host, port, password = _MYSQL_FAMILY[dialect]
    if not os.environ.get(flag):
        pytest.skip(f"no {dialect} service")
    pymysql = pytest.importorskip("pymysql")

    rows = [
        ((date(2026, 1, 1) + timedelta(days=i)).isoformat(), "eu" if i % 2 else "us", 10.0 + i)
        for i in range(120)
    ]
    connect = {
        "host": os.environ.get(host, "127.0.0.1"),
        "port": int(os.environ.get(port, "3306")),
        "user": "root",
        "password": os.environ.get(password, "root"),
        "database": "sqldash_test",
    }
    con = pymysql.connect(**connect)
    try:
        with con.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS grain_orders")
            cur.execute(
                "CREATE TABLE grain_orders (order_date DATE, region VARCHAR(8), amount DOUBLE)"
            )
            cur.executemany("INSERT INTO grain_orders VALUES (%s, %s, %s)", rows)
        con.commit()
    finally:
        con.close()
    root = tmp_path / dialect
    root.mkdir()
    (root / "metrics.yaml").write_text(
        f"source: {{type: {dialect}, host: '{connect['host']}', port: {connect['port']}, "
        f"database: sqldash_test, username: root, password: '{connect['password']}'}}\n"
        "metrics:\n"
        "  revenue:\n"
        "    table: grain_orders\n"
        "    expr: SUM(amount)\n"
        "    dimensions: [{name: region}]\n"
        "    time_dimension: {name: order_date, grain: day}\n"
        "  running:\n"
        "    table: grain_orders\n"
        "    expr: SUM(amount)\n"
        "    cumulative: true\n"
        "    time_dimension: {name: order_date, grain: day}\n"
    )
    layers = {dialect: SemanticLayer(DashboardStore(root)), **_grain_layers(tmp_path)}
    registry = ExecutionRegistry(max_workers=1)
    try:
        answers = {}
        for kind in (dialect, "duckdb"):
            resolved = layers[kind].resolve("revenue")
            query = MetricQuery(grain=grain, dimensions=("region",))
            sql, bind = compile_metric(resolved, query, paramstyle_for(resolved.source))
            result = registry.run_sync(resolved.source, resolved.base_dir, sql, bind, 1000)
            answers[kind] = [(str(r[0])[:10], r[1], float(r[2])) for r in result.rows]
            running = layers[kind].resolve("running")
            query = MetricQuery(grain=grain, time_range=("2026-02-15", None))
            sql, bind = compile_metric(running, query, paramstyle_for(running.source))
            result = registry.run_sync(running.source, running.base_dir, sql, bind, 1000)
            answers[f"{kind}-running"] = [(str(r[0])[:10], float(r[1])) for r in result.rows]
    finally:
        registry.shutdown()
    assert answers[dialect], answers
    assert answers[dialect] == answers["duckdb"]
    assert answers[f"{dialect}-running"] == answers["duckdb-running"]


def _macro_layer(tmp_path):
    """A metric whose expr, dimension and static filter all bucket time inside the
    expression, the way an imported LookML timeframe measure does."""
    (tmp_path / "metrics.yaml").write_text(
        "source: {type: duckdb, database: ':memory:', attach_files: true}\n"
        "relations:\n"
        "  orders: {table: orders}\n"
        "metrics:\n"
        "  active_weeks:\n"
        "    relation: orders\n"
        "    expr: \"COUNT(DISTINCT SQLDASH_TRUNC('week', COALESCE(shipped_at, ordered_at)))\"\n"
        "    filters: [\"SQLDASH_TRUNC('year', ordered_at) >= '2026-01-01'\"]\n"
        "    time_dimension: {name: ordered_at, grain: day}\n"
        "    dimensions:\n"
        "      - {name: cohort, expr: \"SQLDASH_TRUNC('month', signed_up_at)\"}\n"
    )
    return SemanticLayer(DashboardStore(tmp_path))


@pytest.mark.parametrize(
    ("dialect", "expected"),
    [
        ("duckdb", "DATE_TRUNC('week', COALESCE(shipped_at, ordered_at))"),
        ("postgres", "DATE_TRUNC('week', COALESCE(shipped_at, ordered_at))"),
        ("snowflake", "DATE_TRUNC('week', COALESCE(shipped_at, ordered_at))"),
        ("bigquery", "TIMESTAMP_TRUNC(COALESCE(shipped_at, ordered_at), ISOWEEK)"),
        ("sqlite", "date(COALESCE(shipped_at, ordered_at), 'weekday 0', '-6 days')"),
        ("mysql", "DATE_SUB(DATE(COALESCE(shipped_at, ordered_at)), INTERVAL WEEKDAY"),
    ],
)
def test_the_trunc_macro_is_spelled_per_dialect(tmp_path, dialect, expected):
    """#628: an expression that buckets time cannot carry one dialect's DATE_TRUNC
    and still run on the next source the file is pointed at. The macro's argument
    is author SQL, so the nested COALESCE parens have to survive."""
    resolved = _macro_layer(tmp_path).resolve("active_weeks")
    sql, _ = compile_metric(resolved, MetricQuery(dimensions=("cohort",)), "qmark", dialect=dialect)
    assert expected in sql, sql
    assert "SQLDASH_TRUNC" not in sql, sql


def test_the_trunc_macro_expands_in_dimensions_and_static_filters(tmp_path):
    resolved = _macro_layer(tmp_path).resolve("active_weeks")
    sql, _ = compile_metric(
        resolved, MetricQuery(dimensions=("cohort",)), "qmark", dialect="sqlite"
    )
    assert "date(signed_up_at, 'start of month') AS \"cohort\"" in sql, sql
    assert "(date(ordered_at, 'start of year') >= '2026-01-01')" in sql, sql


def test_an_unknown_trunc_macro_grain_is_refused(tmp_path):
    (tmp_path / "metrics.yaml").write_text(
        "source: {type: duckdb, database: ':memory:', attach_files: true}\n"
        "metrics:\n"
        "  bad:\n"
        "    table: orders\n"
        "    expr: \"COUNT(DISTINCT SQLDASH_TRUNC('fortnight', ordered_at))\"\n"
    )
    resolved = SemanticLayer(DashboardStore(tmp_path)).resolve("bad")
    with pytest.raises(SemanticError, match="grain 'fortnight' is not one of"):
        compile_metric(resolved, MetricQuery(), "qmark")


def test_a_malformed_trunc_macro_is_an_error_not_silent_sql(tmp_path):
    """Left in the SQL it becomes `no such function: SQLDASH_TRUNC` from the
    warehouse, which names nothing the author can act on."""
    (tmp_path / "metrics.yaml").write_text(
        "source: {type: duckdb, database: ':memory:', attach_files: true}\n"
        "metrics:\n"
        "  bad:\n"
        "    table: orders\n"
        "    expr: COUNT(DISTINCT SQLDASH_TRUNC(week, ordered_at))\n"
    )
    resolved = SemanticLayer(DashboardStore(tmp_path)).resolve("bad")
    with pytest.raises(SemanticError, match="could not read SQLDASH_TRUNC"):
        compile_metric(resolved, MetricQuery(), "qmark")


def test_sql_that_merely_contains_the_macro_name_still_compiles(tmp_path):
    """The macro is a call, not a word. Matching the bare token rejected
    `SUM(sqldash_trunc_col)` and `= 'SQLDASH_TRUNC'` as malformed macros, so a
    column or a string literal that happened to spell it made a metric that
    compiled before uncompilable, over a name that has nothing to do with the
    macro."""
    duck = duckdb.connect(str(tmp_path / "app.duckdb"))
    duck.execute("CREATE TABLE t (sqldash_trunc_col INTEGER, amount INTEGER, label TEXT)")
    duck.executemany("INSERT INTO t VALUES (?, ?, ?)", [(1, 10, "SQLDASH_TRUNC"), (2, 20, "x")])
    duck.close()
    (tmp_path / "metrics.yaml").write_text(
        f"source: {{type: duckdb, database: '{tmp_path / 'app.duckdb'}'}}\n"
        "metrics:\n"
        "  named_like_the_macro:\n"
        "    table: t\n"
        "    expr: SUM(sqldash_trunc_col)\n"
        "  filtered_on_the_name:\n"
        "    table: t\n"
        "    expr: \"SUM(amount) FILTER (WHERE label = 'SQLDASH_TRUNC')\"\n"
    )
    layer = SemanticLayer(DashboardStore(tmp_path))
    registry = ExecutionRegistry(max_workers=1)
    try:
        answers = {}
        for name in ("named_like_the_macro", "filtered_on_the_name"):
            resolved = layer.resolve(name)
            sql, bind = compile_metric(resolved, MetricQuery(), "qmark")
            result = registry.run_sync(resolved.source, resolved.base_dir, sql, bind, 10)
            answers[name] = result.rows[0][0]
    finally:
        registry.shutdown()
    assert answers == {"named_like_the_macro": 3, "filtered_on_the_name": 10}, answers


def _commented_macro_layer(tmp_path):
    duck = duckdb.connect(str(tmp_path / "app.duckdb"))
    duck.execute("CREATE TABLE orders (amount INTEGER, ordered_at DATE, region TEXT)")
    duck.executemany(
        "INSERT INTO orders VALUES (?, ?, ?)",
        [(10, "2026-01-05", "eu"), (20, "2026-02-05", "SQLDASH_TRUNC(")],
    )
    duck.close()
    (tmp_path / "metrics.yaml").write_text(
        f"source: {{type: duckdb, database: '{tmp_path / 'app.duckdb'}'}}\n"
        "metrics:\n"
        "  line_comment:\n"
        "    table: orders\n"
        "    expr: \"SUM(amount) -- SQLDASH_TRUNC('fortnight', ordered_at)\"\n"
        "  block_comment:\n"
        "    table: orders\n"
        "    expr: \"SUM(amount) /* SQLDASH_TRUNC('fortnight', ordered_at) */\"\n"
        "  valid_grain_comment:\n"
        "    table: orders\n"
        "    expr: \"SUM(amount) -- SQLDASH_TRUNC('week', ordered_at)\"\n"
        "  string_literal:\n"
        "    table: orders\n"
        "    expr: \"SUM(CASE WHEN region = 'SQLDASH_TRUNC(' THEN amount ELSE 0 END)\"\n"
    )
    return SemanticLayer(DashboardStore(tmp_path))


@pytest.mark.parametrize("name", ["line_comment", "block_comment", "valid_grain_comment"])
def test_a_commented_out_trunc_macro_is_not_a_call(tmp_path, name):
    """#675: the expander matched raw text, so a macro the author commented out was
    expanded anyway — an unknown grain inside a `--` or `/* */` comment made the
    metric unqueryable over a grain that appears nowhere the warehouse will read."""
    resolved = _commented_macro_layer(tmp_path).resolve(name)
    sql, bind = compile_metric(resolved, MetricQuery(), "qmark")
    assert "SQLDASH_TRUNC" in sql, sql
    assert "DATE_TRUNC" not in sql, sql
    registry = ExecutionRegistry(max_workers=1)
    try:
        result = registry.run_sync(resolved.source, resolved.base_dir, sql, bind, 10)
    finally:
        registry.shutdown()
    assert result.rows[0][0] == 30, result.rows


def test_a_commented_out_trunc_macro_carries_no_not_runnable_note(tmp_path):
    """The note says the definition will not run if copied into raw SQL, and a
    definition that only mentions the macro in a comment runs fine."""
    layer = _commented_macro_layer(tmp_path)
    detail = metric_detail(layer.resolve("valid_grain_comment"))
    assert "SQLDASH_TRUNC" in detail["expr"]
    assert "expr_note" not in detail, detail


def test_the_macro_name_inside_a_string_literal_is_still_refused(tmp_path):
    """Deliberate and unchanged by #675: only comments are exempted, because
    telling a literal holding `SQLDASH_TRUNC(` from a real call would need a SQL
    parser and the token regex is not one."""
    resolved = _commented_macro_layer(tmp_path).resolve("string_literal")
    with pytest.raises(SemanticError, match="could not read SQLDASH_TRUNC"):
        compile_metric(resolved, MetricQuery(), "qmark")


def test_a_live_trunc_macro_expands_beside_a_commented_out_one(tmp_path):
    """The comment is skipped, not the whole expression."""
    (tmp_path / "metrics.yaml").write_text(
        "source: {type: duckdb, database: ':memory:'}\n"
        "metrics:\n"
        "  mixed:\n"
        "    table: orders\n"
        "    expr: \"COUNT(DISTINCT SQLDASH_TRUNC('month', ordered_at))"
        " -- was SQLDASH_TRUNC('fortnight', ordered_at)\"\n"
    )
    resolved = SemanticLayer(DashboardStore(tmp_path)).resolve("mixed")
    sql, _ = compile_metric(resolved, MetricQuery(), "qmark")
    assert "DATE_TRUNC('month', ordered_at)" in sql, sql
    assert "-- was SQLDASH_TRUNC('fortnight', ordered_at)" in sql, sql


def test_metric_detail_notes_that_the_trunc_macro_is_not_runnable(tmp_path):
    """`get_metric` over MCP and `metric show --json` feed agents that go on to
    write SQL, so an expr they cannot paste has to say so. Only when one is
    present: a note on every metric is noise."""
    (tmp_path / "metrics.yaml").write_text(
        "source: {type: duckdb, database: ':memory:'}\n"
        "metrics:\n"
        "  active_weeks:\n"
        "    table: orders\n"
        "    expr: \"COUNT(DISTINCT SQLDASH_TRUNC('week', ordered_at))\"\n"
        "  plain_revenue:\n"
        "    table: orders\n"
        "    expr: SUM(amount)\n"
    )
    layer = SemanticLayer(DashboardStore(tmp_path))
    macro = metric_detail(layer.resolve("active_weeks"))
    plain = metric_detail(layer.resolve("plain_revenue"))
    assert "SQLDASH_TRUNC" in macro["expr"]
    assert "not a warehouse function" in macro["expr_note"]
    assert "expr_note" not in plain


INLINE_RELATION_DASHBOARD = """title: Inline Relation
source: {type: duckdb, attach_files: true}
metrics:
  ir:
    relation: orders
    expr: SUM(amount)
    time_dimension: {name: order_date}
tiles:
  - title: rev
    metric: ir
"""


def test_inline_relation_cannot_name_a_project_relation_but_says_what_to_do(tmp_path):
    """An inline metric runs on its dashboard's source, so `relation:` stays
    scoped to that dashboard's own `relations:` even when metrics.yaml declares
    the name. Copying a metric out of metrics.yaml is the common way to land
    here (#646), so the refusal has to name both working bases."""
    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "rel.yaml").write_text(INLINE_RELATION_DASHBOARD)
    store = DashboardStore(tmp_path)
    assert "orders" in SemanticLayer(store).metrics_file().relations

    findings = lint_project(store, SemanticLayer(store))
    errors = [f.message for f in findings if f.level == "error"]
    assert len(errors) == 1, findings
    message = errors[0]
    assert "unknown relation 'orders'" in message
    assert "this dashboard's own 'relations:'" in message
    assert "never the project's metrics.yaml" in message
    assert "table:" in message
    assert "relations: {orders: {table: ...}}" in message
    assert "relations: {orders: {table: ...}}" in message


def test_inline_relation_declared_on_the_dashboard_resolves(tmp_path):
    """The fix the refusal recommends has to actually work, on both spellings."""
    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "rel.yaml").write_text(
        "relations:\n  orders: {table: orders}\n" + INLINE_RELATION_DASHBOARD
    )
    (tmp_path / ".sqldash" / "tbl.yaml").write_text(
        "title: Inline Table\nsource: {type: duckdb, attach_files: true}\n"
        "metrics:\n  it:\n    table: orders\n    expr: SUM(amount)\n"
        "    time_dimension: {name: order_date}\ntiles:\n  - {title: rev, metric: it}\n"
    )
    store = DashboardStore(tmp_path)
    layer = SemanticLayer(store)
    assert [f for f in lint_project(store, layer) if f.level == "error"] == []
    assert layer.resolve("ir", dashboard="rel").relation.table == "orders"
    assert layer.resolve("it", dashboard="tbl").relation.table == "orders"


def test_metrics_yaml_unknown_relation_lists_the_declared_ones():
    """metrics.yaml relations are file-scoped too, and a typo there deserves the
    same 'here is what you can name' treatment as an unknown source."""
    with pytest.raises(SemanticError) as exc:
        parse_metrics_file(
            "source: {type: duckdb}\nrelations:\n  orders: {table: orders}\n"
            "metrics:\n  m: {relation: ordres, expr: COUNT(*)}\n"
        )
    assert "unknown relation 'ordres'" in str(exc.value)
    assert "relations declared in this file: orders" in str(exc.value)
    assert "table:" in str(exc.value)
    assert "not always the table's" in str(exc.value)


QUOTE_IN_A_BRANCH = "SELECT 1 AS x {% if region %}'{% endif %} INTO pwned_tbl --'"


def _region_dashboard(tmp_path):
    (tmp_path / "d.yaml").write_text(
        "title: D\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "filters:\n"
        "  - {name: region, type: select, options: [us, eu]}\n"
        "tiles: [{id: w, sql: SELECT 1, position: {x: 0, y: 0, w: 6, h: 4}}]\n"
    )
    dash, _, _ = DashboardStore(tmp_path).load("d")
    return dash


def test_adhoc_guard_reads_the_rendered_sql_not_the_template(tmp_path):
    """The body scan ran on the template, where the quote inside `{% if %}` hid
    INTO in a string literal. With region unset the branch drops and Postgres
    ran `SELECT 1 AS x INTO pwned_tbl`."""
    dash = _region_dashboard(tmp_path)
    assert read_only_violation(QUOTE_IN_A_BRANCH, surface="ad-hoc sql") is None
    with pytest.raises(ParamError) as excinfo:
        bind_named_query(
            dash,
            QUOTE_IN_A_BRANCH,
            params={"region": "all"},
            surface="ad-hoc sql",
            scan_body=True,
        )
    assert str(excinfo.value) == "ad-hoc sql is read-only; a statement containing INTO is refused"


def test_adhoc_guard_passes_a_rendered_read(tmp_path):
    dash = _region_dashboard(tmp_path)
    sql = "SELECT 1 AS x WHERE 1 = 1 {% if region %}AND region = {{ region }}{% endif %}"
    narrowed = bind_named_query(
        dash, sql, params={"region": "us"}, surface="ad-hoc sql", scan_body=True
    )
    assert narrowed.bind == ["us"]
    everything = bind_named_query(
        dash, sql, params={"region": "all"}, surface="ad-hoc sql", scan_body=True
    )
    assert everything.bind == []
    assert "region" not in everything.sql


def _tz_layer(tmp_path, timezone):
    duck = duckdb.connect(str(tmp_path / "tz.duckdb"))
    duck.execute("CREATE TABLE events (ts TIMESTAMPTZ, amount DOUBLE)")
    duck.execute(
        "INSERT INTO events VALUES ('2026-09-10 10:00:00-08', 1), "
        "('2026-09-11 10:00:00+05:30', 10), ('2026-09-12 10:00:00+00', 100), "
        "('2026-08-15 12:00:00-08', 1000)"
    )
    duck.close()
    option = f", timezone: {timezone}" if timezone else ""
    (tmp_path / "metrics.yaml").write_text(
        f"source: {{type: duckdb, database: '{tmp_path / 'tz.duckdb'}'}}\n"
        "metrics:\n"
        "  total:\n"
        "    table: events\n"
        "    expr: SUM(amount)\n"
        f"    time_dimension: {{name: ts, grain: month{option}}}\n"
    )
    return SemanticLayer(DashboardStore(tmp_path))


def test_a_session_timezone_reads_snowflake_timestamp_tz_as_ltz_before_truncating(tmp_path):
    """Snowflake's DATE_TRUNC keeps each TIMESTAMP_TZ row's own offset, so one month
    came back as a bucket per offset (-08:00, +05:30, UTC)."""
    resolved = _tz_layer(tmp_path, "session").resolve("total")
    sql, _ = compile_metric(resolved, MetricQuery(grain="month"), "pyformat", dialect="snowflake")
    assert "DATE_TRUNC('month', CAST(ts AS TIMESTAMP_LTZ)) AS \"ts\"" in sql, sql
    (tmp_path / "plain").mkdir()
    plain = _tz_layer(tmp_path / "plain", None)
    sql, _ = compile_metric(
        plain.resolve("total"), MetricQuery(grain="month"), "pyformat", dialect="snowflake"
    )
    assert "TIMESTAMP_LTZ" not in sql, sql


@pytest.mark.parametrize("dialect", ["duckdb", "postgres", "bigquery", "sqlite", "mysql"])
def test_a_session_timezone_leaves_dialects_that_already_truncate_in_the_session_zone(
    tmp_path, dialect
):
    resolved = _tz_layer(tmp_path, "session").resolve("total")
    sql, _ = compile_metric(resolved, MetricQuery(grain="month"), "pyformat", dialect=dialect)
    assert "TIMESTAMP_LTZ" not in sql, sql


@pytest.mark.parametrize("timezone", [None, "session"])
def test_duckdb_timestamptz_months_are_one_bucket_with_or_without_the_option(tmp_path, timezone):
    pytest.importorskip("pytz", reason="DuckDB returns TIMESTAMPTZ values through pytz")
    layer = _tz_layer(tmp_path, timezone)
    resolved = layer.resolve("total")
    sql, bind = compile_metric(resolved, MetricQuery(grain="month"), "qmark")
    registry = ExecutionRegistry(max_workers=1)
    try:
        result = registry.run_sync(resolved.source, resolved.base_dir, sql, bind, 100)
    finally:
        registry.shutdown()
    assert [row[1] for row in result.rows] == [1000.0, 111.0]


def test_a_time_dimension_timezone_is_session_or_nothing():
    """Only the session zone is supported; a named zone would need a per-dialect
    conversion, and silently truncating in some other zone is the bug this avoids."""
    with pytest.raises(ValidationError, match="session"):
        MetricDef.model_validate(
            {
                "table": "events",
                "expr": "SUM(amount)",
                "time_dimension": {"name": "ts", "timezone": "UTC"},
            }
        )


_LAST_DAY = [
    ("2026-08-30 12:00:00", 1.0),
    ("2026-08-31 00:00:00", 10.0),
    ("2026-08-31 09:30:00", 100.0),
    ("2026-08-31 23:00:00", 1000.0),
    ("2026-09-01 00:00:00", 10000.0),
]


def _timestamp_layers(tmp_path):
    """Rows across the last day of August as TIMESTAMPs in DuckDB and as ISO text in
    SQLite, with a plain, a cumulative, a trailing-day and a trailing-hour metric."""
    con = sqlite3.connect(tmp_path / "ts.db")
    con.execute("CREATE TABLE events (ts TEXT, amount REAL)")
    con.executemany("INSERT INTO events VALUES (?, ?)", _LAST_DAY)
    con.commit()
    con.close()
    duck = duckdb.connect(str(tmp_path / "ts.duckdb"))
    duck.execute("CREATE TABLE events (ts TIMESTAMP, amount DOUBLE)")
    duck.executemany("INSERT INTO events VALUES (?, ?)", _LAST_DAY)
    duck.close()
    layers = {}
    for kind, database in (("sqlite", "ts.db"), ("duckdb", "ts.duckdb")):
        root = tmp_path / kind
        root.mkdir()
        (root / "metrics.yaml").write_text(
            f"source: {{type: {kind}, database: '{tmp_path / database}'}}\n"
            "metrics:\n"
            "  total: {table: events, expr: SUM(amount), time_dimension: {name: ts}}\n"
            "  running:\n"
            "    {table: events, expr: SUM(amount), cumulative: true, time_dimension: {name: ts}}\n"
            "  trailing_2d:\n"
            "    {table: events, expr: SUM(amount), window: 2 days, time_dimension: {name: ts}}\n"
            "  trailing_24h:\n"
            "    table: events\n"
            "    expr: SUM(amount)\n"
            "    window: 24 hours\n"
            "    time_dimension: {name: ts, grain: hour}\n"
        )
        layers[kind] = SemanticLayer(DashboardStore(root))
    return layers


def _run_metric(layer, name, query, width=10):
    """Rows with a leading bucket shortened to `width` characters, so a DuckDB
    timestamp and a SQLite date string compare equal."""
    resolved = layer.resolve(name)
    sql, bind = compile_metric(resolved, query, "qmark")
    registry = ExecutionRegistry(max_workers=1)
    try:
        result = registry.run_sync(resolved.source, resolved.base_dir, sql, bind, 100)
    finally:
        registry.shutdown()
    if not result.rows or len(result.rows[0]) == 1:
        return result.rows
    return [[str(row[0]).replace("T", " ")[:width], *row[1:]] for row in result.rows]


@pytest.mark.parametrize("kind", ["duckdb", "sqlite"])
def test_a_date_only_end_keeps_the_rest_of_that_day(tmp_path, kind):
    """`ts <= '2026-08-31'` stopped at that day's midnight, so 09:30 and 23:00 were
    dropped from a range the caller said ends on the 31st: 10 instead of 1110."""
    layer = _timestamp_layers(tmp_path)[kind]
    one_day = MetricQuery(time_range=("2026-08-31", "2026-08-31"))
    assert _run_metric(layer, "total", one_day) == [[1110.0]]
    by_day = MetricQuery(grain="day", time_range=("2026-08-30", "2026-08-31"))
    assert _run_metric(layer, "total", by_day) == [["2026-08-30", 1.0], ["2026-08-31", 1110.0]]
    instant = MetricQuery(time_range=("2026-08-31", "2026-08-31 12:00:00"))
    assert _run_metric(layer, "total", instant) == [[110.0]]


def test_a_date_only_end_keeps_the_rest_of_that_day_in_windowed_metrics(tmp_path):
    layer = _timestamp_layers(tmp_path)["duckdb"]
    day = ("2026-08-31", "2026-08-31")
    assert _run_metric(layer, "running", MetricQuery(grain="day", time_range=day)) == [
        ["2026-08-31", 1111.0]
    ]
    as_of = MetricQuery(time_range=(None, "2026-08-31"))
    assert _run_metric(layer, "trailing_2d", as_of) == [[1111.0]]
    assert _run_metric(layer, "trailing_24h", as_of) == [[1110.0]]
    assert _run_metric(layer, "trailing_2d", MetricQuery(grain="day", time_range=day)) == [
        ["2026-08-31", 1111.0]
    ]
    hours = _run_metric(layer, "trailing_24h", MetricQuery(grain="hour", time_range=day), width=16)
    assert len(hours) == 24
    assert hours[-1] == ["2026-08-31 23:00", 1110.0]


@pytest.mark.parametrize(
    ("dialect", "spine_end"),
    [
        ("duckdb", "(CAST(? AS TIMESTAMP)) - INTERVAL '1 hours'"),
        ("postgres", "(CAST(%s AS TIMESTAMP)) - INTERVAL '1 hours'"),
        ("snowflake", "DATEADD('hour', -1, CAST(%s AS TIMESTAMP))"),
        ("bigquery", "TIMESTAMP_SUB(CAST(%s AS TIMESTAMP), INTERVAL 1 HOUR)"),
    ],
)
def test_a_date_only_end_is_the_next_day_exclusive_on_every_spine_dialect(
    tmp_path, dialect, spine_end
):
    resolved = _timestamp_layers(tmp_path)["duckdb"].resolve("trailing_24h")
    style = "qmark" if dialect == "duckdb" else "pyformat"
    sql, bind = compile_metric(
        resolved,
        MetricQuery(grain="hour", time_range=("2026-08-31", "2026-08-31")),
        style,
        dialect=dialect,
    )
    mark = "?" if dialect == "duckdb" else "%s"
    assert f"ts < {mark}" in sql, sql
    assert f"ts <= {mark}" not in sql, sql
    assert spine_end in sql, sql
    assert set(bind) == {"2026-08-31", "2026-09-01"}


@pytest.mark.parametrize("end", ["9999-12-31", date.max])
def test_the_last_representable_end_day_keeps_an_inclusive_bound(layer, end):
    sql, bind = compile_metric(rev(layer), MetricQuery(time_range=("2026-01-01", end)), "qmark")
    assert "order_date <= ?" in sql
    assert bind == ["2026-01-01", end]
