import io
import json

import duckdb
import pytest
from ruamel.yaml import YAML
from typer.testing import CliRunner

from sqldash.cli import app
from sqldash.lint import lint_project
from sqldash.models.semantics import MetricsFile
from sqldash.params import ParamError
from sqldash.project.store import DashboardStore
from sqldash.scaffold import create_demo
from sqldash.semantics import SemanticError, SemanticLayer
from sqldash.semantics.cortex import build_semantic_view, parse_semantic_view, render_yaml
from sqldash.semantics.layer import parse_metrics_file

SNOWFLAKE_METRICS = """
source:
  type: snowflake
  account: acme-xy123
  warehouse: WH
  database: ANALYTICS
  schema: PUBLIC
  username: u@acme.com
relations:
  orders: {table: ORDERS}
metrics:
  revenue:
    title: Revenue
    description: Total order revenue in USD
    relation: orders
    expr: SUM(amount)
    synonyms: [sales]
    time_dimension: {name: order_date, grain: day}
    dimensions:
      - {name: region, description: Sales region}
      - {name: category}
  refund_rate:
    table: FINANCE.REFUNDS.DAILY
    expr: AVG(refund_pct)
    filters: ["refund_pct IS NOT NULL"]
"""

SNOWFLAKE_DASH = """
title: Revenue Overview
source:
  type: snowflake
  account: acme-xy123
  database: ANALYTICS
  schema: PUBLIC
  username: u@acme.com
filters:
  - {name: region, type: select, default: us, options: [us, eu]}
queries:
  top_regions: |
    SELECT region, SUM(amount) FROM orders WHERE region = {{ region }} GROUP BY 1
tiles:
  - id: w1
    title: Top regions by revenue
    query: top_regions
    position: {x: 0, y: 0, w: 6, h: 4}
"""


@pytest.fixture
def snowflake_project(tmp_path):
    (tmp_path / "metrics.yaml").write_text(SNOWFLAKE_METRICS)
    (tmp_path / "overview.yaml").write_text(SNOWFLAKE_DASH)
    store = DashboardStore(tmp_path)
    return SemanticLayer(store), store


def test_export_structure(snowflake_project):
    layer, store = snowflake_project
    doc, _warnings = build_semantic_view(layer, store, "acme_analytics", "Acme metrics")
    orders = next(t for t in doc["tables"] if t["name"] == "orders")
    assert orders["base_table"] == {"database": "ANALYTICS", "schema": "PUBLIC", "table": "ORDERS"}
    assert {d["name"] for d in orders["dimensions"]} == {"region", "category"}
    revenue = next(m for m in orders["metrics"] if m["name"] == "revenue")
    assert revenue["expr"] == "SUM(amount)"
    refunds = next(t for t in doc["tables"] if t["name"] == "DAILY")
    assert refunds["base_table"]["database"] == "FINANCE"
    verified = {q["name"]: q for q in doc["verified_queries"]}
    assert "region = 'us'" in verified["overview_top_regions"]["sql"]


def test_export_requires_snowflake(tmp_path):
    create_demo(tmp_path)
    store = DashboardStore(tmp_path)
    with pytest.raises(SemanticError, match="snowflake"):
        build_semantic_view(SemanticLayer(store), store, "demo")


def test_export_warns_when_cumulative_window_or_derived_cannot_round_trip(tmp_path):
    (tmp_path / "metrics.yaml").write_text(
        "source: {type: snowflake, account: a, database: D, schema: S, username: u}\n"
        "relations:\n  orders: {table: D.S.ORDERS}\n"
        "metrics:\n"
        "  revenue: {relation: orders, expr: SUM(amount),"
        " time_dimension: {name: d, expr: d, grain: day}}\n"
        "  order_count: {relation: orders, expr: COUNT(*)}\n"
        "  cumulative_revenue: {relation: orders, expr: SUM(amount), cumulative: true,"
        " time_dimension: {name: d, expr: d, grain: day}}\n"
        "  trailing_28d: {relation: orders, expr: SUM(amount), window: 28 days,"
        " time_dimension: {name: d, expr: d, grain: day}}\n"
        '  aov: {derived: "{revenue} / NULLIF({order_count}, 0)",'
        " time_dimension: {name: d, expr: d, grain: day}}\n"
    )
    store = DashboardStore(tmp_path)
    _, warnings = build_semantic_view(SemanticLayer(store), store, "v")
    text = "\n".join(warnings)
    assert "cumulative_revenue" in text, text
    assert "cumulative" in text, text
    assert "trailing_28d" in text, text
    assert "window" in text, text
    assert "aov" in text, text
    assert "derived" in text, text


def test_lossy_warning_only_fires_when_the_metric_is_exported(tmp_path):
    """A cumulative metric we skip (unqualifiable table) is not a round-trip loss."""
    (tmp_path / "metrics.yaml").write_text(
        "source: {type: snowflake, account: a, username: u}\n"
        "metrics:\n"
        "  revenue: {table: D.S.ORDERS, expr: SUM(amount),"
        " time_dimension: {name: d, expr: d, grain: day}}\n"
        "  cumulative_revenue: {table: ORDERS, expr: SUM(amount), cumulative: true,"
        " time_dimension: {name: d, expr: d, grain: day}}\n"
    )
    store = DashboardStore(tmp_path)
    _, warnings = build_semantic_view(SemanticLayer(store), store, "v")
    text = "\n".join(warnings)
    assert "cannot fully qualify" in text, text
    assert "cumulative_revenue" in text, text
    assert "round-trip import" not in text, text


METADATA_METRICS = """
source: {type: snowflake, account: a, database: D, schema: S, username: u}
relations:
  orders: {table: ORDERS}
metrics:
  revenue:
    title: Revenue
    description: Total order revenue
    relation: orders
    expr: SUM(amount)
    format: currency
    synonyms: [sales]
    owners: [finance]
    time_dimension: {name: ordered_at, description: When the order was placed}
  order_count: {relation: orders, expr: COUNT(*), format: number}
  margin: {relation: orders, expr: AVG(margin), format: percent}
"""


def test_export_names_the_metric_metadata_cortex_cannot_carry(tmp_path):
    (tmp_path / "metrics.yaml").write_text(METADATA_METRICS)
    store = DashboardStore(tmp_path)
    _, warnings = build_semantic_view(SemanticLayer(store), store, "v")
    assert (
        "metric 'revenue' has title, format: currency and owners, which a Cortex "
        "semantic view cannot carry; they were dropped"
    ) in warnings, warnings
    assert (
        "metric 'margin' has format: percent, which a Cortex semantic view cannot carry; "
        "it was dropped"
    ) in warnings, warnings
    assert not [w for w in warnings if w.startswith("metric 'order_count'")], warnings


def test_export_cortex_cli_prints_the_metadata_warning(tmp_path):
    (tmp_path / "metrics.yaml").write_text(METADATA_METRICS)
    out = tmp_path / "sv.yaml"
    result = CliRunner().invoke(app, ["export", "cortex", str(tmp_path), "-o", str(out)])
    assert result.exit_code == 0, result.output
    assert "metric 'revenue' has title, format: currency and owners" in result.output


@pytest.mark.parametrize("nested", [True, False])
def test_the_default_view_name_is_the_project_directory(tmp_path, nested):
    """A project keeps its files in `.sqldash/`, and the name came from that
    folder, so every project exported `name: sqldash` and each one created in a
    schema replaced the last. The help text already promised the project dir."""
    project = tmp_path / "my-shop"
    files = project / ".sqldash" if nested else project
    files.mkdir(parents=True)
    (files / "metrics.yaml").write_text(SNOWFLAKE_METRICS)
    for target in {project, files}:
        result = CliRunner().invoke(app, ["export", "cortex", str(target)])
        assert result.exit_code == 0, result.output
        assert YAML(typ="safe").load(result.stdout)["name"] == "my_shop", target


def test_time_dimension_description_round_trips(tmp_path):
    (tmp_path / "metrics.yaml").write_text(METADATA_METRICS)
    store = DashboardStore(tmp_path)
    doc, _ = build_semantic_view(SemanticLayer(store), store, "v")
    assert doc["tables"][0]["time_dimensions"][0]["description"] == "When the order was placed"
    imported, _ = parse_semantic_view(render_yaml(doc))
    revenue = imported["metrics"]["revenue"]
    assert revenue["time_dimension"]["description"] == "When the order was placed"
    assert revenue["description"] == "Total order revenue"
    assert revenue["synonyms"] == ["sales"]


SHARED_TIME = """
source: {{type: snowflake, account: a, database: D, schema: S, username: u}}
relations:
  orders: {{table: ORDERS}}
metrics:
  revenue: {{relation: orders, expr: SUM(amount), time_dimension: {{name: ordered_at{first}}}}}
  order_count: {{relation: orders, expr: COUNT(*), time_dimension: {{name: ordered_at{second}}}}}
"""


@pytest.mark.parametrize(
    ("first", "second", "kept", "warned"),
    [
        pytest.param(
            "", ", description: Placed", "Placed", False, id="only-a-later-metric-describes"
        ),
        pytest.param(", description: A", ", description: A", "A", False, id="both-agree"),
        pytest.param(", description: A", ", description: B", "A", True, id="they-disagree"),
    ],
)
def test_a_shared_time_dimension_keeps_a_description_or_names_the_loss(
    tmp_path, first, second, kept, warned
):
    """Metrics on one table share its time dimension entry. The description
    must not depend on which metric happened to be built first, and two
    that disagree lose one, which has to be said."""
    (tmp_path / "metrics.yaml").write_text(SHARED_TIME.format(first=first, second=second))
    store = DashboardStore(tmp_path)
    doc, warnings = build_semantic_view(SemanticLayer(store), store, "v")
    assert doc["tables"][0]["time_dimensions"][0]["description"] == kept
    lossy = [w for w in warnings if "different descriptions" in w]
    assert bool(lossy) == warned, warnings


def test_export_import_round_trip(snowflake_project):
    layer, store = snowflake_project
    doc, _ = build_semantic_view(layer, store, "rt")
    imported, _warnings = parse_semantic_view(render_yaml(doc))

    assert imported["relations"]["orders"] == {"table": "ANALYTICS.PUBLIC.ORDERS"}
    revenue = imported["metrics"]["revenue"]
    assert revenue["expr"] == "SUM(amount)"
    assert revenue["time_dimension"] == {"name": "order_date", "grain": "day"}
    assert {d["name"] for d in revenue["dimensions"]} == {"region", "category"}
    assert imported["metrics"]["refund_rate"]["expr"] == (
        "AVG(CASE WHEN (refund_pct IS NOT NULL) THEN refund_pct END)"
    )
    assert "filters" not in imported["metrics"]["refund_rate"]

    imported["source"] = {
        "type": "snowflake",
        "account": "acme-xy123",
        "database": "ANALYTICS",
        "schema": "PUBLIC",
        "username": "u",
    }
    validated = MetricsFile.model_validate(imported)
    assert set(validated.metrics) == {"revenue", "refund_rate"}


def test_a_non_day_grain_survives_export_import(tmp_path):
    """Export used to drop grain (the Snowflake view has no such field) and
    import hardcoded day, so month came back as day."""
    (tmp_path / "metrics.yaml").write_text(
        "source: {type: snowflake, account: a, database: D, schema: S, username: u}\n"
        "metrics:\n"
        "  a: {table: D.S.ORDERS, expr: SUM(x), "
        "time_dimension: {name: d, expr: d, grain: month}}\n"
    )
    store = DashboardStore(tmp_path)
    doc, _ = build_semantic_view(SemanticLayer(store), store, "g")
    imported, _ = parse_semantic_view(render_yaml(doc))
    assert imported["metrics"]["a"]["time_dimension"]["grain"] == "month"


def test_import_rejects_empty():
    with pytest.raises(SemanticError, match="tables"):
        parse_semantic_view("name: empty\n")


def test_import_yaml_output_shape(snowflake_project):
    layer, store = snowflake_project
    doc, _ = build_semantic_view(layer, store, "rt")
    imported, _ = parse_semantic_view(render_yaml(doc))
    text = render_yaml(imported)
    parsed = YAML(typ="safe").load(io.StringIO(text))
    assert parsed["source"]["account"] == "<your-account>"
    assert "revenue" in parsed["metrics"]


# A Cortex Analyst semantic model: raw columns under `measures`, no `metrics` key
# at all. This is the shape Snowflake's own published models use, and the shape
# the round-trip test could never produce, since the exporter writes `metrics`.
MEASURES_MODEL = """
name: Customer_Claim_Semantic_Model
tables:
  - name: CLAIMS
    base_table: {database: INSURANCEDB, schema: DATA, table: CLAIMS}
    dimensions:
      - {name: REGION, expr: REGION, synonyms: [area]}
    time_dimensions:
      - {name: CLAIM_DATE, expr: CLAIM_DATE}
    measures:
      - {name: CLAIM_AMOUNT, expr: CLAIM_AMOUNT, default_aggregation: sum}
      - {name: CLAIM_COUNT, expr: CLAIM_ID, default_aggregation: count_distinct}
      - {name: SETTLEMENT_DAYS, expr: DAYS, default_aggregation: avg}
      - {name: NO_AGG, expr: AMOUNT}
"""


def test_import_reads_measures_from_a_real_semantic_model():
    """`import cortex` read only `metrics`, so every published Snowflake model —
    which uses `measures` — failed with "no importable metrics found"."""
    out, warnings = parse_semantic_view(MEASURES_MODEL)
    assert set(out["metrics"]) == {"claim_amount", "claim_count", "settlement_days", "no_agg"}
    assert out["metrics"]["claim_amount"]["expr"] == "SUM(CLAIM_AMOUNT)"
    assert out["metrics"]["claim_count"]["expr"] == "COUNT(DISTINCT CLAIM_ID)"
    assert out["metrics"]["settlement_days"]["expr"] == "AVG(DAYS)"
    assert out["metrics"]["no_agg"]["expr"] == "SUM(AMOUNT)"
    assert out["metrics"]["claim_amount"]["relation"] == "claims"
    assert out["relations"]["claims"] == {"table": "INSURANCEDB.DATA.CLAIMS"}
    assert not [w for w in warnings if "unsupported" in w]


def test_imported_measures_carry_the_tables_dimensions_and_time():
    out, _ = parse_semantic_view(MEASURES_MODEL)
    metric = out["metrics"]["claim_amount"]
    assert metric["time_dimension"] == {"name": "claim_date", "grain": "day"}
    assert metric["dimensions"] == [{"name": "region", "synonyms": ["area"]}]


def test_import_warns_on_an_aggregation_it_cannot_map():
    view = MEASURES_MODEL.replace("default_aggregation: avg", "default_aggregation: percentile_99")
    out, warnings = parse_semantic_view(view)
    assert out["metrics"]["settlement_days"]["expr"] == "SUM(DAYS)"
    assert any("percentile_99" in w and "review" in w for w in warnings)


FACTS_VIEW = """
name: SAM_EXECUTIVE_VIEW
tables:
  - name: clients
    base_table:
      database: {{DATABASE}}
      schema: CURATED
      table: DIM_CLIENT
    facts:
      - {name: aum_with_sam, expr: AUM}
    metrics:
      - {name: average_aum, expr: AVG(aum_with_sam)}
  - name: flows
    base_table: {database: DB, schema: CURATED, table: FLOWS}
    facts:
      - {name: net_flow, expr: NET_FLOW}
"""


def test_import_keeps_deployment_placeholders_instead_of_failing_to_parse():
    """Snowflake publishes these views as deployment templates. YAML reads
    `database: {{DATABASE}}` as a mapping keyed by a mapping and dies with
    "found unhashable key", which says nothing about the real problem.
    """
    out, warnings = parse_semantic_view(FACTS_VIEW)
    assert out["relations"]["clients"] == {"table": "{{DATABASE}}.CURATED.DIM_CLIENT"}
    assert any("placeholder" in w for w in warnings)


def test_facts_become_metrics_only_when_the_table_declares_none():
    """Facts are the inputs a table's metrics aggregate. Importing them as metrics
    too would invent a metric per column beside the real one."""
    out, warnings = parse_semantic_view(FACTS_VIEW)
    assert "average_aum" in out["metrics"]
    assert "aum_with_sam" not in out["metrics"]
    assert any("facts are inputs" in w for w in warnings)
    # `flows` declares no metrics, so its fact is all there is to import.
    assert out["metrics"]["net_flow"]["expr"] == "SUM(NET_FLOW)"


# The shape that shipped broken: `measures` is not always raw columns. This is
# `portfolio_managers` from SAM_EXECUTIVE_VIEW, the file the import was verified
# against — verified by counting metrics rather than running them.
PREAGGREGATED_MEASURES = """
name: V
tables:
  - name: portfolio_managers
    base_table: {database: DB, schema: S, table: PM}
    measures:
      - {name: pm_count, expr: COUNT(DISTINCT PM_ID)}
      - {name: total_aum, expr: SUM(AUM), default_aggregation: sum}
      - {name: raw_col, expr: HEADCOUNT}
"""


def test_a_measure_that_already_aggregates_is_not_wrapped_again():
    """SUM(COUNT(DISTINCT x)) is rejected as a nested aggregate by every target
    engine, so the metric imports, lints clean, and dies at query time."""
    out, _ = parse_semantic_view(PREAGGREGATED_MEASURES)
    assert out["metrics"]["pm_count"]["expr"] == "COUNT(DISTINCT PM_ID)"
    assert out["metrics"]["total_aum"]["expr"] == "SUM(AUM)"
    assert out["metrics"]["raw_col"]["expr"] == "SUM(HEADCOUNT)"


# Every expr shape the two real Snowflake-Labs files actually produce, captured
# here so the plan-check below covers them without reaching the network: a
# pre-aggregated measure, aggregates over CASE and over a nested scalar call,
# facts feeding metrics, a facts-only table, and {{DATABASE}} placeholders.
REAL_SHAPES = """
name: SAM
tables:
  - name: clients
    base_table:
      database: {{DATABASE}}
      schema: CURATED
      table: DIM_CLIENT
    facts:
      - {name: aum_with_sam, expr: AUM}
    metrics:
      - {name: average_aum, expr: AVG(aum_with_sam)}
      - {name: client_count, expr: COUNT(DISTINCT ClientID)}
  - name: client_flows
    base_table: {database: DB, schema: CURATED, table: FLOWS}
    metrics:
      - {name: gross_inflows, expr: 'SUM(CASE WHEN FlowAmount > 0 THEN FlowAmount ELSE 0 END)'}
      - {name: largest_flow, expr: MAX(ABS(FlowAmount))}
  - name: portfolio_managers
    base_table: {database: DB, schema: CURATED, table: PM}
    measures:
      - {name: pm_count, expr: COUNT(DISTINCT PM_ID)}
      - {name: pm_aum_capacity, expr: AUM_CAPACITY}
  - name: flows_only
    base_table: {database: DB, schema: CURATED, table: F}
    facts:
      - {name: net_flow, expr: NET_FLOW}
"""


def _plan(expr):
    """Ask duckdb to plan the expression, with every identifier it mentions
    supplied as a column, so a real binder error is the only way to fail."""
    import re

    import duckdb

    reserved = {
        "SUM",
        "COUNT",
        "AVG",
        "MIN",
        "MAX",
        "MEDIAN",
        "DISTINCT",
        "CASE",
        "WHEN",
        "THEN",
        "ELSE",
        "END",
        "ABS",
        "AND",
        "OR",
        "NOT",
        "NULL",
    }
    columns = {
        i for i in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\b", expr) if i.upper() not in reserved
    }
    select = ", ".join(f"1 AS {c}" for c in sorted(columns)) or "1 AS x"
    duckdb.sql(f"SELECT {expr} AS m FROM (SELECT {select}) t")


@pytest.mark.parametrize("view", [PREAGGREGATED_MEASURES, REAL_SHAPES])
def test_every_imported_expr_is_a_query_the_engine_will_plan(view):
    """Counting imported metrics is not evidence they run — that gap is exactly
    how the nested aggregate shipped. Plan each one instead, over the real
    shapes and not only a toy fixture."""
    out, _ = parse_semantic_view(view)
    assert out["metrics"]
    for metric in out["metrics"].values():
        _plan(metric["expr"])


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        ("COUNT_IF(x > 0)", "COUNT_IF(x > 0)"),
        ("SUM_IF(x, 1)", "SUM_IF(x, 1)"),
        ("APPROX_PERCENTILE(x, 0.5)", "APPROX_PERCENTILE(x, 0.5)"),
        ("MODE(x)", "MODE(x)"),
        ("MIN_BY(a, b)", "MIN_BY(a, b)"),
        ("BOOLAND_AGG(x)", "BOOLAND_AGG(x)"),
        ("ROW_NUMBER() OVER (PARTITION BY x)", "ROW_NUMBER() OVER (PARTITION BY x)"),
        ("AMOUNT", "SUM(AMOUNT)"),
        ("t.AMOUNT", "SUM(t.AMOUNT)"),
        ("PRICE * QTY", "SUM(PRICE * QTY)"),
    ],
)
def test_only_expressions_that_do_not_already_aggregate_get_wrapped(expr, expected):
    """Naming the aggregates one by one missed COUNT_IF and its family — the same
    double-wrap, one function further out. Anything already aggregating (or
    windowed, which also cannot be nested) is kept verbatim."""
    view = (
        "name: V\ntables:\n  - name: t\n"
        "    base_table: {database: DB, schema: S, table: T}\n"
        f"    measures: [{{name: m, expr: '{expr}'}}]\n"
    )
    out, _ = parse_semantic_view(view)
    assert out["metrics"]["m"]["expr"] == expected


def test_wrapping_an_unrecognized_function_call_is_reported():
    """SUM(COALESCE(x, 0)) is right, but it is a guess — and it is the guess most
    likely to be wrong, so the caller hears about it."""
    view = (
        "name: V\ntables:\n  - name: t\n"
        "    base_table: {database: DB, schema: S, table: T}\n"
        "    measures: [{name: m, expr: 'COALESCE(x, 0)'}]\n"
    )
    out, warnings = parse_semantic_view(view)
    assert out["metrics"]["m"]["expr"] == "SUM(COALESCE(x, 0))"
    assert any("does not recognize as an aggregate" in w for w in warnings)


def test_a_malformed_base_table_is_reported_not_a_traceback():
    """Placeholder quoting turns a `{{...}}` flow mapping into a string, which
    reached .get() and raised AttributeError past the CLI's clean error path."""
    from sqldash.semantics import SemanticError

    with pytest.raises(SemanticError):
        parse_semantic_view(
            "name: V\ntables:\n  - name: t\n"
            "    base_table: {{database: DB, schema: S, table: T}}\n"
            "    measures: [{name: m, expr: A}]\n"
        )


def test_an_explicit_aggregation_on_an_aggregated_expr_is_reported():
    out, warnings = parse_semantic_view(
        PREAGGREGATED_MEASURES.replace(
            "{name: pm_count, expr: COUNT(DISTINCT PM_ID)}",
            "{name: pm_count, expr: COUNT(DISTINCT PM_ID), default_aggregation: avg}",
        )
    )
    assert out["metrics"]["pm_count"]["expr"] == "COUNT(DISTINCT PM_ID)"
    assert any("already aggregates" in w for w in warnings)


def test_measures_beside_metrics_are_skipped_like_facts_are():
    """The two keys mean the same thing to a table that declares its own
    metrics; excluding one and not the other was an accident of which shape
    got implemented first."""
    view = """
name: V
tables:
  - name: t
    base_table: {database: DB, schema: S, table: T}
    measures:
      - {name: raw, expr: AMOUNT}
    metrics:
      - {name: real_metric, expr: SUM(AMOUNT)}
"""
    out, warnings = parse_semantic_view(view)
    assert set(out["metrics"]) == {"real_metric"}
    assert any("measures are inputs" in w for w in warnings)


def test_a_placeholder_keeps_its_trailing_comment_out_of_the_value():
    out, _ = parse_semantic_view(
        "name: V\ntables:\n"
        "  - name: t\n"
        "    base_table:\n"
        "      database: {{DATABASE}}  # set at deploy time\n"
        "      schema: S\n"
        "      table: T\n"
        "    measures: [{name: m, expr: AMOUNT}]\n"
    )
    assert out["relations"]["t"] == {"table": "{{DATABASE}}.S.T"}


FACT_ALIASES = """
name: V
tables:
  - name: t
    base_table: {database: DB, schema: S, table: T}
    facts:
      - {name: fees_ratio, expr: 'FEES / AUM'}
      - {name: aum, expr: AUM_COL}
      - {name: same_name, expr: same_name}
      - {name: count, expr: N_ROWS}
    metrics:
      - {name: avg_fees_ratio, expr: AVG(fees_ratio)}
      - {name: total_aum, expr: SUM(aum)}
      - {name: rows, expr: COUNT(*)}
      - {name: rows_lower, expr: 'count(FEES)'}
      - {name: plain, expr: SUM(same_name)}
      - {name: counted, expr: SUM(count)}
"""


def test_metric_exprs_inline_the_facts_they_are_written_over():
    """A view's facts are aliases scoped to the view, not columns of the base
    table. Importing `AVG(fees_ratio)` verbatim after dropping the fact that
    defines it yields a metric the warehouse cannot bind — and the plan-check
    cannot see it, because it supplies every identifier as a column.
    """
    out, _ = parse_semantic_view(FACT_ALIASES)
    assert out["metrics"]["avg_fees_ratio"]["expr"] == "AVG((FEES / AUM))"
    assert out["metrics"]["total_aum"]["expr"] == "SUM(AUM_COL)"
    assert out["metrics"]["plain"]["expr"] == "SUM(same_name)"


def test_inlining_leaves_function_names_alone():
    """The fixture defines a fact literally named `count`, so without the
    "not followed by (" guard the substitution rewrites the COUNT function
    itself and every expr using it becomes nonsense."""
    out, _ = parse_semantic_view(FACT_ALIASES)
    # `rows` is a SQL reserved word, so the import renames the key (#337).
    assert out["metrics"]["rows_metric"]["expr"] == "COUNT(*)"
    # SQL is case-insensitive, so the guard has to hold for a lowercase call too.
    assert out["metrics"]["rows_lower"]["expr"] == "count(FEES)"
    # ...while a real reference to that fact still inlines.
    assert out["metrics"]["counted"]["expr"] == "SUM(N_ROWS)"


def test_inlined_metrics_bind_against_the_real_columns():
    """The plan-check supplies every identifier as a column, so it passes either
    way. Binding against only the columns the base table actually has is what
    distinguishes an inlined expr from a dangling alias."""
    import duckdb

    out, _ = parse_semantic_view(FACT_ALIASES)
    real_columns = "1 AS FEES, 2 AS AUM, 3 AS AUM_COL, 4 AS same_name, 5 AS N_ROWS"
    for metric in out["metrics"].values():
        duckdb.sql(f"SELECT {metric['expr']} AS m FROM (SELECT {real_columns}) t")


@pytest.mark.parametrize(
    "base_table",
    [
        "\n      database: {{DATABASE}}\n      schema: S\n      table: T",
        "\n      database: {{DATABASE}}  # set at deploy time\n      schema: S\n      table: T",
        '\n      database: "{{DATABASE}}"\n      schema: S\n      table: T',
        " {database: {{DATABASE}}, schema: S, table: T}",
    ],
)
def test_every_placeholder_shape_parses(base_table):
    """The nested flow-mapping form still died with the same "found unhashable
    key" this function exists to remove."""
    out, _ = parse_semantic_view(
        f"name: V\ntables:\n  - name: t\n    base_table:{base_table}\n"
        "    measures: [{name: m, expr: A}]\n"
    )
    assert out["relations"]["t"] == {"table": "{{DATABASE}}.S.T"}


def test_placeholders_inside_a_block_scalar_are_left_alone():
    """A verified query's SQL may legitimately contain `{{ param }}`; quoting
    inside it would corrupt the query."""
    from sqldash.semantics.cortex import _quote_placeholders

    text = "verified_queries:\n  - name: q\n    sql: |\n      SELECT * WHERE d >= {{ start }}\n"
    out, count = _quote_placeholders(text)
    assert count == 0
    assert out == text


def test_a_quoted_value_containing_a_placeholder_is_left_alone():
    """Every verified query in the real file is a quoted `sql:` string holding
    {{DATABASE}}. It already parses — quoting the placeholder inside it injects
    quotes into the middle of the string and corrupts the query."""
    from sqldash.semantics.cortex import _quote_placeholders

    text = '  - name: q\n    sql: "SELECT * FROM SEMANTIC_VIEW({{DATABASE}}.AI.V METRICS m)"\n'
    out, count = _quote_placeholders(text)
    assert count == 0
    assert out == text


def test_a_plain_scalar_merely_containing_a_placeholder_is_left_alone():
    """`sql: SELECT ... ({{DATABASE}}.x)` is a valid YAML plain scalar already."""
    from sqldash.semantics.cortex import _quote_placeholders

    text = "    sql: SELECT * FROM SEMANTIC_VIEW({{DATABASE}}.AI.V METRICS m)\n"
    out, count = _quote_placeholders(text)
    assert count == 0
    assert out == text


@pytest.mark.parametrize(
    "database",
    ["{{DATABASE}}", '"{{DATABASE}}"', "'{{DATABASE}}'"],
)
def test_a_flow_mapping_placeholder_survives_however_it_was_written(database):
    """The flow branch quoted placeholders without checking whether they were
    already quoted, so `{database: "{{DATABASE}}"}` became `""{{DATABASE}}""`
    and failed to parse — a regression on input that was valid before."""
    out, _ = parse_semantic_view(
        "name: V\ntables:\n  - name: t\n"
        f"    base_table: {{database: {database}, schema: S, table: T}}\n"
        "    measures: [{name: m, expr: A}]\n"
    )
    assert out["relations"]["t"] == {"table": "{{DATABASE}}.S.T"}


def test_inlining_does_not_reach_inside_a_string_literal():
    """A fact named `count` and a comparison against the literal 'count' are
    both plausible. Rewriting the literal changes what the metric computes, and
    nothing downstream can tell."""
    out, _ = parse_semantic_view(
        "name: V\ntables:\n  - name: t\n"
        "    base_table: {database: D, schema: S, table: T}\n"
        "    facts: [{name: count, expr: N_ROWS}]\n"
        "    metrics:\n"
        "      - {name: m, expr: \"SUM(CASE WHEN status = 'count' THEN 1 ELSE 0 END)\"}\n"
        "      - {name: n, expr: SUM(count)}\n"
    )
    assert out["metrics"]["m"]["expr"] == "SUM(CASE WHEN status = 'count' THEN 1 ELSE 0 END)"
    assert out["metrics"]["n"]["expr"] == "SUM(N_ROWS)"


def test_a_fact_written_over_another_fact_is_fully_inlined():
    """One substitution pass leaves the inner alias dangling against the base
    table — clean import, query-time failure, the exact shape this exists to
    prevent."""
    import duckdb

    out, _ = parse_semantic_view(
        "name: V\ntables:\n  - name: t\n"
        "    base_table: {database: D, schema: S, table: T}\n"
        "    facts:\n      - {name: a, expr: 'b + 1'}\n      - {name: b, expr: C_COL}\n"
        "    metrics: [{name: m, expr: SUM(a)}]\n"
    )
    assert out["metrics"]["m"]["expr"] == "SUM((C_COL + 1))"
    duckdb.sql(f"SELECT {out['metrics']['m']['expr']} AS m FROM (SELECT 1 AS C_COL) t")


def test_facts_defined_in_a_cycle_are_reported_not_looped_on():
    _out, warnings = parse_semantic_view(
        "name: V\ntables:\n  - name: t\n"
        "    base_table: {database: D, schema: S, table: T}\n"
        "    facts:\n      - {name: a, expr: 'b + 1'}\n      - {name: b, expr: 'a * 2'}\n"
        "    metrics: [{name: m, expr: SUM(a)}]\n"
    )
    assert any("cycle" in w for w in warnings), warnings


def test_a_bare_string_table_is_reported_not_a_traceback():
    """`tables: [ORDERS]` used to AttributeError at first_base. #336."""
    with pytest.raises(SemanticError, match="each entry in 'tables' must be a mapping"):
        parse_semantic_view("name: V\ntables:\n  - ORDERS\n")


def test_a_bare_string_dimension_is_reported_not_a_traceback():
    """`dimensions: [REGION]` used to AttributeError at d.get. #336."""
    with pytest.raises(SemanticError, match="each entry in 'dimensions' must be a mapping"):
        parse_semantic_view(
            "name: V\ntables:\n  - name: ORDERS\n"
            "    base_table: {database: DB, schema: S, table: ORDERS}\n"
            "    dimensions:\n      - REGION\n"
            "    metrics: [{name: revenue, expr: SUM(AMOUNT)}]\n"
        )


@pytest.mark.parametrize(
    ("key", "entry"),
    [
        ("facts", "      - just_a_string\n    metrics: [{name: m, expr: SUM(A)}]\n"),
        ("measures", "      - AMOUNT\n"),
        ("metrics", "      - just_a_string\n"),
    ],
)
def test_a_non_mapping_entry_is_reported_not_a_traceback(key, entry):
    """`.get()` on a string raises past the CLI's SemanticError handling into a
    full traceback — the "helper raising outside the try" shape. A shorthand
    `facts: [AMOUNT]` is a plausible thing for a generator to emit."""
    from sqldash.semantics import SemanticError

    view = (
        "name: V\ntables:\n  - name: t\n"
        "    base_table: {database: D, schema: S, table: T}\n"
        f"    {key}:\n{entry}"
    )
    try:
        _, warnings = parse_semantic_view(view)
    except SemanticError:
        return  # the clean CLI path is also an acceptable answer
    assert any("not a mapping" in w for w in warnings), warnings


def test_a_block_scalar_line_that_looks_like_a_key_is_left_alone():
    """Anchoring to a `key:` is not enough: block-scalar content can look like
    one, and rewriting inside it corrupts whatever the block holds."""
    from sqldash.semantics.cortex import _quote_placeholders

    text = "x:\n  sql: |\n    config: {{ env }}\n    SELECT 1\ndatabase: {{DATABASE}}\n"
    out, count = _quote_placeholders(text)
    assert count == 1
    assert "    config: {{ env }}\n" in out
    assert 'database: "{{DATABASE}}"' in out


def test_a_facts_only_table_inlines_its_chained_facts_too():
    """Alias resolution ran only for tables declaring metrics. A facts-only
    table turns those same names into metrics, so a fact defined over another
    fact imports clean, lints clean, and dies at query time — one branch away
    from the case already covered."""
    import duckdb

    out, _ = parse_semantic_view(
        "name: V\ntables:\n  - name: t\n"
        "    base_table: {database: D, schema: S, table: T}\n"
        "    facts:\n      - {name: a, expr: 'b + 1'}\n      - {name: b, expr: C_COL}\n"
    )
    assert out["metrics"]["a"]["expr"] == "SUM(C_COL + 1)"
    for metric in out["metrics"].values():
        duckdb.sql(f"SELECT {metric['expr']} AS m FROM (SELECT 1 AS C_COL) t")


def test_an_aggregate_inside_a_string_literal_is_not_an_aggregate():
    """Reading `'SUM(x)'` inside a literal made the importer treat the whole
    expression as pre-aggregated and emit a metric with no aggregate at all —
    while the inlining right below already went to lengths to stay out of
    quotes."""
    out, _ = parse_semantic_view(
        "name: V\ntables:\n  - name: t\n"
        "    base_table: {database: D, schema: S, table: T}\n"
        "    measures: [{name: m, expr: \"CASE WHEN f('SUM(x)') THEN 1 ELSE 0 END\"}]\n"
    )
    assert out["metrics"]["m"]["expr"] == "SUM(CASE WHEN f('SUM(x)') THEN 1 ELSE 0 END)"


def test_a_placeholder_in_a_flow_sequence_parses():
    """A flow sequence fails for the same reason a flow mapping does; the
    docstring claimed only two shapes break."""
    out, _ = parse_semantic_view(
        "name: V\ntags: [{{DATABASE}}, prod]\ntables:\n  - name: t\n"
        "    base_table: {database: D, schema: S, table: T}\n"
        "    measures: [{name: m, expr: A}]\n"
    )
    assert out["metrics"]["m"]["expr"] == "SUM(A)"


@pytest.mark.parametrize(
    ("expr", "wrapped", "warns"),
    [
        # Named: certain, so nothing is said.
        ("CORR(a, b)", False, False),
        ("KURTOSIS(a)", False, False),
        ("GROUPING_ID(a)", False, False),
        ("COUNT(DISTINCT a)", False, False),
        # Shaped like an aggregate: probably right, but a guess — so audible.
        ("COUNT_IF(x > 0)", False, True),
        ("MY_FUNC_IF(x)", False, True),
        ("BOOLAND_AGG(x)", False, True),
        # Plain column: the case we are confident about.
        ("AMOUNT", True, False),
        # An unrecognized call: wrapped, and said so.
        ("COALESCE(x, 0)", True, True),
    ],
)
def test_aggregate_classification_says_when_it_is_guessing(expr, wrapped, warns):
    """CORR and KURTOSIS are real aggregates the named list omitted, so they were
    double-wrapped into SQL no engine accepts. And a scalar UDF shaped like
    `\\w+_IF` was trusted silently, emitting a metric with no aggregate at all —
    while an unrecognized call one branch over was already warned about.
    """
    out, warnings = parse_semantic_view(
        "name: V\ntables:\n  - name: t\n"
        "    base_table: {database: D, schema: S, table: T}\n"
        f"    measures: [{{name: m, expr: '{expr}'}}]\n"
    )
    assert out["metrics"]["m"]["expr"] == (f"SUM({expr})" if wrapped else expr)
    mentions = [w for w in warnings if "measure 'm'" in w]
    assert bool(mentions) is warns, warnings
    if warns:
        # The two warnings say opposite things; asserting only that one exists
        # let a mutation swap them and still pass.
        expected = "was wrapped" if wrapped else "left unwrapped"
        assert expected in mentions[0], mentions


def test_a_hash_inside_a_quoted_string_is_not_a_comment():
    """Cutting the value at the first `#` dropped the rest of the flow mapping,
    leaving its placeholder unquoted — the very parse failure this removes."""
    out, _ = parse_semantic_view(
        "name: V\ntables:\n  - name: t\n"
        '    base_table: {database: {{X}}, schema: "a#b", table: T}\n'
        "    measures: [{name: m, expr: A}]\n"
    )
    assert out["relations"]["t"] == {"table": "{{X}}.a#b.T"}


def test_a_spaced_hash_inside_a_quoted_string_is_not_a_comment_either():
    """A `#` with no space before it is not a comment anywhere. The quoted-span
    check is what covers the one that does follow a space."""
    from sqldash.semantics.cortex import _split_comment

    value = '{database: {{X}}, schema: "a #b", table: T}'
    assert _split_comment(value) == (value, "")
    assert _split_comment("{{X}}  # real comment") == ("{{X}}", "# real comment")


def test_a_block_header_with_a_quoted_key_is_still_a_block():
    """`"sql": |` was not recognized as a block header, so its SQL was rewritten
    as if it were YAML."""
    from sqldash.semantics.cortex import _quote_placeholders

    text = '"sql": |\n  database: {{DATABASE}}\n  SELECT 1\n'
    out, count = _quote_placeholders(text)
    assert count == 0
    assert out == text


def test_cortex_export_skips_an_ambiguous_metric_rather_than_publishing_one_definition(tmp_path):
    """The seventh all_metrics() consumer. This document is executed by
    SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML and answered from, so emitting one of
    two conflicting definitions publishes a claim sqldash itself refuses.
    """
    from sqldash.project.store import DashboardStore
    from sqldash.semantics import SemanticLayer
    from sqldash.semantics.cortex import build_semantic_view

    (tmp_path / "metrics.yaml").write_text(
        "source: {type: snowflake, account: a, username: u, database: DB, schema: S}\n"
        "relations:\n  t: {table: DB.S.T}\n"
        "metrics:\n  good_rev: {relation: t, expr: SUM(amount)}\n"
    )
    for n, agg in (("a", "SUM"), ("b", "AVG")):
        (tmp_path / f"{n}.yaml").write_text(
            f"title: Dash {n}\n"
            "source: {type: snowflake, account: a, username: u, database: DB, schema: S}\n"
            f"metrics:\n  inline_rev: {{table: DB.S.T, expr: '{agg}(amount)'}}\n"
            "tiles: [{id: w, metric: inline_rev}]\n"
        )
    store = DashboardStore(tmp_path)
    doc, warnings = build_semantic_view(SemanticLayer(store), store, "v")
    exported = {m["name"] for t in doc["tables"] for m in t.get("metrics", [])}
    assert exported == {"good_rev"}
    assert any("inline_rev" in w and "no single definition" in w for w in warnings)


def test_cortex_export_says_why_when_everything_was_skipped(tmp_path):
    """ "no snowflake-source metrics" sends someone whose metrics are all
    snowflake — but ambiguous — to fix the wrong thing."""
    from sqldash.project.store import DashboardStore
    from sqldash.semantics import SemanticError, SemanticLayer
    from sqldash.semantics.cortex import build_semantic_view

    for n, agg in (("a", "SUM"), ("b", "AVG")):
        (tmp_path / f"{n}.yaml").write_text(
            f"title: Dash {n}\n"
            "source: {type: snowflake, account: a, username: u, database: DB, schema: S}\n"
            f"metrics:\n  inline_rev: {{table: DB.S.T, expr: '{agg}(amount)'}}\n"
            "tiles: [{id: w, metric: inline_rev}]\n"
        )
    store = DashboardStore(tmp_path)
    with pytest.raises(SemanticError, match="no single definition"):
        build_semantic_view(SemanticLayer(store), store, "v")


SNOWFLAKE_DERIVED = """
source: {type: snowflake, account: a, username: u, database: ANALYTICS, schema: PUBLIC}
relations:
  orders: {table: ORDERS}
metrics:
  revenue: {relation: orders, expr: SUM(amount)}
  order_count: {relation: orders, expr: COUNT(*)}
  avg_order_value: {derived: "{revenue} / NULLIF({order_count}, 0)"}
"""


def test_a_derived_metric_lands_in_its_peers_logical_table(tmp_path):
    """Expansion clears a derived metric's `relation:`, so grouping by the name
    the metric carried put it in a second logical table over the same
    base_table as the metrics it is derived from — the README's canonical
    pattern producing malformed interop output, silently.
    """
    from sqldash.project.store import DashboardStore
    from sqldash.semantics import SemanticLayer
    from sqldash.semantics.cortex import build_semantic_view

    (tmp_path / "metrics.yaml").write_text(SNOWFLAKE_DERIVED)
    store = DashboardStore(tmp_path)
    doc, _ = build_semantic_view(SemanticLayer(store), store, "v")
    assert len(doc["tables"]) == 1, doc["tables"]
    table = doc["tables"][0]
    assert table["name"] == "orders", table
    assert {m["name"] for m in table["metrics"]} == {
        "revenue",
        "order_count",
        "avg_order_value",
    }


def test_the_round_trip_does_not_duplicate_the_relation(tmp_path):
    """Two logical tables over one base_table imported back as two relations
    pointing at the same physical table."""
    from sqldash.project.store import DashboardStore
    from sqldash.semantics import SemanticLayer
    from sqldash.semantics.cortex import build_semantic_view, render_yaml

    (tmp_path / "metrics.yaml").write_text(SNOWFLAKE_DERIVED)
    store = DashboardStore(tmp_path)
    doc, _ = build_semantic_view(SemanticLayer(store), store, "v")
    out, _ = parse_semantic_view(render_yaml(doc))
    assert list(out["relations"]) == ["orders"], out["relations"]


def test_tables_that_are_genuinely_different_stay_separate(tmp_path):
    """Grouping by physical identity must not merge two tables that share a
    short name in different databases."""
    from sqldash.project.store import DashboardStore
    from sqldash.semantics import SemanticLayer
    from sqldash.semantics.cortex import build_semantic_view

    (tmp_path / "metrics.yaml").write_text(
        "source: {type: snowflake, account: a, username: u, "
        "database: ANALYTICS, schema: PUBLIC}\n"
        "relations:\n"
        "  orders: {table: ORDERS}\n"
        "  archive: {table: OLD.ARCHIVE.ORDERS}\n"
        "metrics:\n"
        "  revenue: {relation: orders, expr: SUM(amount)}\n"
        "  archived: {relation: archive, expr: SUM(amount)}\n"
    )
    store = DashboardStore(tmp_path)
    doc, _ = build_semantic_view(SemanticLayer(store), store, "v")
    assert len(doc["tables"]) == 2, doc["tables"]
    assert {t["name"] for t in doc["tables"]} == {"orders", "archive"}


def test_two_tables_that_want_the_same_label_are_qualified(tmp_path):
    """Grouping by physical identity keeps them separate, but the *label* was a
    bare table name — not unique across schemas. The importer keys relations by
    name, so a duplicate silently rebound one group's metrics to the other
    group's physical table: a different warehouse table, in valid-looking YAML.
    """
    from sqldash.project.store import DashboardStore
    from sqldash.semantics import SemanticLayer
    from sqldash.semantics.cortex import build_semantic_view, render_yaml

    (tmp_path / "metrics.yaml").write_text(
        "source: {type: snowflake, account: a, username: u, "
        "database: ANALYTICS, schema: PUBLIC}\n"
        "metrics:\n"
        "  rev: {table: ANALYTICS.PUBLIC.ORDERS, expr: SUM(amount)}\n"
        "  rev_old: {table: OLD.ARCHIVE.ORDERS, expr: SUM(amount)}\n"
    )
    store = DashboardStore(tmp_path)
    doc, warnings = build_semantic_view(SemanticLayer(store), store, "v")

    names = [t["name"] for t in doc["tables"]]
    assert len(set(names)) == 2, names
    assert any("more than one physical table" in w for w in warnings), warnings

    # The round trip binds each metric to the table it was written against.
    out, _ = parse_semantic_view(render_yaml(doc))
    assert len(out["relations"]) == 2, out["relations"]
    bound = {name: out["relations"][m["relation"]]["table"] for name, m in out["metrics"].items()}
    assert bound["rev"] == "ANALYTICS.PUBLIC.ORDERS", bound
    assert bound["rev_old"] == "OLD.ARCHIVE.ORDERS", bound


def test_a_unique_label_is_left_alone(tmp_path):
    """The qualifying only fires on a collision — an ordinary export keeps the
    relation name the author chose."""
    from sqldash.project.store import DashboardStore
    from sqldash.semantics import SemanticLayer
    from sqldash.semantics.cortex import build_semantic_view

    (tmp_path / "metrics.yaml").write_text(SNOWFLAKE_DERIVED)
    store = DashboardStore(tmp_path)
    doc, warnings = build_semantic_view(SemanticLayer(store), store, "v")
    assert [t["name"] for t in doc["tables"]] == ["orders"], doc["tables"]
    assert not [w for w in warnings if "more than one physical table" in w], warnings


def test_qualifying_a_collision_does_not_recreate_it(tmp_path):
    """The dedup pass checked each candidate against the *original* names and
    never recorded what it handed out, so two tables in different databases
    sharing a schema both became SCHEMA_TABLE — the pass reintroducing the
    collision it exists to remove, while warning that it had prevented it."""
    from sqldash.project.store import DashboardStore
    from sqldash.semantics import SemanticLayer
    from sqldash.semantics.cortex import build_semantic_view

    (tmp_path / "metrics.yaml").write_text(
        "source: {type: snowflake, account: a, username: u, "
        "database: ANALYTICS, schema: PUBLIC}\n"
        "metrics:\n"
        "  rev: {table: ANALYTICS.PUBLIC.ORDERS, expr: SUM(amount)}\n"
        "  rev_old: {table: OLD.PUBLIC.ORDERS, expr: SUM(amount)}\n"
    )
    store = DashboardStore(tmp_path)
    doc, _ = build_semantic_view(SemanticLayer(store), store, "v")
    names = [t["name"] for t in doc["tables"]]
    assert len(doc["tables"]) == 2, doc["tables"]
    assert len(set(names)) == len(names), names


def test_a_qualified_collision_round_trips_to_the_right_physical_tables(tmp_path):
    """The point of the labels is that importing the view back binds each
    metric to the table it was defined on."""
    from sqldash.project.store import DashboardStore
    from sqldash.semantics import SemanticLayer
    from sqldash.semantics.cortex import build_semantic_view, parse_semantic_view, render_yaml

    (tmp_path / "metrics.yaml").write_text(
        "source: {type: snowflake, account: a, username: u, "
        "database: ANALYTICS, schema: PUBLIC}\n"
        "metrics:\n"
        "  rev: {table: ANALYTICS.PUBLIC.ORDERS, expr: SUM(amount)}\n"
        "  rev_old: {table: OLD.PUBLIC.ORDERS, expr: SUM(amount)}\n"
    )
    store = DashboardStore(tmp_path)
    doc, _ = build_semantic_view(SemanticLayer(store), store, "v")
    back, _ = parse_semantic_view(render_yaml(doc))
    tables = {name: rel["table"] for name, rel in back["relations"].items()}
    bound = {n: tables[m["relation"]] for n, m in back["metrics"].items()}
    assert bound == {
        "rev": "ANALYTICS.PUBLIC.ORDERS",
        "rev_old": "OLD.PUBLIC.ORDERS",
    }, bound


def test_a_shallower_qualification_is_preferred_when_it_separates_them(tmp_path):
    """Different schemas need only the schema, not the whole three-part name."""
    from sqldash.project.store import DashboardStore
    from sqldash.semantics import SemanticLayer
    from sqldash.semantics.cortex import build_semantic_view

    (tmp_path / "metrics.yaml").write_text(
        "source: {type: snowflake, account: a, username: u, "
        "database: ANALYTICS, schema: PUBLIC}\n"
        "metrics:\n"
        "  rev: {table: ANALYTICS.PUBLIC.ORDERS, expr: SUM(amount)}\n"
        "  rev_old: {table: OLD.ARCHIVE.ORDERS, expr: SUM(amount)}\n"
    )
    store = DashboardStore(tmp_path)
    doc, _ = build_semantic_view(SemanticLayer(store), store, "v")
    assert {t["name"] for t in doc["tables"]} == {"PUBLIC_ORDERS", "ARCHIVE_ORDERS"}


def test_merging_relations_that_disagree_on_time_dimension_warns(tmp_path):
    """Grouping by physical table merges two relations over one table, and
    `import cortex` keeps only the first time dimension per metric — so the
    second metric's grain comes back changed. Say so on the way out, where the
    definitions that disagree are still in hand."""
    from sqldash.project.store import DashboardStore
    from sqldash.semantics import SemanticLayer
    from sqldash.semantics.cortex import build_semantic_view

    (tmp_path / "metrics.yaml").write_text(
        "source: {type: snowflake, account: a, username: u, database: A, schema: P}\n"
        "relations:\n"
        "  orders: {table: ORDERS}\n"
        "  orders2: {table: ORDERS}\n"
        "metrics:\n"
        "  revenue: {relation: orders, expr: SUM(amount), "
        "time_dimension: {name: order_date, grain: day}}\n"
        "  revenue2: {relation: orders2, expr: SUM(amount), "
        "time_dimension: {name: created_at, grain: day}}\n"
    )
    store = DashboardStore(tmp_path)
    _doc, warnings = build_semantic_view(SemanticLayer(store), store, "v")
    assert any(
        "different time dimensions" in w and "order_date" in w and "created_at" in w
        for w in warnings
    ), warnings


def test_a_group_that_cannot_be_qualified_still_gets_unique_labels(tmp_path):
    """A sql relation's base_table is a `definition`, not a
    database/schema/table, so no qualification level separates a group
    containing one — the duplicate survived while the warning claimed it had
    been removed, and the round trip bound a real warehouse table's metric to a
    SELECT."""
    from sqldash.project.store import DashboardStore
    from sqldash.semantics import SemanticLayer
    from sqldash.semantics.cortex import build_semantic_view, parse_semantic_view, render_yaml

    (tmp_path / "metrics.yaml").write_text(
        "source: {type: snowflake, account: a, username: u, "
        "database: ANALYTICS, schema: PUBLIC}\n"
        "metrics:\n"
        "  rev: {table: ANALYTICS.PUBLIC.m_base, expr: SUM(amount)}\n"
        '  m: {sql: "SELECT 1 AS x", expr: SUM(x)}\n'
    )
    store = DashboardStore(tmp_path)
    doc, _ = build_semantic_view(SemanticLayer(store), store, "v")
    names = [t["name"] for t in doc["tables"]]
    assert len(set(names)) == len(names), names

    back, _ = parse_semantic_view(render_yaml(doc))
    rel = back["relations"]
    assert rel[back["metrics"]["rev"]["relation"]] == {"table": "ANALYTICS.PUBLIC.m_base"}
    assert rel[back["metrics"]["m"]["relation"]] == {"sql": "SELECT 1 AS x"}


def test_one_time_dimension_name_over_two_exprs_is_reported(tmp_path):
    """The disagreement check keyed on name alone, so two relations spelling one
    name over different columns dropped the second silently — the quieter half
    of the same loss."""
    from sqldash.project.store import DashboardStore
    from sqldash.semantics import SemanticLayer
    from sqldash.semantics.cortex import build_semantic_view

    (tmp_path / "metrics.yaml").write_text(
        "source: {type: snowflake, account: a, username: u, database: A, schema: P}\n"
        "relations:\n"
        "  orders: {table: ORDERS}\n"
        "  orders2: {table: ORDERS}\n"
        "metrics:\n"
        "  revenue: {relation: orders, expr: SUM(amount), "
        "time_dimension: {name: ts, expr: order_date, grain: day}}\n"
        "  revenue2: {relation: orders2, expr: SUM(amount), "
        "time_dimension: {name: ts, expr: created_at, grain: day}}\n"
    )
    store = DashboardStore(tmp_path)
    _doc, warnings = build_semantic_view(SemanticLayer(store), store, "v")
    assert any("order_date" in w and "created_at" in w and "'ts'" in w for w in warnings), warnings


def test_a_merge_warning_names_the_table_the_output_actually_has(tmp_path):
    """The warning was emitted mid-collection, before the qualification pass
    renamed the entry — so it quoted a label that appears nowhere in the doc."""
    from sqldash.project.store import DashboardStore
    from sqldash.semantics import SemanticLayer
    from sqldash.semantics.cortex import build_semantic_view

    (tmp_path / "metrics.yaml").write_text(
        "source: {type: snowflake, account: a, username: u, database: A, schema: P}\n"
        "metrics:\n"
        "  a: {table: A.P.ORDERS, expr: SUM(x), time_dimension: {name: d1, grain: day}}\n"
        "  b: {table: A.P.ORDERS, expr: SUM(y), time_dimension: {name: d2, grain: day}}\n"
        "  c: {table: B.P.ORDERS, expr: SUM(z)}\n"
    )
    store = DashboardStore(tmp_path)
    doc, warnings = build_semantic_view(SemanticLayer(store), store, "v")
    emitted = {t["name"] for t in doc["tables"]}
    merge = next(w for w in warnings if "different time dimensions" in w)
    assert any(f"'{name}'" in merge for name in emitted), (merge, emitted)


def test_the_exported_view_keeps_the_name_it_was_given(tmp_path):
    """The qualification pass looped `for name, entries in claimed.items()`,
    rebinding this function's own `name` parameter — so the doc built afterwards
    was named after the last table label and the CLI's `--name` became a no-op,
    in valid-looking YAML. Every multi-table export hit it, collision or not."""
    from sqldash.project.store import DashboardStore
    from sqldash.semantics import SemanticLayer
    from sqldash.semantics.cortex import build_semantic_view

    (tmp_path / "metrics.yaml").write_text(
        "source: {type: snowflake, account: a, username: u, database: A, schema: P}\n"
        "relations:\n"
        "  orders: {table: ORDERS}\n"
        "  customers: {table: CUSTOMERS}\n"
        "metrics:\n"
        "  rev: {relation: orders, expr: SUM(amount)}\n"
        "  cust: {relation: customers, expr: 'COUNT(*)'}\n"
    )
    store = DashboardStore(tmp_path)
    doc, _ = build_semantic_view(SemanticLayer(store), store, "my_view")
    assert doc["name"] == "my_view"


def test_the_view_name_survives_a_label_collision(tmp_path):
    """The rebinding also had to survive the branch that actually renames."""
    from sqldash.project.store import DashboardStore
    from sqldash.semantics import SemanticLayer
    from sqldash.semantics.cortex import build_semantic_view

    (tmp_path / "metrics.yaml").write_text(
        "source: {type: snowflake, account: a, username: u, "
        "database: ANALYTICS, schema: PUBLIC}\n"
        "metrics:\n"
        "  rev: {table: ANALYTICS.PUBLIC.ORDERS, expr: SUM(amount)}\n"
        "  rev_old: {table: OLD.PUBLIC.ORDERS, expr: SUM(amount)}\n"
    )
    store = DashboardStore(tmp_path)
    doc, _ = build_semantic_view(SemanticLayer(store), store, "my_view")
    assert doc["name"] == "my_view"


def test_a_dimension_merged_away_across_relations_is_reported(tmp_path):
    """Grouping by physical table merges relations that disagree on a dimension
    too, first-wins by name. The time-dimension half of that loss warned and
    this half did not — the same drop, reported inconsistently."""
    from sqldash.project.store import DashboardStore
    from sqldash.semantics import SemanticLayer
    from sqldash.semantics.cortex import build_semantic_view

    (tmp_path / "metrics.yaml").write_text(
        "source: {type: snowflake, account: a, username: u, database: A, schema: P}\n"
        "relations:\n"
        "  orders: {table: ORDERS}\n"
        "  orders2: {table: ORDERS}\n"
        "metrics:\n"
        "  revenue: {relation: orders, expr: SUM(amount), "
        "dimensions: [{name: region, expr: REGION}]}\n"
        "  revenue2: {relation: orders2, expr: SUM(amount), "
        "dimensions: [{name: region, expr: REGION_CODE}]}\n"
    )
    store = DashboardStore(tmp_path)
    _doc, warnings = build_semantic_view(SemanticLayer(store), store, "v")
    assert any("region" in w and "REGION_CODE" in w and "dimension" in w for w in warnings), (
        warnings
    )


def test_relations_that_agree_on_a_dimension_are_not_reported(tmp_path):
    """Merging two relations that spell a dimension the same way loses nothing,
    so it must stay quiet — a warning on every merge would train people to
    ignore the one that matters."""
    from sqldash.project.store import DashboardStore
    from sqldash.semantics import SemanticLayer
    from sqldash.semantics.cortex import build_semantic_view

    (tmp_path / "metrics.yaml").write_text(
        "source: {type: snowflake, account: a, username: u, database: A, schema: P}\n"
        "relations:\n"
        "  orders: {table: ORDERS}\n"
        "  orders2: {table: ORDERS}\n"
        "metrics:\n"
        "  revenue: {relation: orders, expr: SUM(amount), "
        "dimensions: [{name: region, expr: REGION}]}\n"
        "  revenue2: {relation: orders2, expr: SUM(amount), "
        "dimensions: [{name: region, expr: REGION}]}\n"
    )
    store = DashboardStore(tmp_path)
    _doc, warnings = build_semantic_view(SemanticLayer(store), store, "v")
    assert not [w for w in warnings if "dimension" in w], warnings


DISCARD_COLLISION = (
    "source: {type: snowflake, account: a, username: u, database: A, schema: PUBLIC}\n"
    "relations:\n"
    "  PUBLIC_ORDERS: {table: ORDERS}\n"
    "metrics:\n"
    "  rev: {relation: PUBLIC_ORDERS, expr: SUM(amount)}\n"
    "  rev_old: {table: B.ARCHIVE.PUBLIC_ORDERS, expr: SUM(amount)}\n"
    "  rev2: {table: C.PUBLIC.ORDERS, expr: SUM(amount)}\n"
    "  rev3: {table: D.OTHER.ORDERS, expr: SUM(amount)}\n"
)


def test_a_label_an_entry_kept_is_not_handed_to_a_later_group(tmp_path):
    """The pass freed the group's own label unconditionally, but the first entry
    usually qualifies to the label it already had — so a later group could take
    a name still in use, and the round trip bound one table's metrics to
    another's. The collision this pass exists to remove, handed out by the pass,
    twice over."""
    from sqldash.project.store import DashboardStore
    from sqldash.semantics import SemanticLayer
    from sqldash.semantics.cortex import build_semantic_view, parse_semantic_view, render_yaml

    (tmp_path / "metrics.yaml").write_text(DISCARD_COLLISION)
    store = DashboardStore(tmp_path)
    doc, _ = build_semantic_view(SemanticLayer(store), store, "v")
    names = [t["name"] for t in doc["tables"]]
    assert len(set(names)) == len(names), names

    back, _ = parse_semantic_view(render_yaml(doc))
    rel = {k: v.get("table") for k, v in back["relations"].items()}
    bound = {n: rel[m["relation"]] for n, m in back["metrics"].items()}
    assert bound == {
        "rev": "A.PUBLIC.ORDERS",
        "rev_old": "B.ARCHIVE.PUBLIC_ORDERS",
        "rev2": "C.PUBLIC.ORDERS",
        "rev3": "D.OTHER.ORDERS",
    }, bound


def test_the_later_group_qualifies_rather_than_falling_back_to_a_suffix(tmp_path):
    """Keeping the held label in `taken` is what pushes the second group to the
    three-part level. A numeric suffix would also be unique, so binding alone
    does not prove the bookkeeping is right."""
    from sqldash.project.store import DashboardStore
    from sqldash.semantics import SemanticLayer
    from sqldash.semantics.cortex import build_semantic_view

    (tmp_path / "metrics.yaml").write_text(DISCARD_COLLISION)
    store = DashboardStore(tmp_path)
    doc, _ = build_semantic_view(SemanticLayer(store), store, "v")
    assert {t["name"] for t in doc["tables"]} == {
        "PUBLIC_ORDERS",
        "ARCHIVE_PUBLIC_ORDERS",
        "C_PUBLIC_ORDERS",
        "D_OTHER_ORDERS",
    }, [t["name"] for t in doc["tables"]]


@pytest.mark.parametrize(
    "metrics",
    [
        "  a: {table: A.P.T, expr: SUM(x)}\n  b: {table: B.P.T, expr: SUM(x)}\n",
        "  a: {table: A.P.T, expr: SUM(x)}\n  b: {table: B.P.T, expr: SUM(x)}\n"
        "  c: {table: C.P.T, expr: SUM(x)}\n",
        "  a: {table: A.P.T, expr: SUM(x)}\n  b: {table: B.Q.T, expr: SUM(x)}\n"
        "  c: {table: C.P.T, expr: SUM(x)}\n",
        '  a: {table: A.P.m_base, expr: SUM(x)}\n  m: {sql: "SELECT 1 AS x", expr: SUM(x)}\n',
        '  a: {table: A.P.m_base, expr: SUM(x)}\n  m: {sql: "SELECT 1 AS x", expr: SUM(x)}\n'
        "  m_base_2: {table: B.P.m_base, expr: SUM(x)}\n",
    ],
)
def test_exported_table_labels_are_always_unique(tmp_path, metrics):
    """The invariant the importer depends on, over the shapes that have broken
    it: same schema across databases, three-way groups, mixed levels, sql
    relations, and a name that collides with the suffix fallback's own
    candidate."""
    from sqldash.project.store import DashboardStore
    from sqldash.semantics import SemanticLayer
    from sqldash.semantics.cortex import build_semantic_view

    (tmp_path / "metrics.yaml").write_text(
        "source: {type: snowflake, account: a, username: u, database: A, schema: P}\n"
        "metrics:\n" + metrics
    )
    store = DashboardStore(tmp_path)
    doc, _ = build_semantic_view(SemanticLayer(store), store, "v")
    names = [t["name"] for t in doc["tables"]]
    assert len(set(names)) == len(names), names


def _filter_project(tmp_path, metrics: str):
    (tmp_path / "metrics.yaml").write_text(
        "source: {type: snowflake, account: a, username: u, database: A, schema: P}\n"
        "relations:\n"
        "  orders: {table: ORDERS}\n"
        "  orders2: {table: ORDERS}\n"
        "metrics:\n" + metrics
    )
    store = DashboardStore(tmp_path)
    return build_semantic_view(SemanticLayer(store), store, "v")


def test_metric_filters_are_folded_into_the_expr_not_published_as_table_filters(tmp_path):
    """A semantic view's table filters are named conditions Cortex Analyst may
    pick for a question; a query never applies them to a metric. Exported that
    way, `region = 'us'` revenue came back as the unfiltered total on Snowflake
    (836406.37 against sqldash's 392870.99), with a warning that claimed the
    opposite. Guarding the aggregate's input is the same number."""
    doc, warnings = _filter_project(
        tmp_path,
        "  revenue: {relation: orders, expr: SUM(amount)}\n"
        "  us_revenue: {relation: orders, expr: SUM(amount), filters: [\"region = 'us'\"]}\n"
        "  big_us: {relation: orders2, expr: COUNT(*), "
        'filters: ["region = \'us\'", "amount > 150"]}\n'
        "  us_aov: {relation: orders, "
        "expr: 'ROUND(SUM(amount) / NULLIF(COUNT(DISTINCT id), 0), 2)',"
        " filters: [\"region = 'us'\"]}\n",
    )
    [table] = doc["tables"]
    assert "filters" not in table
    exprs = {m["name"]: m["expr"] for m in table["metrics"]}
    assert exprs == {
        "revenue": "SUM(amount)",
        "us_revenue": "SUM(CASE WHEN (region = 'us') THEN amount END)",
        "big_us": "COUNT(CASE WHEN (region = 'us') AND (amount > 150) THEN 1 END)",
        "us_aov": "ROUND(SUM(CASE WHEN (region = 'us') THEN amount END) / "
        "NULLIF(COUNT(DISTINCT CASE WHEN (region = 'us') THEN id END), 0), 2)",
    }
    assert not [w for w in warnings if "filter" in w], warnings

    orders = (
        "(SELECT * FROM (VALUES (1, 'us', 100.0), (2, 'us', 200.0), (3, 'eu', 400.0))"
        " t(id, region, amount))"
    )
    folded = {
        name: duckdb.sql(f"SELECT {e} FROM {orders}").fetchone()[0] for name, e in exprs.items()
    }
    assert float(folded["us_revenue"]) == 300.0
    assert folded["big_us"] == 1
    assert float(folded["us_aov"]) == 150.0


@pytest.mark.parametrize(
    "expr",
    ["CORR(a, b)", "SUM(a) OVER ()", "LISTAGG(x, ',')", "COUNT_IF(x > 1)", "amount"],
)
def test_a_filtered_metric_whose_expr_cannot_carry_the_filter_is_skipped(tmp_path, expr):
    """Publishing it without the filter is the silent wrong number this exists
    to prevent, so it is left out and named."""
    doc, warnings = _filter_project(
        tmp_path,
        "  revenue: {relation: orders, expr: SUM(amount)}\n"
        f'  odd: {{relation: orders, expr: "{expr}", filters: ["region = \'us\'"]}}\n',
    )
    assert [m["name"] for m in doc["tables"][0]["metrics"]] == ["revenue"]
    assert any("skipped metric 'odd'" in w and "folded" in w for w in warnings), warnings


def test_a_quoted_paren_in_a_filtered_expr_does_not_end_the_aggregate(tmp_path):
    doc, _ = _filter_project(
        tmp_path,
        "  n: {relation: orders, expr: \"SUM(CASE WHEN note = 'a)' THEN 1 ELSE 0 END)\", "
        "filters: [\"region = 'us'\"]}\n",
    )
    assert doc["tables"][0]["metrics"][0]["expr"] == (
        "SUM(CASE WHEN (region = 'us') THEN CASE WHEN note = 'a)' THEN 1 ELSE 0 END END)"
    )


def test_an_untimed_metric_on_a_timed_table_is_reported(tmp_path):
    """`import cortex` gives every metric on a table that table's first time
    dimension, so a metric authored without one comes back queryable by time
    against a column its author never associated with it — a time range that
    used to raise "has no time_dimension" now silently filters. Grouping by
    physical table is what puts an untimed metric on a timed table; before it
    they were separate tables and the round trip was lossless."""
    from sqldash.project.store import DashboardStore
    from sqldash.semantics import SemanticLayer
    from sqldash.semantics.cortex import build_semantic_view

    (tmp_path / "metrics.yaml").write_text(
        "source: {type: snowflake, account: a, username: u, database: A, schema: P}\n"
        "relations:\n"
        "  orders: {table: ORDERS}\n"
        "  orders2: {table: ORDERS}\n"
        "metrics:\n"
        "  revenue: {relation: orders, expr: SUM(amount), "
        "time_dimension: {name: order_date, grain: day}}\n"
        "  revenue2: {relation: orders2, expr: SUM(x)}\n"
    )
    store = DashboardStore(tmp_path)
    _doc, warnings = build_semantic_view(SemanticLayer(store), store, "v")
    assert any(
        "no time dimension" in w and "revenue2" in w and "order_date" in w for w in warnings
    ), warnings


def test_a_table_no_metric_timed_stays_quiet(tmp_path):
    """Nothing is gained on import when the table has no time dimension to give."""
    from sqldash.project.store import DashboardStore
    from sqldash.semantics import SemanticLayer
    from sqldash.semantics.cortex import build_semantic_view

    (tmp_path / "metrics.yaml").write_text(
        "source: {type: snowflake, account: a, username: u, database: A, schema: P}\n"
        "relations:\n"
        "  orders: {table: ORDERS}\n"
        "  orders2: {table: ORDERS}\n"
        "metrics:\n"
        "  a: {relation: orders, expr: SUM(x)}\n"
        "  b: {relation: orders2, expr: SUM(y)}\n"
    )
    store = DashboardStore(tmp_path)
    _doc, warnings = build_semantic_view(SemanticLayer(store), store, "v")
    assert not [w for w in warnings if "no time dimension" in w], warnings


def test_the_untimed_warning_survives_metric_order(tmp_path):
    """The untimed metric may be seen before the timed one, so the check cannot
    be a running comparison against what the table has so far."""
    from sqldash.project.store import DashboardStore
    from sqldash.semantics import SemanticLayer
    from sqldash.semantics.cortex import build_semantic_view

    (tmp_path / "metrics.yaml").write_text(
        "source: {type: snowflake, account: a, username: u, database: A, schema: P}\n"
        "relations:\n"
        "  orders: {table: ORDERS}\n"
        "  orders2: {table: ORDERS}\n"
        "metrics:\n"
        "  revenue2: {relation: orders2, expr: SUM(x)}\n"
        "  revenue: {relation: orders, expr: SUM(amount), "
        "time_dimension: {name: order_date, grain: day}}\n"
    )
    store = DashboardStore(tmp_path)
    _doc, warnings = build_semantic_view(SemanticLayer(store), store, "v")
    assert any("no time dimension" in w and "revenue2" in w for w in warnings), warnings


def test_a_derived_metric_over_a_sql_relation_joins_its_peers(tmp_path):
    """Base tables were keyed by physical identity but sql relations by the
    metric's own name, and expansion clears `relation:` on a derived metric — so
    a derived metric over a sql relation still split from the peers it was
    derived from, into two tables with identical definitions. That is the split
    this fix is about, in the half it did not reach."""
    from sqldash.project.store import DashboardStore
    from sqldash.semantics import SemanticLayer
    from sqldash.semantics.cortex import build_semantic_view, parse_semantic_view, render_yaml

    (tmp_path / "metrics.yaml").write_text(
        "source: {type: snowflake, account: a, username: u, database: A, schema: P}\n"
        "metrics:\n"
        '  revenue: {sql: "SELECT amount FROM t", expr: SUM(amount)}\n'
        '  half: {derived: "{revenue} / 2"}\n'
    )
    store = DashboardStore(tmp_path)
    doc, _ = build_semantic_view(SemanticLayer(store), store, "v")
    assert len(doc["tables"]) == 1, doc["tables"]
    assert {m["name"] for m in doc["tables"][0]["metrics"]} == {"revenue", "half"}

    back, _ = parse_semantic_view(render_yaml(doc))
    assert len(back["relations"]) == 1, back["relations"]


def test_sql_relations_with_different_definitions_stay_separate(tmp_path):
    """Keying on the definition must not merge two genuinely different queries."""
    from sqldash.project.store import DashboardStore
    from sqldash.semantics import SemanticLayer
    from sqldash.semantics.cortex import build_semantic_view

    (tmp_path / "metrics.yaml").write_text(
        "source: {type: snowflake, account: a, username: u, database: A, schema: P}\n"
        "metrics:\n"
        '  one: {sql: "SELECT a FROM t1", expr: SUM(a)}\n'
        '  two: {sql: "SELECT b FROM t2", expr: SUM(b)}\n'
    )
    store = DashboardStore(tmp_path)
    doc, _ = build_semantic_view(SemanticLayer(store), store, "v")
    assert len(doc["tables"]) == 2, doc["tables"]


def test_export_fails_loud_on_a_query_whose_date_default_is_not_a_token(tmp_path):
    """A typo'd default used to skip the verified query with a warning —
    a silent incomplete export. ParamError is the same loud verdict as
    every other surface."""
    (tmp_path / "metrics.yaml").write_text(
        "source: {type: snowflake, account: a, username: u, database: A, schema: P}\n"
        "metrics:\n"
        "  revenue: {table: A.P.ORDERS, expr: SUM(amount)}\n"
    )
    (tmp_path / "d.yaml").write_text(
        "title: D\n"
        "source: {type: snowflake, account: a, username: u, database: A, schema: P}\n"
        "filters:\n"
        "  - {name: start, type: date, default: last_30_day}\n"
        "queries:\n"
        "  q: SELECT 1 WHERE d >= {{ start }}\n"
        "tiles: [{id: w, query: q, position: {x: 0, y: 0, w: 6, h: 4}}]\n"
    )
    store = DashboardStore(tmp_path)
    with pytest.raises(ParamError, match=r"d\.q") as excinfo:
        build_semantic_view(SemanticLayer(store), store, "v")
    assert "last_30_day" in str(excinfo.value)


def test_export_skips_a_daterange_default_that_is_not_a_preset(tmp_path):
    """`default: today` on a daterange is a valid date token but not a
    range preset. Binding then continue-ing left dates_start unbound and
    export KeyError'd. Missing-param skip is the old, correct verdict."""
    (tmp_path / "metrics.yaml").write_text(
        "source: {type: snowflake, account: a, username: u, database: A, schema: P}\n"
        "metrics:\n"
        "  revenue: {table: A.P.ORDERS, expr: SUM(amount)}\n"
    )
    (tmp_path / "d.yaml").write_text(
        "title: D\n"
        "source: {type: snowflake, account: a, username: u, database: A, schema: P}\n"
        "filters:\n"
        "  - {name: dates, type: daterange, default: today}\n"
        "queries:\n"
        "  q: SELECT 1 WHERE d >= {{ dates_start }}\n"
        "tiles: [{id: w, query: q, position: {x: 0, y: 0, w: 6, h: 4}}]\n"
    )
    store = DashboardStore(tmp_path)
    doc, warnings = build_semantic_view(SemanticLayer(store), store, "v")
    assert not doc.get("verified_queries"), doc.get("verified_queries")
    assert any("dates_start" in w and "d.q" in w for w in warnings), warnings


ISSUE_330_VIEW = """
name: V
tables:
  - name: ORDERS
    base_table: {database: DB, schema: S, table: ORDERS}
    facts:
      - {name: AMOUNT, expr: AMOUNT}
      - {name: ORDER_ID, expr: ORDER_ID}
  - name: REFUNDS
    base_table: {database: DB, schema: S, table: REFUNDS}
    facts:
      - {name: AMOUNT, expr: AMOUNT}
      - {name: REFUND_ID, expr: REFUND_ID}
"""


def test_a_fact_name_shared_by_two_tables_is_qualified_not_overwritten(tmp_path):
    """Facts are namespaced per table in a semantic view, so two tables exposing
    `AMOUNT` is ordinary input. Keying on the bare name let REFUNDS silently
    replace ORDERS, and the survivor summed the wrong table with no warning.
    #330. The convention is `import lookml`'s: first keeps the bare name, later
    ones are `<table>_<name>`."""
    from sqldash.lint import lint_project

    out, warnings = parse_semantic_view(ISSUE_330_VIEW)
    assert set(out["metrics"]) == {"amount", "order_id", "refunds_amount", "refund_id"}
    assert out["metrics"]["amount"] == {"relation": "orders", "expr": "SUM(AMOUNT)"}
    assert out["metrics"]["refunds_amount"] == {"relation": "refunds", "expr": "SUM(AMOUNT)"}
    assert [w for w in warnings if "refunds_amount" in w and "orders" in w] == [
        "table 'refunds': metric 'amount' is already defined by table 'orders', "
        "so it was imported as 'refunds_amount'"
    ], warnings

    (tmp_path / "metrics.yaml").write_text(render_yaml(out))
    store = DashboardStore(tmp_path)
    findings = lint_project(store, SemanticLayer(store))
    assert not [f for f in findings if f.level == "error"], findings


def test_a_metric_name_shared_by_two_tables_is_qualified_too():
    """The `metrics:` path writes to the same dict as facts do, so it collides
    the same way."""
    out, warnings = parse_semantic_view(
        "name: V\ntables:\n"
        "  - name: ORDERS\n    base_table: {database: DB, schema: S, table: ORDERS}\n"
        "    facts: [{name: AMOUNT, expr: AMOUNT}]\n"
        "    metrics: [{name: total, expr: SUM(AMOUNT)}]\n"
        "  - name: REFUNDS\n    base_table: {database: DB, schema: S, table: REFUNDS}\n"
        "    facts: [{name: AMOUNT, expr: AMOUNT}]\n"
        "    metrics: [{name: total, expr: SUM(AMOUNT)}]\n"
    )
    assert out["metrics"]["total"]["relation"] == "orders"
    assert out["metrics"]["refunds_total"]["relation"] == "refunds"
    assert any("refunds_total" in w for w in warnings), warnings


def test_a_qualified_name_that_is_itself_taken_still_ends_up_unique():
    """Unique keys are the invariant; a table that already has a metric called
    `REFUNDS_AMOUNT` must not be overwritten by the qualification either."""
    out, warnings = parse_semantic_view(
        "name: V\ntables:\n"
        "  - name: ORDERS\n    base_table: {database: DB, schema: S, table: ORDERS}\n"
        "    facts: [{name: AMOUNT, expr: AMOUNT}, {name: REFUNDS_AMOUNT, expr: RA}]\n"
        "  - name: REFUNDS\n    base_table: {database: DB, schema: S, table: REFUNDS}\n"
        "    facts: [{name: AMOUNT, expr: AMOUNT}]\n"
    )
    assert out["metrics"]["refunds_amount"] == {"relation": "orders", "expr": "SUM(RA)"}
    assert out["metrics"]["refunds_amount_2"] == {"relation": "refunds", "expr": "SUM(AMOUNT)"}
    assert any("refunds_amount_2" in w for w in warnings), warnings


def test_dimensions_shared_across_tables_stay_on_their_own_metrics():
    """Dimensions are copied onto each metric, so a name two tables both use
    is not a collision: each metric keeps the definition from its own table."""
    out, warnings = parse_semantic_view(
        "name: V\ntables:\n"
        "  - name: ORDERS\n    base_table: {database: DB, schema: S, table: ORDERS}\n"
        "    dimensions: [{name: REGION, expr: SHIP_REGION}]\n"
        "    facts: [{name: AMOUNT, expr: AMOUNT}]\n"
        "  - name: REFUNDS\n    base_table: {database: DB, schema: S, table: REFUNDS}\n"
        "    dimensions: [{name: REGION, expr: BILL_REGION}]\n"
        "    facts: [{name: AMOUNT, expr: AMOUNT}]\n"
    )
    assert out["metrics"]["amount"]["dimensions"] == [{"name": "region", "expr": "SHIP_REGION"}]
    assert out["metrics"]["refunds_amount"]["dimensions"] == [
        {"name": "region", "expr": "BILL_REGION"}
    ]
    assert not [w for w in warnings if "region" in w], warnings


ILLEGAL_NAMES = """
name: V
tables:
  - name: ORDERS
    base_table: {database: DB, schema: S, table: ORDERS}
    dimensions:
      - {name: "order status", expr: ORDER_STATUS}
      - {name: "order"}
    time_dimensions:
      - {name: "when", expr: CREATED_AT}
    metrics:
      - {name: select, expr: SUM(AMOUNT)}
"""


def test_import_cortex_slugifies_names_sqldash_would_refuse():
    """Quoted Snowflake identifiers with spaces, and reserved words, are ordinary
    source input that sqldash's own models reject — so the importer wrote a
    metrics.yaml that `sqldash lint` was the first thing to notice (#337)."""
    out, warnings = parse_semantic_view(ILLEGAL_NAMES)
    metric = out["metrics"]["select_metric"]
    assert [d["name"] for d in metric["dimensions"]] == ["order_status", "order_field"]
    assert metric["time_dimension"]["name"] == "when_field"
    for original, renamed in (
        ("order status", "order_status"),
        ("order", "order_field"),
        ("when", "when_field"),
        ("select", "select_metric"),
    ):
        assert any(f"'{original}'" in w and f"'{renamed}'" in w for w in warnings), warnings


def test_a_renamed_dimension_keeps_pointing_at_its_own_column():
    """A dimension's expr defaults to its name, so renaming one that had no expr
    would repoint it at a column the table does not have."""
    out, _ = parse_semantic_view(ILLEGAL_NAMES)
    dims = {d["name"]: d.get("expr") for d in out["metrics"]["select_metric"]["dimensions"]}
    assert dims["order_status"] == "ORDER_STATUS"
    assert dims["order_field"] == '"order"'


def test_import_cortex_output_validates_as_a_metrics_file():
    out, _ = parse_semantic_view(ILLEGAL_NAMES)
    out["source"] = {
        "type": "snowflake",
        "account": "a",
        "database": "DB",
        "schema": "S",
        "username": "u",
    }
    MetricsFile.model_validate(out)


def test_a_relation_name_sqldash_would_refuse_is_slugified_and_still_referenced():
    out, warnings = parse_semantic_view(
        "name: V\ntables:\n"
        '  - name: "order details"\n'
        "    base_table: {database: DB, schema: S, table: ORDERS}\n"
        "    facts: [{name: AMOUNT, expr: AMOUNT}]\n"
    )
    assert "order_details" in out["relations"]
    assert out["metrics"]["amount"]["relation"] == "order_details"
    assert any("order details" in w and "order_details" in w for w in warnings), warnings


def test_the_written_file_is_validated_even_though_it_carries_placeholders(tmp_path):
    """`_write_metrics` gated its own validation on `<your-account>` being
    absent, and both importers always emit it — so the check never ran (#337)."""
    source = tmp_path / "v.yaml"
    source.write_text(ILLEGAL_NAMES)
    out = tmp_path / "metrics.yaml"
    result = CliRunner().invoke(app, ["import", "cortex", str(source), "-o", str(out)])
    assert result.exit_code == 0, result.output
    written = YAML(typ="safe").load(io.StringIO(out.read_text()))
    assert "<your-account>" in written["source"]["account"]
    assert set(written["metrics"]) == {"select_metric"}
    parse_metrics_file(out.read_text())


def test_an_output_sqldash_cannot_load_is_an_error_at_the_write_not_at_the_next_lint(
    tmp_path, monkeypatch
):
    """The slugifier is meant to make this unreachable; the check behind it is
    what keeps a future importer change from shipping an unloadable file."""
    monkeypatch.setattr("sqldash.semantics.naming.slugify_name", lambda value, *a, **k: value)
    source = tmp_path / "v.yaml"
    source.write_text(ILLEGAL_NAMES)
    out = tmp_path / "metrics.yaml"
    result = CliRunner().invoke(app, ["import", "cortex", str(source), "-o", str(out)])
    assert result.exit_code == 1, result.output
    assert "cannot load" in result.output, result.output
    assert not out.exists()


def test_two_cortex_dimensions_that_slugify_alike_both_survive(tmp_path):
    """Same hole the LookML path had: `order` is reserved and becomes
    `order_field`, colliding with a real `order_field` on the same table. The
    pair failed the model's per-metric uniqueness check and the importer wrote
    nothing at all. #337 review."""
    view = tmp_path / "v.yaml"
    view.write_text(
        "name: V\n"
        "tables:\n"
        "  - name: ORDERS\n"
        "    base_table: {database: DB, schema: S, table: ORDERS}\n"
        "    dimensions:\n"
        '      - {name: "order"}\n'
        "      - {name: order_field}\n"
        "    metrics:\n"
        "      - {name: revenue, expr: SUM(AMOUNT)}\n"
    )
    out, warnings = parse_semantic_view(view.read_text())
    dims = out["metrics"]["revenue"]["dimensions"]
    assert [d["name"] for d in dims] == ["order_field", "order_field_2"]
    assert any("'order_field' is already taken" in w for w in warnings), warnings
    MetricsFile.model_validate(out)


def test_a_reserved_column_keeps_the_case_snowflake_stored_it_in(tmp_path):
    """A semantic view names a column as Snowflake stored it, so a reserved one
    arrives uppercase and must go out quoted in that same case: `"ORDER"` is the
    stored identifier, while bare `ORDER` does not parse on DuckDB or Postgres
    and lowercase `"order"` would be a different column. #337 review."""
    view = (
        "name: V\n"
        "tables:\n"
        "  - name: ORDERS\n"
        "    base_table: {database: DB, schema: S, table: ORDERS}\n"
        "    dimensions:\n"
        "      - {name: ORDER}\n"
        "    metrics:\n"
        "      - {name: revenue, expr: SUM(AMOUNT)}\n"
    )
    out, _ = parse_semantic_view(view)
    dims = out["metrics"]["revenue"]["dimensions"]
    assert dims[0]["name"] == "order_field"
    assert dims[0]["expr"] == '"ORDER"'
    MetricsFile.model_validate(out)


ISSUE_441_VIEW = """
database: ANALYTICS
schema: PUBLIC
tables:
  - name: ORDERS
    base_table: {database: ANALYTICS, schema: PUBLIC, table: ORDERS}
    dimensions:
      - {name: REGION, sql: REGION, data_type: TEXT}
      - {name: ORDER_DATE, sql: ORDER_DATE, data_type: DATE}
    time_dimensions:
      - {name: ORDER_DATE, sql: ORDER_DATE, type: DAY}
    measures:
      - {name: REVENUE, expr: SUM(AMOUNT), data_type: NUMBER}
"""


def test_a_date_listed_as_both_dimension_and_time_dimension_is_not_duplicated():
    """Snowflake semantic views commonly list a date column in both
    `dimensions` and `time_dimensions`. Copying both onto the metric failed
    uniqueness (`dimension names must be unique within a metric`) on otherwise
    valid input. #441. lookml already skips type time/date from `dims`."""
    out, _ = parse_semantic_view(ISSUE_441_VIEW)
    metric = out["metrics"]["revenue"]
    td_name = metric["time_dimension"]["name"]
    assert td_name == "order_date"
    dim_names = [d["name"] for d in metric.get("dimensions", [])]
    assert dim_names == ["region"]
    assert td_name not in dim_names
    MetricsFile.model_validate(out)
    parse_metrics_file(render_yaml(out))

    metrics_view = ISSUE_441_VIEW.replace("    measures:\n", "    metrics:\n")
    out, _ = parse_semantic_view(metrics_view)
    metric = out["metrics"]["revenue"]
    assert metric["time_dimension"]["name"] == "order_date"
    assert [d["name"] for d in metric["dimensions"]] == ["region"]
    MetricsFile.model_validate(out)


def test_import_cortex_cli_accepts_a_date_in_both_dimension_lists(tmp_path):
    source = tmp_path / "v.yaml"
    source.write_text(ISSUE_441_VIEW)
    out = tmp_path / "metrics.yaml"
    result = CliRunner().invoke(app, ["import", "cortex", str(source), "-o", str(out)])
    assert result.exit_code == 0, result.output
    written = YAML(typ="safe").load(io.StringIO(out.read_text()))
    metric = written["metrics"]["revenue"]
    assert metric["time_dimension"]["name"] == "order_date"
    assert [d["name"] for d in metric["dimensions"]] == ["region"]
    parse_metrics_file(out.read_text())
    store = DashboardStore(tmp_path)
    findings = lint_project(store, SemanticLayer(store))
    assert not [f for f in findings if f.level == "error"], findings


_MACRO_METRICS = """
source: {type: snowflake, account: a, database: DB, schema: PUBLIC}
relations:
  weekly:
    sql: "SELECT SQLDASH_TRUNC('week', ordered_at) AS wk, amount, signed_up_at FROM orders"
metrics:
  active_weeks:
    relation: weekly
    expr: "COUNT(DISTINCT SQLDASH_TRUNC('week', ordered_at))"
    filters: ["SQLDASH_TRUNC('year', ordered_at) >= '2026-01-01'"]
    time_dimension: {name: ordered_at, expr: "SQLDASH_TRUNC('day', ordered_at)", grain: day}
    dimensions:
      - {name: cohort, expr: "SQLDASH_TRUNC('month', signed_up_at)"}
"""


def test_cortex_export_resolves_the_trunc_macro(tmp_path):
    """A semantic view is executed by Snowflake, so the macro cannot ship in one.
    It reached all four author SQL sites here — base_table definition, dimension
    expr, time dimension expr, metric expr and filter — with no warning."""
    (tmp_path / "metrics.yaml").write_text(_MACRO_METRICS)
    store = DashboardStore(tmp_path)
    doc, warnings = build_semantic_view(SemanticLayer(store), store, "v")
    text = render_yaml(doc)
    assert "SQLDASH_TRUNC" not in text, text
    table = doc["tables"][0]
    assert table["base_table"]["definition"].startswith("SELECT DATE_TRUNC('week', ordered_at)")
    assert table["dimensions"][0]["expr"] == "DATE_TRUNC('month', signed_up_at)"
    assert table["time_dimensions"][0]["expr"] == "DATE_TRUNC('day', ordered_at)"
    assert table["metrics"][0]["expr"] == (
        "COUNT(DISTINCT CASE WHEN (DATE_TRUNC('year', ordered_at) >= '2026-01-01') "
        "THEN DATE_TRUNC('week', ordered_at) END)"
    )
    assert not [w for w in warnings if "SQLDASH" in w], warnings


def test_imported_metrics_yaml_carries_no_yaml_anchors():
    """The converter reuses one list for every metric sharing a dimension, and
    the round-trip dumper wrote the second use as `synonyms: *id001`. The file
    loads, so nothing caught it, but a generated metrics.yaml is meant to be read
    and edited by hand and an anchor pointing into another metric is not what
    anyone wrote. #681."""
    out, _ = parse_semantic_view(MEASURES_MODEL)
    text = render_yaml(out)
    assert "&id" not in text, text
    assert "*id" not in text, text
    assert text.count("- area") == len(out["metrics"])
    reloaded = YAML(typ="safe").load(text)
    assert reloaded["metrics"]["claim_amount"]["dimensions"] == [
        {"name": "region", "synonyms": ["area"]}
    ]


# What SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW returned, live, for a view created
# from `sqldash export cortex` of the demo project: names come back the way
# Snowflake stores an unquoted identifier, while exprs stay as written.
READ_BACK_VIEW = """
name: SQLDASH_T_SV
tables:
  - name: ORDERS
    base_table: {database: SQLDASH_DEMO, schema: PUBLIC, table: ORDERS}
    dimensions:
      - {name: CATEGORY, description: Product category, expr: category, data_type: VARCHAR}
      - {name: REGION, description: Sales region, expr: region, data_type: VARCHAR}
    metrics:
      - {name: AVG_ORDER_VALUE, expr: "ROUND((SUM(amount)) / NULLIF((COUNT(*)), 0), 2)"}
      - {name: ORDER_COUNT, expr: COUNT(*), access_modifier: public_access}
      - name: REVENUE
        synonyms: [sales, turnover]
        expr: SUM(amount)
        access_modifier: public_access
    time_dimensions:
      - {name: ORDER_DATE, expr: order_date, data_type: DATE, grain: day}
"""


def _demo_over(tmp_path, imported: dict) -> DashboardStore:
    """The demo project with its metrics.yaml replaced by an import, pointed
    back at the demo's own orders so the imported metrics can actually run."""
    create_demo(tmp_path)
    imported["source"] = {"type": "duckdb", "attach_files": True}
    imported["relations"] = {name: {"table": "orders"} for name in imported["relations"]}
    (tmp_path / ".sqldash" / "metrics.yaml").write_text(render_yaml(imported))
    return DashboardStore(tmp_path)


def _query(target, *args) -> str:
    result = CliRunner().invoke(
        app, ["metric", "query", "revenue", "--target", str(target), "-f", "json", *args]
    )
    assert result.exit_code == 0, result.output
    return result.output


def test_names_snowflake_read_back_uppercase_fold_to_what_the_dashboards_reference(tmp_path):
    """A view created from `revenue` reads back as `REVENUE`. Snowflake treats
    the two as one identifier, but a metrics.yaml key is case-sensitive, so every
    tile, agent and query over the imported file referenced a metric that no
    longer existed and the result columns came back as REGION/REVENUE."""
    out, warnings = parse_semantic_view(READ_BACK_VIEW)
    assert set(out["relations"]) == {"orders"}
    assert set(out["metrics"]) == {"avg_order_value", "order_count", "revenue"}
    revenue = out["metrics"]["revenue"]
    assert revenue["relation"] == "orders"
    assert revenue["time_dimension"] == {"name": "order_date", "grain": "day"}
    assert [d["name"] for d in revenue["dimensions"]] == ["category", "region"]
    assert not [w for w in warnings if "imported as" in w], warnings

    store = _demo_over(tmp_path / "imported", out)
    findings = lint_project(store, SemanticLayer(store))
    assert not [f for f in findings if f.level == "error"], findings
    create_demo(tmp_path / "original")
    for args in ((), ("-d", "region")):
        assert _query(tmp_path / "imported", *args) == _query(tmp_path / "original", *args)


def test_a_mixed_case_name_came_from_a_quoted_identifier_and_is_kept():
    out, _ = parse_semantic_view(
        "name: V\ntables:\n"
        "  - name: Orders\n    base_table: {database: D, schema: S, table: ORDERS}\n"
        "    dimensions: [{name: ShipRegion, expr: '\"ShipRegion\"'}, {name: CITY}]\n"
        "    metrics: [{name: NetRevenue, expr: SUM(AMOUNT)}, {name: GROSS_2, expr: SUM(G)}]\n"
    )
    assert set(out["metrics"]) == {"NetRevenue", "gross_2"}
    assert out["metrics"]["NetRevenue"]["relation"] == "Orders"
    assert out["metrics"]["NetRevenue"]["dimensions"] == [
        {"name": "ShipRegion", "expr": '"ShipRegion"'},
        {"name": "city"},
    ]


# SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW, live, for a view whose metrics were
# written over facts. Snowflake requires the `<table>.<fact>` spelling in a
# metric and returns the fact names the way it stores them.
QUALIFIED_FACTS_VIEW = """
name: SQLDASH_T_C_FACTS
tables:
  - name: ORDERS
    base_table: {database: SQLDASH_DEMO, schema: PUBLIC, table: ORDERS}
    facts:
      - {name: AMOUNT_CENTS, expr: amount * 100, data_type: "NUMBER(13,2)"}
      - {name: AMOUNT_USD, expr: amount, data_type: "NUMBER(10,2)"}
      - {name: TAXED, expr: orders.amount_usd * 1.1}
    metrics:
      - {name: REVENUE, expr: SUM(orders.amount_usd)}
      - {name: REVENUE_CENTS, expr: SUM(orders.amount_cents)}
      - {name: REVENUE_TAXED, expr: SUM(ORDERS . TAXED)}
      - {name: OTHER_TABLE, expr: SUM(refunds.amount_usd)}
"""


def test_a_fact_referenced_through_its_table_is_inlined_whatever_its_case():
    """`SUM(orders.amount_usd)` over a fact read back as `AMOUNT_USD` was left
    as written and failed with "invalid identifier ORDERS.AMOUNT_USD"; with a
    lowercase fact the table prefix stayed and `SUM(orders.(amount * 100))` was a
    syntax error. A name another table qualifies is not this table's fact."""
    out, _ = parse_semantic_view(QUALIFIED_FACTS_VIEW)
    exprs = {name: metric["expr"] for name, metric in out["metrics"].items()}
    assert exprs == {
        "revenue": "SUM(amount)",
        "revenue_cents": "SUM((amount * 100))",
        "revenue_taxed": "SUM((amount * 1.1))",
        "other_table": "SUM(refunds.amount_usd)",
    }
    orders = "(SELECT 10.5 AS amount UNION ALL SELECT 4.5) t"
    for name, expected in (("revenue", 15), ("revenue_cents", 1500), ("revenue_taxed", 16.5)):
        value = duckdb.sql(f"SELECT {exprs[name]} FROM {orders}").fetchone()[0]
        assert float(value) == pytest.approx(expected), name


# Created live on Snowflake and read back with SYSTEM$READ_YAML_FROM_SEMANTIC_VIEW
# (trimmed): a view-level metric over two table metrics, a dimension-only table
# reached through a relationship, and the fields a sqldash model has no room for.
VIEW_LEVEL_VIEW = """
name: SQLDASH_T_C_DROPS
tables:
  - name: ORDERS
    synonyms: [sales_orders]
    base_table: {database: SQLDASH_DEMO, schema: PUBLIC, table: ORDERS}
    dimensions:
      - {name: CATEGORY, expr: category}
      - {name: REGION, synonyms: [area], expr: region, sample_values: [us, eu]}
    metrics:
      - {name: ORDER_COUNT, expr: COUNT(*)}
      - {name: REVENUE, synonyms: [sales], expr: SUM(amount)}
    time_dimensions:
      - {name: ORDER_DATE, synonyms: [day], expr: order_date}
  - name: REGIONS
    base_table: {database: SQLDASH_DEMO, schema: PUBLIC, table: SQLDASH_T_C_REGIONS}
    primary_key: {columns: [REGION]}
    dimensions:
      - {name: MANAGER, synonyms: [owner], expr: manager}
metrics:
  - name: REVENUE_PER_ORDER
    synonyms: []
    description: Average revenue per order
    expr: orders.revenue / orders.order_count
  - {name: MIXED, expr: orders.revenue - regions.headcount}
relationships:
  - name: ORDERS_TO_REGIONS
    left_table: ORDERS
    right_table: REGIONS
    relationship_columns:
      - {left_column: REGION, right_column: REGION}
"""


def test_a_view_level_metric_over_one_tables_metrics_imports_as_derived():
    """`metrics:` at the top of a view was never read: REVENUE_PER_ORDER was
    missing from the output with no warning at all."""
    out, warnings = parse_semantic_view(VIEW_LEVEL_VIEW)
    metric = out["metrics"]["revenue_per_order"]
    assert metric["derived"] == "{revenue} / {order_count}"
    assert metric["description"] == "Average revenue per order"
    assert metric["time_dimension"] == out["metrics"]["revenue"]["time_dimension"]
    assert metric["dimensions"] == out["metrics"]["revenue"]["dimensions"]
    assert "mixed" not in out["metrics"]
    assert any("view-level metric 'MIXED'" in w and "'regions.headcount'" in w for w in warnings), (
        warnings
    )
    parse_metrics_file(render_yaml(out))


def test_a_derived_view_metric_returns_what_its_components_do(tmp_path):
    create_demo(tmp_path)
    out, _ = parse_semantic_view(VIEW_LEVEL_VIEW)
    out["source"] = {"type": "duckdb", "attach_files": True}
    out["relations"] = {"orders": {"table": "orders"}}
    (tmp_path / ".sqldash" / "metrics.yaml").write_text(render_yaml(out))
    values = {}
    for name in ("revenue", "order_count", "revenue_per_order"):
        result = CliRunner().invoke(
            app, ["metric", "query", name, "--target", str(tmp_path), "-f", "json"]
        )
        assert result.exit_code == 0, result.output
        values[name] = float(next(iter(json.loads(result.output)[0].values())))
    assert values["revenue_per_order"] == pytest.approx(values["revenue"] / values["order_count"])


def test_what_import_cortex_cannot_carry_is_named_not_dropped():
    """Table synonyms, sample_values, time dimension synonyms, relationships and
    a dimension-only table all vanished without a word; the dimension-only
    table even left an empty relation behind."""
    out, warnings = parse_semantic_view(VIEW_LEVEL_VIEW)
    assert set(out["relations"]) == {"orders"}
    text = "\n".join(warnings)
    for expected in (
        "table 'ORDERS': synonyms (sales_orders) dropped",
        "table 'ORDERS': sample_values on REGION dropped",
        "table 'ORDERS': synonyms on time dimension ORDER_DATE dropped",
        "table 'REGIONS' skipped: it has no metrics or facts, so its dimensions (MANAGER)",
        "relationships ORDERS_TO_REGIONS skipped",
    ):
        assert expected in text, text


@pytest.mark.parametrize(
    ("expr", "reason"),
    [
        ("SUM(orders.amount)", "'orders.amount' is not a metric"),
        ("orders.revenue / amount", "'amount' is not a table's metric"),
        ("42", "does not reference a table's metric"),
    ],
)
def test_a_view_level_metric_that_is_not_a_derived_metric_is_named(expr, reason):
    view = (
        "name: V\ntables:\n"
        "  - name: orders\n    base_table: {database: D, schema: S, table: O}\n"
        "    metrics: [{name: revenue, expr: SUM(amount)}]\n"
        f"metrics: [{{name: odd, expr: '{expr}'}}]\n"
    )
    out, warnings = parse_semantic_view(view)
    assert set(out["metrics"]) == {"revenue"}
    assert any("view-level metric 'odd'" in w and reason in w for w in warnings), warnings


# A view read back from Snowflake, live, with a named filter on its table.
# Querying it gave ORDER_COUNT 6262, every row: the filter is only offered to
# Cortex Analyst.
NAMED_FILTER_VIEW = """
name: SQLDASH_T_C_NAMED
tables:
  - name: orders
    base_table: {database: SQLDASH_DEMO, schema: PUBLIC, table: ORDERS}
    dimensions:
      - {name: region, expr: region}
    time_dimensions:
      - {name: order_date, expr: order_date}
    filters:
      - {name: big_orders, synonyms: [large orders], expr: amount > 150}
      - {expr: "region <> 'eu'"}
    metrics:
      - {name: revenue, expr: SUM(amount)}
      - {name: order_count, expr: COUNT(*)}
"""


def test_a_named_table_filter_is_not_applied_to_every_metric(tmp_path):
    """Importing `big_orders` as a filter on every metric of the table changed
    every number: order_count went from Snowflake's 6262 to 2109, with only a
    'review' warning. It is named and left out instead."""
    out, warnings = parse_semantic_view(NAMED_FILTER_VIEW)
    assert not [m for m in out["metrics"].values() if "filters" in m], out["metrics"]
    named = [w for w in warnings if "named filter" in w]
    assert len(named) == 2, warnings
    assert "'big_orders' (amount > 150)" in named[0]
    assert "(region <> 'eu')" in named[1]

    create_demo(tmp_path / "original")
    create_demo(tmp_path / "imported")
    out["source"] = {"type": "duckdb", "attach_files": True}
    out["relations"] = {"orders": {"table": "orders"}}
    (tmp_path / "imported" / ".sqldash" / "metrics.yaml").write_text(render_yaml(out))
    for metric in ("order_count", "revenue"):
        results = [
            CliRunner().invoke(
                app, ["metric", "query", metric, "--target", str(tmp_path / side), "-f", "json"]
            )
            for side in ("original", "imported")
        ]
        assert all(r.exit_code == 0 for r in results), [r.output for r in results]
        assert results[0].output == results[1].output, metric


IDENTITY_FACT_VIEW = """
name: SQLDASH_T_CX_IDENT
tables:
  - name: sales
    base_table: {database: SQLDASH_DEMO, schema: PUBLIC, table: ORDERS}
    facts:
      - {name: amount, expr: amount}
      - {name: amount_cents, expr: amount * 100}
      - {name: taxed, expr: sales.amount * 1.1}
    metrics:
      - {name: revenue, expr: SUM(sales.amount)}
      - {name: revenue_cents, expr: SUM(sales.amount_cents)}
      - {name: revenue_taxed, expr: SUM(sales.taxed)}
"""


def test_a_qualified_reference_to_an_identity_fact_drops_the_table_prefix():
    """A fact whose name is its expr was never treated as an alias, so
    `SUM(sales.amount)` kept the logical table name, which is not a relation
    alias once imported: "invalid identifier SALES.AMOUNT" at query time."""
    out, _ = parse_semantic_view(IDENTITY_FACT_VIEW)
    exprs = {name: metric["expr"] for name, metric in out["metrics"].items()}
    assert exprs == {
        "revenue": "SUM(amount)",
        "revenue_cents": "SUM((amount * 100))",
        "revenue_taxed": "SUM((amount * 1.1))",
    }
    orders = "(SELECT 10.5 AS amount UNION ALL SELECT 4.5) t"
    for name, expected in (("revenue", 15), ("revenue_cents", 1500), ("revenue_taxed", 16.5)):
        value = duckdb.sql(f"SELECT {exprs[name]} FROM {orders}").fetchone()[0]
        assert float(value) == pytest.approx(expected), name


SESSION_TIME = """
source: {type: snowflake, account: a, database: D, schema: S, username: u}
relations:
  events: {table: EVENTS}
metrics:
  event_count:
    relation: events
    expr: COUNT(*)
    time_dimension: {name: created_at, grain: month, timezone: session}
  revenue:
    relation: events
    expr: SUM(amount)
    time_dimension: {name: created_at, grain: month, timezone: session}
"""


def test_a_session_time_dimension_exports_as_an_ltz_cast_and_round_trips(tmp_path):
    """Cortex Analyst truncates the time dimension itself, and DATE_TRUNC over a
    TIMESTAMP_TZ keeps each row's offset, so a raw expr split one month into a
    bucket per offset (checked live: 3 rows for 3 offsets, 1 row through the cast).
    The export used to drop `timezone: session` without a word."""
    (tmp_path / "metrics.yaml").write_text(SESSION_TIME)
    store = DashboardStore(tmp_path)
    doc, warnings = build_semantic_view(SemanticLayer(store), store, "v")
    assert doc["tables"][0]["time_dimensions"] == [
        {"name": "created_at", "expr": "CAST(created_at AS TIMESTAMP_LTZ)", "grain": "month"}
    ]
    assert not [w for w in warnings if "time dimension" in w], warnings
    imported, _ = parse_semantic_view(render_yaml(doc))
    for metric in imported["metrics"].values():
        assert metric["time_dimension"] == {
            "name": "created_at",
            "grain": "month",
            "timezone": "session",
        }
    parse_metrics_file(render_yaml(imported))


@pytest.mark.parametrize(
    ("expr", "expected"),
    [
        pytest.param("created_at", {"expr": "created_at"}, id="no-cast"),
        pytest.param(
            "cast( ts as timestamp_ltz )", {"expr": "ts", "timezone": "session"}, id="any-case"
        ),
        pytest.param(
            "CAST(CONVERT_TIMEZONE('UTC', ts) AS TIMESTAMP_LTZ)",
            {"expr": "CONVERT_TIMEZONE('UTC', ts)", "timezone": "session"},
            id="nested-call",
        ),
        pytest.param(
            "CAST(a AS DATE) + CAST(b AS TIMESTAMP_LTZ)",
            {"expr": "CAST(a AS DATE) + CAST(b AS TIMESTAMP_LTZ)"},
            id="not-one-cast",
        ),
    ],
)
def test_only_a_whole_ltz_cast_imports_as_timezone_session(expr, expected):
    view = f"""
name: v
tables:
  - name: events
    base_table: {{database: D, schema: S, table: EVENTS}}
    time_dimensions:
      - {{name: t, expr: "{expr}"}}
    metrics:
      - {{name: event_count, expr: COUNT(*)}}
"""
    out, _ = parse_semantic_view(view)
    assert out["metrics"]["event_count"]["time_dimension"] == {
        "name": "t",
        "grain": "day",
        **expected,
    }
