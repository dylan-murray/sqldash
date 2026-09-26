import sqlite3
from datetime import date, timedelta

import duckdb
import pytest
import yaml

from sqldash.execution import ExecutionRegistry
from sqldash.models.semantics import MetricsFile
from sqldash.project.store import DashboardStore
from sqldash.semantics import SemanticError, SemanticLayer
from sqldash.semantics.compiler import MetricQuery, compile_metric
from sqldash.semantics.cortex import render_yaml
from sqldash.semantics.layer import WorkspaceLayer
from sqldash.semantics.lookml import export_lookml, import_lookml

VIEW = """
view: orders {
  sql_table_name: ANALYTICS.PUBLIC.ORDERS ;;

  dimension: region {
    sql: ${TABLE}.region ;;
    description: "Sales region"
  }

  dimension: category {
    sql: ${TABLE}.category ;;
  }

  dimension_group: ordered {
    type: time
    timeframes: [date, week, month]
    sql: ${TABLE}.order_date ;;
  }

  measure: total_revenue {
    type: sum
    sql: ${TABLE}.amount ;;
    description: "Total order revenue"
  }

  measure: order_count {
    type: count
  }

  measure: weird_percentile {
    type: percentile
    sql: ${TABLE}.amount ;;
  }

  measure: cross_ref {
    type: sum
    sql: ${orders.amount} ;;
  }
}
"""


@pytest.fixture
def view_file(tmp_path):
    path = tmp_path / "orders.view.lkml"
    path.write_text(VIEW)
    return path


def test_lookml_import(view_file):
    doc, warnings = import_lookml(view_file)
    assert doc["relations"]["orders"] == {"table": "ANALYTICS.PUBLIC.ORDERS"}

    revenue = doc["metrics"]["total_revenue"]
    assert revenue["expr"] == "SUM(amount)"
    assert revenue["description"] == "Total order revenue"
    assert revenue["time_dimension"] == {"name": "ordered", "grain": "day", "expr": "order_date"}
    assert {d["name"] for d in revenue["dimensions"]} == {"region", "category"}

    assert doc["metrics"]["order_count"]["expr"] == "COUNT(*)"
    assert "weird_percentile" not in doc["metrics"]
    assert "cross_ref" not in doc["metrics"]
    assert any("percentile" in w for w in warnings)
    assert any("cross" in w or "${orders.amount}" in w for w in warnings)


def test_lookml_import_validates_as_metrics_file(view_file):
    doc, _ = import_lookml(view_file)
    doc["source"] = {
        "type": "snowflake",
        "account": "a",
        "database": "ANALYTICS",
        "schema": "PUBLIC",
        "username": "u",
    }
    validated = MetricsFile.model_validate(doc)
    assert validated.metrics["total_revenue"].relation == "orders"


def test_lookml_import_no_views(tmp_path):
    (tmp_path / "empty.lkml").write_text("# nothing here\n")
    with pytest.raises(SemanticError, match="no importable measures"):
        import_lookml(tmp_path)


def test_a_parse_error_is_named_rather_than_reported_as_no_measures(tmp_path):
    """A file that never parsed is recorded as a warning, and the final raise
    discarded them — so a syntax error was reported as "no importable measures
    found", sending the user to look at their measures instead of at line 3.
    """
    broken = tmp_path / "broken.lkml"
    broken.write_text("view: broken {\n  dimension: x {\n")
    with pytest.raises(SemanticError) as excinfo:
        import_lookml(broken)
    message = str(excinfo.value)
    assert "parse error" in message, message
    assert "broken.lkml" in message, message
    assert "Traceback" not in message, message
    assert ".venv" not in message, message


def test_a_view_with_no_measures_still_says_just_that(tmp_path):
    """The plain message is right when the file parsed and simply has nothing
    to import — the detail is only there to explain an unexplained emptiness."""
    view = tmp_path / "v.lkml"
    view.write_text("view: v {\n  sql_table_name: public.t ;;\n  dimension: id { sql: 1 ;; }\n}\n")
    with pytest.raises(SemanticError) as excinfo:
        import_lookml(view)
    assert "parse error" not in str(excinfo.value), str(excinfo.value)


SAME_VIEW_REFS = """
view: orders {
  sql_table_name: public.orders ;;
  dimension: amount { type: number sql: ${TABLE}.amount_usd ;; }
  dimension: region { type: string sql: ${TABLE}.region ;; }
  measure: revenue { type: sum sql: ${amount} ;; }
  measure: avg_ticket { type: average sql: ${amount} ;; }
  measure: distinct_regions { type: count_distinct sql: ${region} ;; }
  measure: order_count { type: count }
  measure: from_other { type: sum sql: ${customers.spend} ;; }
  measure: no_sql_sum { type: sum }
}
"""


def test_a_measure_over_a_same_view_dimension_imports(tmp_path):
    """`sql: ${amount}` inside a measure is a reference to this view's own
    `amount` dimension — the dominant way a LookML measure names what it
    aggregates. Treating every `${...}` as cross-field rejected it, so an import
    kept only `type: count` measures and the feature barely worked.
    """
    view = tmp_path / "v.lkml"
    view.write_text(SAME_VIEW_REFS)
    out, _ = import_lookml(view)
    exprs = {name: m["expr"] for name, m in out["metrics"].items()}
    # ...and it resolves to the column the dimension points at, not its name.
    assert exprs["revenue"] == "SUM(amount_usd)"
    assert exprs["avg_ticket"] == "AVG(amount_usd)"
    assert exprs["distinct_regions"] == "COUNT(DISTINCT region)"
    assert exprs["order_count"] == "COUNT(*)"


def test_a_reference_this_view_cannot_satisfy_is_still_refused(tmp_path):
    """The guard exists for real cross-view references; only the same-view case
    was collateral."""
    view = tmp_path / "v.lkml"
    view.write_text(SAME_VIEW_REFS)
    out, warnings = import_lookml(view)
    assert "from_other" not in out["metrics"]
    assert any("${customers.spend}" in w for w in warnings), warnings


def test_a_skipped_measure_is_reported_once(tmp_path):
    """A rejected reference produced two warnings for one measure — the reason,
    then a generic "no usable sql" that restated it."""
    view = tmp_path / "v.lkml"
    view.write_text(SAME_VIEW_REFS)
    _out, warnings = import_lookml(view)
    assert len([w for w in warnings if "from_other" in w]) == 1, warnings
    assert any("no sql to aggregate" in w and "no_sql_sum" in w for w in warnings), warnings


CHAINED_AND_TIMEFRAMES = """
view: orders {
  sql_table_name: public.orders ;;
  dimension: gross_amount { sql: ${TABLE}.gross ;; }
  dimension: tax { sql: ${TABLE}.tax ;; }
  dimension: net { sql: ${gross_amount} - ${tax} ;; }
  dimension_group: ordered { type: time sql: ${TABLE}.ordered_at ;; }
  measure: total_net { type: sum sql: ${net} ;; }
  measure: distinct_days { type: count_distinct sql: ${ordered_date} ;; }
  measure: distinct_months { type: count_distinct sql: ${ordered_month} ;; }
  measure: last_order { type: max sql: ${ordered_raw} ;; }
  measure: by_weekday { type: count_distinct sql: ${ordered_day_of_week} ;; }
  measure: count_with_ref { type: count sql: ${customers.spend} ;; }
}
"""


def test_a_measure_over_a_computed_dimension_imports(tmp_path):
    """Substituting once left `${gross_amount}` in the result and then reported
    it as a cross-field reference — naming a field this very view defines, which
    the resolver's own docstring says is impossible. Fields resolve to a fixpoint
    now, the way cortex fact aliases do.
    """
    view = tmp_path / "v.lkml"
    view.write_text(CHAINED_AND_TIMEFRAMES)
    out, warnings = import_lookml(view)
    assert out["metrics"]["total_net"]["expr"] == "SUM(gross - tax)"
    assert not [w for w in warnings if "total_net" in w], warnings


def test_a_measure_over_a_dimension_group_timeframe_imports_at_that_grain(tmp_path):
    """A dimension_group generates `ordered_date`, `ordered_week`, ... and
    measures reference those, never the bare group name — so mapping only the
    bare name supported a spelling nobody writes.

    The timeframe is the point of the reference, not decoration: resolving
    `${ordered_date}` to the bare column counts distinct *timestamps*, so a
    table with a time component imports a metric that quietly returns a
    different number than the one it was copied from.

    The grain is carried as the neutral macro, not one dialect's DATE_TRUNC —
    the importer does not know the target source, and the file is portable.
    """
    view = tmp_path / "v.lkml"
    view.write_text(CHAINED_AND_TIMEFRAMES)
    out, _ = import_lookml(view)
    assert (
        out["metrics"]["distinct_days"]["expr"]
        == "COUNT(DISTINCT SQLDASH_TRUNC('day', ordered_at))"
    )
    assert (
        out["metrics"]["distinct_months"]["expr"]
        == "COUNT(DISTINCT SQLDASH_TRUNC('month', ordered_at))"
    )


def test_the_raw_timeframe_is_the_untruncated_column(tmp_path):
    """`raw` and `time` are the timestamp itself — truncating them would be the
    same error in the other direction."""
    view = tmp_path / "v.lkml"
    view.write_text(CHAINED_AND_TIMEFRAMES)
    out, _ = import_lookml(view)
    assert out["metrics"]["last_order"]["expr"] == "MAX(ordered_at)"


def test_a_timeframe_with_no_sql_equivalent_is_skipped_not_guessed(tmp_path):
    """`day_of_week` extracts rather than truncates, so there is no DATE_TRUNC
    for it. Importing it as the raw column would be a wrong number; refusing it
    is at least honest."""
    view = tmp_path / "v.lkml"
    view.write_text(CHAINED_AND_TIMEFRAMES)
    out, warnings = import_lookml(view)
    assert "by_weekday" not in out["metrics"]
    assert any("day_of_week" in w and "no SQL equivalent" in w for w in warnings), warnings


def test_a_count_measure_is_not_reported_as_skipped_when_it_ships(tmp_path):
    """COUNT(*) ignores the column, so a `type: count` measure's sql is never
    used. Resolving it anyway warned "skipped" for a measure that then appeared
    in the output — the same false warning this importer had for rejections."""
    view = tmp_path / "v.lkml"
    view.write_text(CHAINED_AND_TIMEFRAMES)
    out, warnings = import_lookml(view)
    assert out["metrics"]["count_with_ref"]["expr"] == "COUNT(*)"
    assert not [w for w in warnings if "count_with_ref" in w], warnings


def test_fields_defined_in_a_cycle_are_reported_not_looped_on(tmp_path):
    view = tmp_path / "v.lkml"
    view.write_text(
        "view: orders {\n"
        "  sql_table_name: public.orders ;;\n"
        "  dimension: a { sql: ${b} + 1 ;; }\n"
        "  dimension: b { sql: ${a} * 2 ;; }\n"
        "  dimension: ok { sql: ${TABLE}.ok ;; }\n"
        "  measure: fine { type: sum sql: ${ok} ;; }\n"
        "}\n"
    )
    out, warnings = import_lookml(view)
    assert any("cycle" in w for w in warnings), warnings
    # ...and the rest of the view still imports.
    assert out["metrics"]["fine"]["expr"] == "SUM(ok)"


def test_a_leftover_reference_is_reported_by_its_actual_cause(tmp_path):
    """Every unresolved `${...}` was called a "cross-field reference", including
    names the view defines itself — telling an author their own view was somebody
    else's. The reason has to name what actually happened."""
    view = tmp_path / "v.lkml"
    view.write_text(
        "view: orders {\n"
        "  sql_table_name: public.orders ;;\n"
        "  dimension: amount { sql: ${TABLE}.amount ;; }\n"
        "  measure: gross { type: sum sql: ${amount} ;; }\n"
        "  measure: over_measure { type: sum sql: ${gross} * 2 ;; }\n"
        "  measure: elsewhere { type: sum sql: ${users.age} ;; }\n"
        "  measure: ghost { type: sum sql: ${nonexistent} ;; }\n"
        "}\n"
    )
    _out, warnings = import_lookml(view)
    reasons = {w.split(": ", 2)[-1] for w in warnings}
    assert any("another measure" in r and "${gross}" in r for r in reasons), warnings
    assert any("another view" in r and "${users.age}" in r for r in reasons), warnings
    assert any("names no field this view defines" in r for r in reasons), warnings
    assert not any("cross-field" in w for w in warnings), warnings


def test_a_field_in_a_cycle_is_named_as_such_not_as_cross_field(tmp_path):
    """A cyclic pair is defined by the view, so "cross-field" was the one thing
    it certainly was not."""
    view = tmp_path / "v.lkml"
    view.write_text(
        "view: orders {\n"
        "  sql_table_name: public.orders ;;\n"
        "  dimension: a { sql: ${b} + 1 ;; }\n"
        "  dimension: b { sql: ${a} * 2 ;; }\n"
        "  dimension: ok { sql: ${TABLE}.ok ;; }\n"
        "  measure: fine { type: sum sql: ${ok} ;; }\n"
        "  measure: cyclic { type: sum sql: ${a} ;; }\n"
        "}\n"
    )
    _out, warnings = import_lookml(view)
    assert any("cyclic" in w and "reference cycle" in w for w in warnings), warnings
    assert not any("cross-field" in w for w in warnings), warnings


def test_compact_inline_list_then_more_fields_still_imports(tmp_path):
    """lkml 1.3.7's lexer never returns on `[a, b]; next: …` on one line, so
    compact LookML — valid, and common — hung `import lookml` forever."""
    view = tmp_path / "orders.view.lkml"
    view.write_text(
        "view: orders {\n"
        "  sql_table_name: public.orders ;;\n"
        "  dimension_group: ordered { type: time; "
        "timeframes: [date, week, month]; sql: ${TABLE}.order_date ;; }\n"
        "  measure: total_revenue { type: sum; sql: ${TABLE}.amount ;; }\n"
        "}\n"
    )
    doc, warnings = import_lookml(view)
    assert not any("parse error" in w for w in warnings), warnings
    assert doc["metrics"]["total_revenue"]["expr"] == "SUM(amount)"
    assert doc["metrics"]["total_revenue"]["time_dimension"]["name"] == "ordered"


def test_a_sql_terminator_then_another_field_does_not_eat_the_next_measure(tmp_path):
    """Splitting every `; key:` also split the second `;` of `;;`, so
    `sql: x ;; description: "y"` swallowed the next measure with no warning."""
    view = tmp_path / "orders.view.lkml"
    view.write_text(
        "view: orders {\n"
        "  sql_table_name: public.orders ;;\n"
        '  measure: total_revenue { sql: ${TABLE}.amount ;; description: "Total" }\n'
        "  measure: total_count { type: count; sql: ${TABLE}.id ;; }\n"
        "}\n"
    )
    doc, warnings = import_lookml(view)
    assert not any("parse error" in w for w in warnings), warnings
    assert set(doc["metrics"]) == {"total_revenue", "total_count"}, doc["metrics"]
    assert doc["metrics"]["total_revenue"]["description"] == "Total"


def _table_of(tmp_path, sql_table_name):
    view = tmp_path / "orders.view.lkml"
    view.write_text(
        "view: orders {\n"
        f"  sql_table_name: {sql_table_name} ;;\n"
        "  measure: revenue { type: sum sql: ${TABLE}.amount ;; }\n"
        "}\n"
    )
    doc, _ = import_lookml(view)
    return doc["relations"]["orders"]["table"]


def test_a_partially_quoted_sql_table_name_keeps_its_quotes(tmp_path):
    """`str.strip('`"')` removes every leading and trailing quote character, so
    a reference that only *starts* with a quoted identifier lost its opening
    quote and kept the inner one — `PROD DB".ORDERS`, which no engine parses,
    written verbatim into `relations:` while the file still linted clean (#332).
    """
    assert _table_of(tmp_path, '"PROD DB".ORDERS') == '"PROD DB".ORDERS'


def test_a_partially_backticked_sql_table_name_keeps_its_backticks(tmp_path):
    assert _table_of(tmp_path, "`PROD DB`.ORDERS") == "`PROD DB`.ORDERS"


def test_an_interior_quoted_segment_is_left_alone(tmp_path):
    assert _table_of(tmp_path, 'analytics."PROD DB".ORDERS') == 'analytics."PROD DB".ORDERS'


def test_a_wholly_quoted_plain_identifier_is_unquoted(tmp_path):
    assert _table_of(tmp_path, '"ORDERS"') == "ORDERS"


def test_a_reference_that_merely_starts_and_ends_quoted_is_not_unquoted(tmp_path):
    """`"PROD DB"."ORDERS"` starts and ends with a quote without being one
    quoted identifier; stripping the pair would splice the halves together."""
    assert _table_of(tmp_path, '"PROD DB"."ORDERS"') == '"PROD DB"."ORDERS"'


def test_export_lookml_emits_views_and_warns_on_lossy_metrics(tmp_path):
    (tmp_path / "metrics.yaml").write_text(
        "source: {type: snowflake, account: a, database: D, schema: S, username: u}\n"
        "relations:\n  orders: {table: D.S.ORDERS}\n"
        "metrics:\n"
        "  revenue: {relation: orders, expr: SUM(amount),"
        " time_dimension: {name: d, expr: d, grain: day},"
        " dimensions: [{name: region}]}\n"
        "  order_count: {relation: orders, expr: COUNT(*)}\n"
        "  cumulative_revenue: {relation: orders, expr: SUM(amount), cumulative: true,"
        " time_dimension: {name: d, expr: d, grain: day}}\n"
        "  trailing_28d: {relation: orders, expr: SUM(amount), window: 28 days,"
        " time_dimension: {name: d, expr: d, grain: day}}\n"
        '  aov: {derived: "{revenue} / NULLIF({order_count}, 0)"}\n'
    )
    store = DashboardStore(tmp_path)
    text, warnings = export_lookml(SemanticLayer(store), store)
    assert "view: orders" in text
    assert "sql_table_name: D.S.ORDERS" in text
    assert "type: sum" in text
    assert "${TABLE}.amount" in text
    assert "type: count" in text
    assert "dimension: region" in text
    assert "dimension_group: d" in text
    joined = "\n".join(warnings)
    assert "cumulative_revenue" in joined
    assert "no running total" not in joined, "LookML has type: running_total (#690)"
    assert "running_total is computed over the returned rows" in joined
    assert "trailing_28d" in joined
    assert "window" in joined
    assert "aov" in joined
    assert "derived" in joined


def test_export_lookml_workspace_sql_view_name_has_no_slash(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "metrics.yaml").write_text(
        "source: {type: duckdb, database: ':memory:'}\n"
        "metrics:\n  x: {sql: 'SELECT 1 AS amount', expr: SUM(amount)}\n"
    )
    layer = WorkspaceLayer({"repo": SemanticLayer(DashboardStore(root))})
    text, _ = export_lookml(layer, DashboardStore(root))
    assert "view: x_base" in text
    assert "view: repo/" not in text


def test_export_lookml_sql_relation_uses_derived_table(tmp_path):
    (tmp_path / "metrics.yaml").write_text(
        "source: {type: duckdb, database: ':memory:'}\n"
        "relations:\n  orders: {sql: 'SELECT 1 AS amount'}\n"
        "metrics:\n  n: {relation: orders, expr: SUM(amount)}\n"
    )
    store = DashboardStore(tmp_path)
    text, _ = export_lookml(SemanticLayer(store), store)
    assert "derived_table:" in text
    assert "SELECT 1 AS amount" in text


def test_export_lookml_qualifies_colliding_view_names(tmp_path):
    (tmp_path / "metrics.yaml").write_text(
        "source: {type: snowflake, account: a, database: D, schema: S, username: u}\n"
        "metrics:\n"
        "  a: {table: D.S1.ORDERS, expr: SUM(x)}\n"
        "  b: {table: D.S2.ORDERS, expr: SUM(y)}\n"
    )
    store = DashboardStore(tmp_path)
    text, warnings = export_lookml(SemanticLayer(store), store)
    names = [
        line.removeprefix("view: ").removesuffix(" {")
        for line in text.splitlines()
        if line.startswith("view:")
    ]
    assert names == ["ORDERS", "D_S2_ORDERS"]
    assert "sql_table_name: D.S1.ORDERS" in text
    assert "sql_table_name: D.S2.ORDERS" in text
    assert any("collides" in w and "D_S2_ORDERS" in w for w in warnings)


def test_export_lookml_slugifies_non_identifier_view_names(tmp_path):
    (tmp_path / "metrics.yaml").write_text(
        "source: {type: snowflake, account: a, database: D, schema: S, username: u}\n"
        "metrics:\n"
        "  rev: {table: 'D.S.\"Order Details\"', expr: SUM(amount)}\n"
        "  y: {table: D.S.2024_sales, expr: SUM(amount)}\n"
    )
    store = DashboardStore(tmp_path)
    text, warnings = export_lookml(SemanticLayer(store), store)
    assert "view: Order_Details" in text
    assert 'view: "Order Details"' not in text
    assert "view: t_2024_sales" in text
    assert 'sql_table_name: D.S."Order Details"' in text
    assert any("Order_Details" in w and "not a LookML identifier" in w for w in warnings)
    assert any("t_2024_sales" in w and "not a LookML identifier" in w for w in warnings)
    lkml = pytest.importorskip("lkml")
    lkml.load(text)


def test_export_lookml_quoted_table_tail_keeps_dots_inside_quotes(tmp_path):
    (tmp_path / "metrics.yaml").write_text(
        "source: {type: snowflake, account: a, database: D, schema: S, username: u}\n"
        "metrics:\n"
        "  z: {table: 'D.S.\"Ord.ers\"', expr: SUM(amount)}\n"
    )
    store = DashboardStore(tmp_path)
    text, warnings = export_lookml(SemanticLayer(store), store)
    assert "view: Ord_ers" in text
    assert "view: ers" not in text
    assert 'sql_table_name: D.S."Ord.ers"' in text
    assert any("Ord_ers" in w for w in warnings)


def test_export_lookml_skips_empty_expr(tmp_path):
    (tmp_path / "metrics.yaml").write_text(
        "source: {type: snowflake, account: a, database: D, schema: S, username: u}\n"
        "metrics:\n"
        '  e: {table: D.S.ORDERS, expr: ""}\n'
        "  revenue: {table: D.S.ORDERS, expr: SUM(amount)}\n"
    )
    store = DashboardStore(tmp_path)
    text, warnings = export_lookml(SemanticLayer(store), store)
    assert "measure: e" not in text
    assert "measure: revenue" in text
    assert "sql:  ;;" not in text
    assert any("e" in w and "empty" in w for w in warnings)


def test_export_lookml_drops_a_dimension_that_collides_with_a_measure(tmp_path):
    (tmp_path / "metrics.yaml").write_text(
        "source: {type: snowflake, account: a, database: D, schema: S, username: u}\n"
        "metrics:\n"
        "  revenue: {table: D.S.ORDERS, expr: SUM(amount), dimensions: [{name: revenue}]}\n"
    )
    store = DashboardStore(tmp_path)
    text, warnings = export_lookml(SemanticLayer(store), store)
    assert "measure: revenue" in text
    assert "dimension: revenue" not in text
    assert any("dimension 'revenue'" in w and "collides" in w for w in warnings)


def test_export_lookml_drops_a_dimension_that_collides_with_a_time_group(tmp_path):
    (tmp_path / "metrics.yaml").write_text(
        "source: {type: snowflake, account: a, database: D, schema: S, username: u}\n"
        "metrics:\n"
        "  a: {table: D.S.ORDERS, expr: SUM(x),"
        " time_dimension: {name: d, expr: dt, grain: day}}\n"
        "  b: {table: D.S.ORDERS, expr: SUM(y), dimensions: [{name: d}]}\n"
    )
    store = DashboardStore(tmp_path)
    text, warnings = export_lookml(SemanticLayer(store), store)
    assert "dimension_group: d" in text
    assert "dimension: d {" not in text
    assert any("dimension 'd'" in w and "collides" in w for w in warnings)


def test_export_lookml_keeps_the_first_relation_name_for_one_table(tmp_path):
    (tmp_path / "metrics.yaml").write_text(
        "source: {type: snowflake, account: a, database: D, schema: S, username: u}\n"
        "relations:\n"
        "  orders: {table: D.S.ORDERS}\n"
        "  orders2: {table: D.S.ORDERS}\n"
        "metrics:\n"
        "  a: {relation: orders, expr: SUM(x)}\n"
        "  b: {relation: orders2, expr: SUM(y)}\n"
    )
    store = DashboardStore(tmp_path)
    text, warnings = export_lookml(SemanticLayer(store), store)
    assert "view: orders" in text
    assert "view: orders2" not in text
    assert any("orders2" in w and "shares a table" in w for w in warnings)


def test_export_lookml_derived_is_a_number_measure(tmp_path):
    (tmp_path / "metrics.yaml").write_text(
        "source: {type: snowflake, account: a, database: D, schema: S, username: u}\n"
        "relations:\n  orders: {table: D.S.ORDERS}\n"
        "metrics:\n"
        "  revenue: {relation: orders, expr: SUM(amount)}\n"
        "  order_count: {relation: orders, expr: COUNT(*)}\n"
        '  aov: {derived: "{revenue} / NULLIF({order_count}, 0)"}\n'
    )
    store = DashboardStore(tmp_path)
    text, warnings = export_lookml(SemanticLayer(store), store)
    assert "measure: aov" in text
    assert "type: number" in text
    assert "SUM(amount)" in text
    assert "COUNT(*)" in text
    aov_warnings = [w for w in warnings if "aov" in w]
    assert len(aov_warnings) == 1
    assert "is derived" in aov_warnings[0]
    assert "drops the measure" in aov_warnings[0]


def test_export_lookml_warns_when_expr_is_not_a_lookml_aggregate(tmp_path):
    (tmp_path / "metrics.yaml").write_text(
        "source: {type: snowflake, account: a, database: D, schema: S, username: u}\n"
        "metrics:\n"
        "  nonnull: {table: D.S.ORDERS, expr: COUNT(amount)}\n"
    )
    store = DashboardStore(tmp_path)
    text, warnings = export_lookml(SemanticLayer(store), store)
    assert "measure: nonnull" in text
    assert "type: number" in text
    assert "COUNT(amount)" in text
    assert any("not a LookML aggregate" in w and "drops the measure" in w for w in warnings)
    assert not any("is derived" in w for w in warnings)


def test_export_lookml_qualifies_colliding_measure_names(tmp_path):
    for repo in ("acme", "other"):
        root = tmp_path / repo
        root.mkdir()
        (root / "metrics.yaml").write_text(
            "source: {type: snowflake, account: a, database: D, schema: S, username: u}\n"
            "metrics:\n"
            "  revenue: {table: D.S.ORDERS, expr: SUM(amount)}\n"
        )
    layer = WorkspaceLayer(
        {
            "acme": SemanticLayer(DashboardStore(tmp_path / "acme")),
            "other": SemanticLayer(DashboardStore(tmp_path / "other")),
        }
    )
    text, warnings = export_lookml(layer, DashboardStore(tmp_path / "acme"))
    names = [
        line.removeprefix("  measure: ").removesuffix(" {")
        for line in text.splitlines()
        if line.startswith("  measure:")
    ]
    assert names == ["revenue", "other_revenue"]
    assert any("collides" in w and "other_revenue" in w for w in warnings)


def test_export_lookml_qualified_measure_names_are_identifiers(tmp_path):
    for repo in ("acme", "2024_analytics"):
        root = tmp_path / repo
        root.mkdir()
        (root / "metrics.yaml").write_text(
            "source: {type: snowflake, account: a, database: D, schema: S, username: u}\n"
            "metrics:\n"
            "  revenue: {table: D.S.ORDERS, expr: SUM(amount)}\n"
        )
    layer = WorkspaceLayer(
        {
            "acme": SemanticLayer(DashboardStore(tmp_path / "acme")),
            "2024_analytics": SemanticLayer(DashboardStore(tmp_path / "2024_analytics")),
        }
    )
    text, warnings = export_lookml(layer, DashboardStore(tmp_path / "acme"))
    names = [
        line.removeprefix("  measure: ").removesuffix(" {")
        for line in text.splitlines()
        if line.startswith("  measure:")
    ]
    assert names == ["revenue", "t_2024_analytics_revenue"]
    assert "measure: 2024_analytics_revenue" not in text
    assert any("t_2024_analytics_revenue" in w for w in warnings)
    lkml = pytest.importorskip("lkml")
    lkml.load(text)


def test_export_lookml_composite_expr_is_not_a_broken_sum(tmp_path):
    (tmp_path / "metrics.yaml").write_text(
        "source: {type: snowflake, account: a, database: D, schema: S, username: u}\n"
        "metrics:\n"
        "  total: {table: D.S.ORDERS, expr: SUM(net) + SUM(tax)}\n"
        "  nested: {table: D.S.ORDERS, expr: SUM((a+b))}\n"
    )
    store = DashboardStore(tmp_path)
    text, warnings = export_lookml(SemanticLayer(store), store)
    assert "sql: net) + SUM(tax" not in text
    assert "sql: SUM(net) + SUM(tax)" in text
    assert "type: number" in text
    assert "type: sum" in text
    assert "(a+b)" in text
    assert any("total" in w and "not a LookML aggregate" in w for w in warnings)
    assert not any("nested" in w and "not a LookML aggregate" in w for w in warnings)


def test_export_lookml_workspace_warnings_stay_on_their_repo(tmp_path):
    (tmp_path / "acme").mkdir()
    (tmp_path / "other").mkdir()
    (tmp_path / "acme" / "metrics.yaml").write_text(
        "source: {type: snowflake, account: a, database: D, schema: S, username: u}\n"
        "metrics:\n"
        "  amount: {table: D.S.ORDERS, expr: SUM(amount)}\n"
        "  order_count: {table: D.S.ORDERS, expr: COUNT(*)}\n"
        '  revenue: {derived: "{amount} / NULLIF({order_count}, 0)"}\n'
    )
    (tmp_path / "other" / "metrics.yaml").write_text(
        "source: {type: snowflake, account: a, database: D, schema: S, username: u}\n"
        "metrics:\n"
        "  revenue: {table: D.S.ORDERS, expr: COUNT(amount)}\n"
    )
    layer = WorkspaceLayer(
        {
            "acme": SemanticLayer(DashboardStore(tmp_path / "acme")),
            "other": SemanticLayer(DashboardStore(tmp_path / "other")),
        }
    )
    _, warnings = export_lookml(layer, DashboardStore(tmp_path / "acme"))
    acme = [w for w in warnings if "acme/revenue" in w]
    other = [w for w in warnings if "other/revenue" in w]
    assert len(acme) == 1
    assert "is derived" in acme[0]
    assert len(other) == 1
    assert "not a LookML aggregate" in other[0]
    assert not any("is derived" in w for w in other)


def test_a_semicolon_inside_a_string_is_not_rewritten(tmp_path):
    """A regex rewrite split `; type:` inside sql and descriptions, so the
    imported expr silently disagreed with the source."""
    view = tmp_path / "orders.view.lkml"
    view.write_text(
        "view: orders {\n"
        "  sql_table_name: public.orders ;;\n"
        "  measure: flagged { type: sum; "
        "sql: ${TABLE}.status = 'active; type: A' ;; }\n"
        "}\n"
    )
    doc, warnings = import_lookml(view)
    assert not any("parse error" in w for w in warnings), warnings
    assert "active; type: A" in doc["metrics"]["flagged"]["expr"], doc["metrics"]["flagged"]


def test_a_semicolon_inside_unquoted_sql_is_not_rewritten(tmp_path):
    """Quotes were not the class. Unquoted `sql: SELECT a; b: c ;;` is
    valid LookML; treating `; b:` as a field separator imported
    `SUM(SELECT a\\n b: c)` with no warning."""
    from sqldash.semantics.lookml import _expand_compact_fields

    src = (
        "view: x {\n  sql_table_name: t ;;\n  measure: m { type: sum; sql: SELECT a; b: c ;; }\n}\n"
    )
    assert "SELECT a; b: c" in _expand_compact_fields(src)
    view = tmp_path / "x.view.lkml"
    view.write_text(src)
    doc, warnings = import_lookml(view)
    assert not any("parse error" in w for w in warnings), warnings
    assert "SELECT a; b: c" in doc["metrics"]["m"]["expr"], doc["metrics"]["m"]


def test_a_hung_lkml_parse_is_an_error_not_a_wedge(tmp_path, monkeypatch):
    """Do not feed lkml the hanging input in CI — the lexer loop is how
    a 1s cap becomes an unbounded job if the kill is slow. The 8s cap
    is still what production uses; this checks we turn TimeoutExpired
    into SemanticError."""
    import subprocess

    from sqldash.semantics import lookml

    def boom(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="lkml", timeout=lookml._LKML_LOAD_TIMEOUT_S)

    monkeypatch.setattr(lookml.subprocess, "run", boom)
    view = tmp_path / "x.lkml"
    view.write_text("view: x { sql_table_name: t ;; measure: m { type: count } }\n")
    with pytest.raises(SemanticError, match="did not complete"):
        import_lookml(view)


TWO_TIME_GROUPS = """
view: orders {
  sql_table_name: analytics.orders ;;
  dimension_group: created { type: time timeframes: [date, week] sql: ${TABLE}.created_at ;; }
  dimension_group: shipped { type: time timeframes: [date, week] sql: ${TABLE}.shipped_at ;; }
  measure: revenue { type: sum sql: ${TABLE}.amount ;; }
}
"""


def test_a_second_time_group_is_reported_not_silently_dropped(tmp_path):
    """The loop broke after the first `type: time` group before `_resolve_sql`
    ran, so the skip machinery never saw the second one and every measure
    carried a time_dimension the author never scoped it by. #331. Same
    wording as `import cortex` on the same shape."""
    view = tmp_path / "v.lkml"
    view.write_text(TWO_TIME_GROUPS)
    out, warnings = import_lookml(view)
    assert out["metrics"]["revenue"]["time_dimension"] == {
        "name": "created",
        "grain": "day",
        "expr": "created_at",
    }
    assert warnings == [
        "view 'orders': only the first time dimension ('created') was kept per metric; "
        "dropped: shipped"
    ]


def test_a_date_typed_dimension_group_is_a_day_grain_time_dimension(tmp_path):
    """`type: date` was skipped by the `!= "time"` test with no warning, so the
    view imported with no time dimension at all. A date-only column has no
    time component, so day grain is exact for it."""
    view = tmp_path / "v.lkml"
    view.write_text(
        "view: orders {\n  sql_table_name: analytics.orders ;;\n"
        "  dimension_group: ordered { type: date sql: ${TABLE}.order_date ;; }\n"
        "  measure: revenue { type: sum sql: ${TABLE}.amount ;; }\n}\n"
    )
    out, warnings = import_lookml(view)
    assert out["metrics"]["revenue"]["time_dimension"] == {
        "name": "ordered",
        "grain": "day",
        "expr": "order_date",
    }
    assert warnings == []


def test_a_duration_group_is_not_a_time_dimension(tmp_path):
    """`type: duration` groups measure intervals between two columns; they are
    neither a time dimension nor something the drop warning should name."""
    view = tmp_path / "v.lkml"
    view.write_text(
        "view: orders {\n  sql_table_name: analytics.orders ;;\n"
        "  dimension_group: created { type: time sql: ${TABLE}.created_at ;; }\n"
        "  dimension_group: to_ship { type: duration intervals: [day]"
        " sql_start: ${TABLE}.created_at ;; sql_end: ${TABLE}.shipped_at ;; }\n"
        "  measure: revenue { type: sum sql: ${TABLE}.amount ;; }\n}\n"
    )
    out, warnings = import_lookml(view)
    assert out["metrics"]["revenue"]["time_dimension"]["name"] == "created"
    assert warnings == []


RESERVED_NAMES = """
view: order {
  sql_table_name: DB.S.ORDERS ;;
  dimension: user { sql: ${TABLE}.user_id ;; }
  dimension: order { }
  dimension_group: when { type: time sql: ${TABLE}.created_at ;; }
  measure: select { type: sum sql: ${TABLE}.amount ;; }
}
"""


def test_import_lookml_renames_fields_sqldash_would_refuse(tmp_path):
    """LookML happily names a measure `select`; #291 made sqldash's models
    reject a reserved word, so the import wrote a file its own linter rejected
    with no diagnostic at the write (#337)."""
    view = tmp_path / "v.lkml"
    view.write_text(RESERVED_NAMES)
    out, warnings = import_lookml(view)
    metric = out["metrics"]["select_metric"]
    assert metric["time_dimension"]["name"] == "when_field"
    assert [d["name"] for d in metric["dimensions"]] == ["user_field", "order_field"]
    for original, renamed in (
        ("user", "user_field"),
        ("order", "order_field"),
        ("when", "when_field"),
        ("select", "select_metric"),
    ):
        assert any(f"'{original}'" in w and f"'{renamed}'" in w for w in warnings), warnings


def test_a_reserved_view_name_is_left_alone_but_a_non_identifier_one_is_not(tmp_path):
    """Relation keys are the one name the model allows a reserved word — nothing
    emits them into SQL — so `view: order` keeps its name."""
    view = tmp_path / "v.lkml"
    view.write_text(RESERVED_NAMES)
    out, _ = import_lookml(view)
    assert "order" in out["relations"]
    assert out["metrics"]["select_metric"]["relation"] == "order"


def test_a_renamed_lookml_dimension_keeps_pointing_at_its_own_column(tmp_path):
    view = tmp_path / "v.lkml"
    view.write_text(RESERVED_NAMES)
    out, _ = import_lookml(view)
    dims = {d["name"]: d.get("expr") for d in out["metrics"]["select_metric"]["dimensions"]}
    assert dims["user_field"] == "user_id"
    assert dims["order_field"] == '"order"'


def test_a_renamed_reserved_dimension_can_actually_be_queried(tmp_path):
    """The rename wrote `expr: order` bare, which lint accepts and no engine
    parses: `SELECT order AS "order_field"` is a syntax error, so every query of
    the metric died while the import looked clean. #337 review."""
    import duckdb

    from sqldash.execution import ExecutionRegistry
    from sqldash.models.source import Source
    from sqldash.semantics.naming import source_column

    assert source_column("order") == '"order"'
    db = tmp_path / "w.duckdb"
    conn = duckdb.connect(str(db))
    conn.execute('CREATE TABLE orders("order" VARCHAR, amount INT)')
    conn.execute("INSERT INTO orders VALUES ('a', 10), ('b', 32)")
    conn.close()
    sql = (
        f'SELECT {source_column("order")} AS "order_field", SUM(amount) AS m '
        "FROM orders GROUP BY 1 ORDER BY 1"
    )
    registry = ExecutionRegistry(max_workers=1)
    try:
        outcome = registry.run_sync(
            Source(type="duckdb", database="w.duckdb"), tmp_path, sql, [], 10, timeout=10
        )
    finally:
        registry.shutdown()
    assert outcome.rows == [["a", 10], ["b", 32]], outcome.rows


def test_import_lookml_output_validates_as_a_metrics_file(tmp_path):
    view = tmp_path / "v.lkml"
    view.write_text(RESERVED_NAMES)
    out, _ = import_lookml(view)
    out["source"] = {
        "type": "snowflake",
        "account": "a",
        "database": "DB",
        "schema": "S",
        "username": "u",
    }
    MetricsFile.model_validate(out)


def test_two_dimensions_that_slugify_alike_both_survive(tmp_path):
    """`order` is reserved and becomes `order_field`, colliding with a real
    `order_field` in the same view. Undeduped, the pair failed the model's
    "dimension names must be unique within a metric" check and the importer
    wrote nothing at all — the one outcome this importer promises never to
    produce. #337 review."""
    view = tmp_path / "sales.view.lkml"
    view.write_text(
        "view: sales {\n"
        "  sql_table_name: analytics.sales ;;\n"
        "  dimension: order { type: string sql: ${TABLE}.order_col ;; }\n"
        "  dimension: order_field { type: string sql: ${TABLE}.other_col ;; }\n"
        "  measure: revenue { type: sum sql: ${TABLE}.amount ;; }\n"
        "}\n"
    )
    out, warnings = import_lookml(tmp_path)
    dims = out["metrics"]["revenue"]["dimensions"]
    assert [d["name"] for d in dims] == ["order_field", "order_field_2"]
    assert [d["expr"] for d in dims] == ["order_col", "other_col"]
    assert any("'order_field' is already taken" in w for w in warnings), warnings
    MetricsFile.model_validate(out)


_DUCK = "source: {type: duckdb, attach_files: true}\nrelations:\n  orders: {table: orders}\n"


def _export(tmp_path, metrics_yaml: str) -> tuple[str, list[str]]:
    (tmp_path / "metrics.yaml").write_text(_DUCK + metrics_yaml)
    store = DashboardStore(tmp_path)
    return export_lookml(SemanticLayer(store), store)


def _round_trip(tmp_path, metrics_yaml: str) -> tuple[dict, list[str]]:
    pytest.importorskip("lkml")
    text, _ = _export(tmp_path, metrics_yaml)
    lkml_file = tmp_path / "out.view.lkml"
    lkml_file.write_text(text)
    return import_lookml(lkml_file)


def test_export_lookml_carries_title_format_and_dimension_description(tmp_path):
    """#578: LookML has label: and value_format_name:, and a dimension can hold a
    description, so none of these needs to be lost or warned about."""
    text, warnings = _export(
        tmp_path,
        "metrics:\n"
        "  revenue:\n"
        "    title: Revenue\n"
        "    relation: orders\n"
        "    expr: SUM(amount)\n"
        "    format: percent\n"
        "    dimensions: [{name: region, description: Sales region}]\n",
    )
    assert 'label: "Revenue"' in text
    assert "value_format_name: percent_1" in text
    assert 'description: "Sales region"' in text
    assert warnings == []


def test_export_lookml_warns_for_metadata_it_cannot_carry(tmp_path):
    """Everything LookML has no field for is named in a warning rather than
    disappearing, the same contract as every other loss in this converter."""
    text, warnings = _export(
        tmp_path,
        "metrics:\n"
        "  revenue:\n"
        "    relation: orders\n"
        "    expr: SUM(amount)\n"
        "    format: currency\n"
        "    synonyms: [sales]\n"
        "    owners: [finance]\n"
        "    dimensions: [{name: region, synonyms: [area]}]\n"
        "  signups: {relation: orders, expr: COUNT(*), format: compact}\n"
        "  orders_n: {relation: orders, expr: COUNT(*), format: number}\n",
    )
    joined = "\n".join(warnings)
    assert "metric 'revenue' has owners, which LookML has no field parameter for" in joined
    assert "metric 'revenue' has format: currency" in joined
    assert "exported as usd" in joined
    assert "synonyms" not in joined, "#679: LookML has a field-level synonyms:"
    assert "metric 'signups' has format: compact" in joined
    assert "orders_n" not in joined, "number is Looker's default and loses nothing"
    assert "value_format_name: usd" in text
    assert text.count("value_format_name") == 1


def test_export_lookml_carries_synonyms(tmp_path):
    """#679: `synonyms:` is a real LookML field parameter on measures and
    dimensions, so neither the metric's nor the dimension's need dropping."""
    text, warnings = _export(
        tmp_path,
        "metrics:\n"
        "  revenue:\n"
        "    relation: orders\n"
        "    expr: SUM(amount)\n"
        "    synonyms: [sales, 'turnover \"net\"']\n"
        "    dimensions: [{name: region, synonyms: [territory, market]}]\n",
    )
    assert 'synonyms: ["territory", "market"]' in text
    assert 'synonyms: ["sales", "turnover \\"net\\""]' in text
    assert warnings == []


def test_export_lookml_warns_when_one_dimension_has_two_descriptions(tmp_path):
    _, warnings = _export(
        tmp_path,
        "metrics:\n"
        "  revenue: {relation: orders, expr: SUM(amount),"
        " dimensions: [{name: region, description: Sales region}]}\n"
        "  margin: {relation: orders, expr: SUM(margin),"
        " dimensions: [{name: region, description: Billing region}]}\n",
    )
    assert any("different descriptions across metrics" in w for w in warnings), warnings


def test_lookml_round_trip_keeps_title_format_and_dimension_description(tmp_path):
    """The issue's clearest case: import already reads a dimension description, so
    losing it on export made sqldash's own round trip lossy."""
    doc, warnings = _round_trip(
        tmp_path,
        "metrics:\n"
        "  revenue:\n"
        "    title: Revenue\n"
        "    description: Total order revenue\n"
        "    relation: orders\n"
        "    expr: SUM(amount)\n"
        "    format: EUR\n"
        "    dimensions: [{name: region, description: Sales region}]\n",
    )
    revenue = doc["metrics"]["revenue"]
    assert revenue["title"] == "Revenue"
    assert revenue["description"] == "Total order revenue"
    assert revenue["format"] == "EUR"
    assert revenue["dimensions"] == [{"name": "region", "description": "Sales region"}]
    assert warnings == []


def test_lookml_round_trip_keeps_synonyms(tmp_path):
    """#679: the round trip is sqldash's own, so a synonym that leaves on a
    measure or a dimension has to come back on the same field."""
    doc, warnings = _round_trip(
        tmp_path,
        "metrics:\n"
        "  revenue:\n"
        "    relation: orders\n"
        "    expr: SUM(amount)\n"
        "    synonyms: [sales, turnover]\n"
        "    dimensions:\n"
        "      - {name: region, description: Sales region, synonyms: [territory, market]}\n"
        "      - {name: category}\n",
    )
    revenue = doc["metrics"]["revenue"]
    assert revenue["synonyms"] == ["sales", "turnover"]
    assert revenue["dimensions"] == [
        {"name": "region", "description": "Sales region", "synonyms": ["territory", "market"]},
        {"name": "category"},
    ]
    assert warnings == []
    MetricsFile.model_validate({**doc, "source": {"type": "duckdb"}})


def test_lookml_round_trip_gives_each_metric_its_own_dimension_synonyms(tmp_path):
    """Two metrics over one view share a dimension list; the dumped YAML has to
    write it out per metric rather than alias one metric's list into the next."""
    text, _ = _export(
        tmp_path,
        "metrics:\n"
        "  revenue: {relation: orders, expr: SUM(amount),"
        " dimensions: [{name: region, synonyms: [territory]}]}\n"
        "  margin: {relation: orders, expr: SUM(margin),"
        " dimensions: [{name: region, synonyms: [territory]}]}\n",
    )
    pytest.importorskip("lkml")
    lkml_file = tmp_path / "out.view.lkml"
    lkml_file.write_text(text)
    doc, _ = import_lookml(lkml_file)
    rendered = render_yaml(doc)
    assert "*id" not in rendered, rendered
    assert "&id" not in rendered, rendered
    assert rendered.count("- territory") == 2, rendered


def test_lookml_round_trip_keeps_quotes_and_backslashes(tmp_path):
    """lkml returns strings still escaped, so a quote used to come back as \\"."""
    text = 'Revenue in "USD", see C:\\\\reports'
    doc, _ = _round_trip(
        tmp_path,
        "metrics:\n"
        "  revenue:\n"
        f"    description: '{text}'\n"
        "    relation: orders\n"
        "    expr: SUM(amount)\n",
    )
    assert doc["metrics"]["revenue"]["description"] == text


def test_import_lookml_reads_label_and_value_format_name(tmp_path):
    pytest.importorskip("lkml")
    view = tmp_path / "orders.view.lkml"
    view.write_text(
        "view: orders {\n"
        "  sql_table_name: orders ;;\n"
        '  measure: rate { type: average sql: ${TABLE}.rate ;; label: "Win rate"'
        " value_format_name: percent_2 }\n"
        "  measure: order_id { type: max sql: ${TABLE}.id ;; value_format_name: id }\n"
        "}\n"
    )
    doc, warnings = import_lookml(view)
    assert doc["metrics"]["rate"]["title"] == "Win rate"
    assert doc["metrics"]["rate"]["format"] == "percent"
    assert "format" not in doc["metrics"]["order_id"]
    assert any("value_format_name id has no sqldash format" in w for w in warnings), warnings
    MetricsFile.model_validate({**doc, "source": {"type": "duckdb"}})


def test_import_lookml_reads_synonyms(tmp_path):
    """#679's import repro: a real Looker model's synonyms used to vanish with no
    warning. `synonyms:` takes a bare string too, which is one synonym."""
    pytest.importorskip("lkml")
    view = tmp_path / "orders.view.lkml"
    view.write_text(
        "view: orders {\n"
        "  sql_table_name: orders ;;\n"
        '  dimension: region { sql: ${TABLE}.region ;; synonyms: ["territory", "market"] }\n'
        '  dimension: category { sql: ${TABLE}.category ;; synonyms: "kind" }\n'
        "  measure: revenue { type: sum sql: ${TABLE}.amount ;; "
        'label: "Revenue" synonyms: ["sales", "turnover"] }\n'
        "}\n"
    )
    doc, warnings = import_lookml(view)
    revenue = doc["metrics"]["revenue"]
    assert revenue["synonyms"] == ["sales", "turnover"]
    assert revenue["dimensions"] == [
        {"name": "region", "synonyms": ["territory", "market"]},
        {"name": "category", "synonyms": ["kind"]},
    ]
    assert warnings == []
    MetricsFile.model_validate(doc)


def test_import_lookml_warns_for_a_dimension_groups_synonyms(tmp_path):
    """A sqldash time dimension has no synonyms field, so that one is a real
    loss — and the converter's contract is that a loss is announced."""
    pytest.importorskip("lkml")
    view = tmp_path / "orders.view.lkml"
    view.write_text(
        "view: orders {\n"
        "  sql_table_name: orders ;;\n"
        "  dimension_group: ordered { type: time timeframes: [raw, date] "
        'sql: ${TABLE}.ordered_at ;; synonyms: ["placed", "booked"] }\n'
        "  measure: revenue { type: sum sql: ${TABLE}.amount ;; }\n"
        "}\n"
    )
    doc, warnings = import_lookml(view)
    assert doc["metrics"]["revenue"]["time_dimension"]["name"] == "ordered"
    assert any(
        "dimension_group 'ordered' has synonyms" in w and "they were dropped" in w for w in warnings
    ), warnings


def test_export_lookml_writes_one_synonyms_line_for_a_shared_dimension(tmp_path):
    """Every metric on a view repeats its dimensions; the view holds one field."""
    text, warnings = _export(
        tmp_path,
        "metrics:\n"
        "  revenue: {relation: orders, expr: SUM(amount),"
        " dimensions: [{name: region, synonyms: [area]}]}\n"
        "  margin: {relation: orders, expr: SUM(margin),"
        " dimensions: [{name: region, synonyms: [area]}]}\n",
    )
    assert text.count('synonyms: ["area"]') == 1
    assert warnings == []


def test_export_lookml_warns_when_one_dimension_has_two_synonym_lists(tmp_path):
    """The same rule descriptions get: one LookML field cannot hold both."""
    text, warnings = _export(
        tmp_path,
        "metrics:\n"
        "  revenue: {relation: orders, expr: SUM(amount),"
        " dimensions: [{name: region, synonyms: [area]}]}\n"
        "  margin: {relation: orders, expr: SUM(margin),"
        " dimensions: [{name: region, synonyms: [territory]}]}\n",
    )
    assert any("different synonyms across metrics" in w for w in warnings), warnings
    assert 'synonyms: ["area"]' in text
    assert "territory" not in text


BASE_VIEW = """
view: base_orders {
  extension: required
  sql_table_name: analytics.orders ;;
  dimension: status { sql: ${TABLE}.status ;; }
  dimension: amount { sql: ${TABLE}.amount ;; description: "Gross amount" }
  measure: revenue { type: sum sql: ${amount} ;; label: "Revenue" }
  measure: order_count { type: count }
}
"""


def test_an_extending_view_imports_what_it_inherits(tmp_path):
    (tmp_path / "base.view.lkml").write_text(BASE_VIEW)
    (tmp_path / "orders.view.lkml").write_text(
        "view: orders {\n"
        "  extends: [base_orders]\n"
        "  dimension: category { sql: ${TABLE}.category ;; }\n"
        "  measure: avg_amount { type: average sql: ${amount} ;; }\n"
        "}\n"
    )
    doc, warnings = import_lookml(tmp_path)
    assert doc["relations"] == {"orders": {"table": "analytics.orders"}}
    assert set(doc["metrics"]) == {"revenue", "order_count", "avg_amount"}
    assert doc["metrics"]["revenue"]["expr"] == "SUM(amount)"
    assert doc["metrics"]["revenue"]["title"] == "Revenue"
    assert [d["name"] for d in doc["metrics"]["avg_amount"]["dimensions"]] == [
        "status",
        "amount",
        "category",
    ]
    assert any("'base_orders': extension: required" in w for w in warnings), warnings
    MetricsFile.model_validate({**doc, "source": {"type": "duckdb"}})


def test_an_extending_view_overrides_what_it_restates(tmp_path):
    """The child wins per parameter: a redefined field keeps what it does not
    restate, and a new table replaces the inherited one."""
    view = tmp_path / "v.view.lkml"
    view.write_text(
        BASE_VIEW + "view: eu_orders {\n"
        "  extends: [base_orders]\n"
        "  derived_table: { sql: SELECT * FROM analytics.orders_eu ;; }\n"
        "  dimension: amount { sql: ${TABLE}.amount_eur ;; }\n"
        "  measure: order_count { type: count_distinct sql: ${TABLE}.order_id ;; }\n"
        "}\n"
    )
    doc, _ = import_lookml(view)
    assert doc["relations"] == {"eu_orders": {"sql": "SELECT * FROM analytics.orders_eu"}}
    assert doc["metrics"]["revenue"]["expr"] == "SUM(amount_eur)"
    assert doc["metrics"]["order_count"]["expr"] == "COUNT(DISTINCT order_id)"
    amount = next(d for d in doc["metrics"]["revenue"]["dimensions"] if d["name"] == "amount")
    assert amount == {"name": "amount", "expr": "amount_eur", "description": "Gross amount"}


def test_multiple_extends_apply_left_to_right(tmp_path):
    view = tmp_path / "v.view.lkml"
    view.write_text(
        "view: a {\n  extension: required\n  sql_table_name: t_a ;;\n"
        "  measure: m { type: sum sql: ${TABLE}.from_a ;; }\n}\n"
        "view: b {\n  extension: required\n  sql_table_name: t_b ;;\n"
        "  measure: m { type: sum sql: ${TABLE}.from_b ;; }\n"
        "  measure: only_b { type: count }\n}\n"
        "view: c {\n  extends: [a, b]\n}\n"
    )
    doc, _ = import_lookml(view)
    assert doc["relations"] == {"c": {"table": "t_b"}}
    assert doc["metrics"]["m"]["expr"] == "SUM(from_b)"
    assert "only_b" in doc["metrics"]


def test_a_base_that_is_not_required_imports_on_its_own_too(tmp_path):
    view = tmp_path / "v.view.lkml"
    view.write_text(
        "view: orders {\n  sql_table_name: t ;;\n  measure: n { type: count }\n}\n"
        "view: big_orders {\n  extends: [orders]\n"
        "  measure: m { type: max sql: ${TABLE}.x ;; }\n}\n"
    )
    doc, warnings = import_lookml(view)
    assert doc["relations"] == {"orders": {"table": "t"}, "big_orders": {"table": "t"}}
    assert doc["metrics"]["n"]["relation"] == "orders"
    assert doc["metrics"]["big_orders_n"]["relation"] == "big_orders"
    assert not warnings, warnings


def test_an_unresolvable_extends_is_named_not_a_traceback(tmp_path):
    view = tmp_path / "v.view.lkml"
    view.write_text(
        "view: orphan {\n  extends: [missing]\n  measure: n { type: count }\n}\n"
        "view: x {\n  extends: [y]\n  sql_table_name: t ;;\n  measure: n { type: count }\n}\n"
        "view: y {\n  extends: [x]\n  measure: n { type: count }\n}\n"
    )
    with pytest.raises(SemanticError) as excinfo:
        import_lookml(view)
    message = str(excinfo.value)
    assert "skipped view 'orphan': extends 'missing', which is not in the imported" in message
    assert "cycle" in message, message


def test_extends_written_outside_the_braces_is_named_not_a_traceback(tmp_path):
    """`view: a extends: b { ... }` parses as a bare view name plus a top-level
    `extends` block; it used to die on `'str' object has no attribute 'get'`."""
    view = tmp_path / "min.view.lkml"
    view.write_text(
        "view: orders_min extends: base_orders {\n"
        "  measure: revenue { type: sum sql: ${TABLE}.amount ;; }\n}\n"
    )
    with pytest.raises(SemanticError) as excinfo:
        import_lookml(view)
    message = str(excinfo.value)
    assert "skipped view 'orders_min' in min.view.lkml" in message, message
    assert "extends: [base_view]" in message, message


@pytest.mark.parametrize(
    ("body", "warning"),
    [
        pytest.param(
            "dimension: bad extends: other { sql: ${TABLE}.x ;; }",
            "view 'v': skipped a dimension lkml read only as 'bad'",
            id="extends-outside-the-field-braces",
        ),
        pytest.param(
            "dimension: region_copy { extends: [status] }",
            "view 'v': skipped dimension 'region_copy': field-level extends",
            id="field-level-extends",
        ),
    ],
)
def test_a_field_the_importer_cannot_read_is_named_not_dropped(tmp_path, body, warning):
    """A loss is avoided or announced. A field written with `extends:` outside
    its braces parses to a bare string, and a field-level `extends:` would
    import with none of the inherited sql; both used to vanish or come back as
    a different field without a word."""
    view = tmp_path / "v.view.lkml"
    view.write_text(
        "view: v {\n  sql_table_name: public.t ;;\n"
        "  dimension: status { sql: ${TABLE}.status ;; }\n"
        f"  {body}\n  measure: n {{ type: count }}\n}}\n"
    )
    data, warnings = import_lookml(tmp_path)
    assert any(w.startswith(warning) for w in warnings), warnings
    dims = [d["name"] for d in data["metrics"]["n"].get("dimensions", [])]
    assert dims == ["status"], dims


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("dimensions: foo extends: bar { sql: ${TABLE}.x ;; }", id="dimensions"),
        pytest.param("dimension_groups: g extends: y { type: time }", id="dimension-groups"),
    ],
)
def test_a_field_list_lkml_reads_as_a_string_is_named(tmp_path, body):
    """`dimensions: foo extends: bar { ... }` parses to the bare string 'foo'
    under the plural key, and the importer walked its characters: a raw
    AttributeError, the same traceback #602 was about."""
    view = tmp_path / "v.view.lkml"
    view.write_text(
        f"view: v {{\n  sql_table_name: public.t ;;\n  {body}\n  measure: n {{ type: count }}\n}}\n"
    )
    data, warnings = import_lookml(tmp_path)
    key = body.split(":")[0]
    assert any(w.startswith(f"view 'v': skipped {key}") for w in warnings), warnings
    assert "n" in data["metrics"]


def test_a_derived_table_lkml_reads_as_a_string_is_named(tmp_path):
    """`derived_table: foo extends: bar { sql: ... ;; }` parses to the string 'foo',
    and the importer called `.get("sql")` on it: the #602 traceback again, one key over."""
    (tmp_path / "v.view.lkml").write_text(
        "view: v {\n  derived_table: foo extends: bar { sql: SELECT 1 ;; }\n"
        "  measure: n { type: count }\n}\n"
        "view: w {\n  sql_table_name: public.w ;;\n  measure: m { type: count }\n}\n"
    )
    data, warnings = import_lookml(tmp_path)
    assert any(w.startswith("skipped view 'v'") for w in warnings), warnings
    assert "m" in data["metrics"]


def test_a_duplicate_view_that_extends_is_named_not_half_imported(tmp_path):
    """Extends resolves by view name, and a second view with the same name was
    appended unmerged: its measure vanished under "no sql_table_name". Looker
    rejects duplicate names; the importer now says that is why it skipped it."""
    (tmp_path / "a.view.lkml").write_text(
        "view: base {\n  sql_table_name: public.t ;;\n  measure: n { type: count }\n}\n"
        "view: dup {\n  sql_table_name: public.d ;;\n  measure: d1 { type: count }\n}\n"
    )
    (tmp_path / "b.view.lkml").write_text(
        "view: dup {\n  extends: [base]\n  measure: d2 { type: count }\n}\n"
    )
    _, warnings = import_lookml(tmp_path)
    assert any(w.startswith("skipped view 'dup'") and "same name" in w for w in warnings), warnings
    assert not any("no sql_table_name or derived_table" in w for w in warnings), warnings


def test_an_extends_cycle_is_named_once_with_its_path(tmp_path):
    """Each level of a cycle wrapped the one below it, so a three-view cycle
    printed three nested 'could not be resolved' clauses."""
    (tmp_path / "v.view.lkml").write_text(
        "view: a {\n  extends: [b]\n  sql_table_name: t ;;\n  measure: n { type: count }\n}\n"
        "view: b {\n  extends: [c]\n}\n"
        "view: c {\n  extends: [a]\n}\n"
        "view: w {\n  sql_table_name: w ;;\n  measure: m { type: count }\n}\n"
    )
    _, warnings = import_lookml(tmp_path)
    cycle = [w for w in warnings if w.startswith("skipped view 'a'")]
    assert cycle == ["skipped view 'a': extends 'a' in a cycle (a -> b -> c -> a)"], warnings
    assert not any("could not be resolved" in w for w in warnings), warnings


FILTERED = """
view: orders {
  sql_table_name: orders ;;
  dimension: status { sql: ${TABLE}.status ;; }
  dimension: amount { type: number sql: ${TABLE}.amount ;; }
  dimension: is_big { type: yesno sql: ${TABLE}.amount >= 150 ;; }
  measure: paid_revenue { type: sum sql: ${TABLE}.amount ;; filters: [status: "paid"] }
  measure: not_refunded { type: sum sql: ${amount} ;; filters: [orders.status: "-refunded"] }
  measure: open_or_paid { type: sum sql: ${amount} ;; filters: [status: "paid, open"] }
  measure: big_paid { type: count filters: [status: "paid", is_big: "yes"] }
  measure: over_100 { type: sum sql: ${amount} ;; filters: [amount: ">100"] }
  measure: revenue { type: sum sql: ${amount} ;; }
}
"""


def test_a_measures_filters_are_carried_onto_the_metric(tmp_path):
    """`filters:` is part of what a LookML measure counts. Dropping it imported
    "paid revenue" as total revenue, lint clean and with no warning (#601)."""
    view = tmp_path / "v.view.lkml"
    view.write_text(FILTERED)
    doc, warnings = import_lookml(view)
    metrics = doc["metrics"]
    assert metrics["paid_revenue"]["filters"] == ["status = 'paid'"]
    assert "filters" not in metrics["revenue"]
    assert not warnings, warnings
    MetricsFile.model_validate({**doc, "source": {"type": "duckdb"}})

    conn = duckdb.connect()
    conn.execute(
        "CREATE TABLE orders AS SELECT * FROM (VALUES ('paid', 100), ('paid', 200), "
        "('paid', 150), ('refunded', 50), ('open', 30), (NULL, 10)) t(status, amount)"
    )

    def value(name):
        metric = metrics[name]
        where = " AND ".join(f"({f})" for f in metric.get("filters", [])) or "TRUE"
        return conn.execute(f"SELECT {metric['expr']} FROM orders WHERE {where}").fetchone()[0]

    assert value("paid_revenue") == 450
    assert value("not_refunded") == 490
    assert value("open_or_paid") == 480
    assert value("big_paid") == 2
    assert value("over_100") == 350
    assert value("revenue") == 540


@pytest.mark.parametrize(
    ("filters", "reason"),
    [
        ('[status: "paid^,x"]', "has no sqldash equivalent"),
        ('[status: "EMPTY"]', "has no sqldash equivalent"),
        ('[amount: "[1, 10]"]', "has no sqldash equivalent"),
        ('[created_date: "7 days"]', "names no dimension this view defines"),
        ('[users.status: "paid"]', "is on another view"),
        ('[is_big: "maybe"]', "has no sqldash equivalent"),
    ],
)
def test_a_filter_that_cannot_be_carried_skips_the_measure_by_name(tmp_path, filters, reason):
    view = tmp_path / "v.view.lkml"
    view.write_text(
        FILTERED.replace(
            "measure: revenue {",
            f"measure: odd {{ type: sum sql: ${{amount}} ;; filters: {filters} }}\n"
            "  measure: revenue {",
        )
    )
    doc, warnings = import_lookml(view)
    assert "odd" not in doc["metrics"]
    assert any(
        w.startswith("skipped measure 'orders.odd': filter") and reason in w for w in warnings
    ), warnings


def test_a_tier_dimension_is_skipped_rather_than_imported_as_the_raw_column(tmp_path):
    """`type: tier` buckets values; importing just its sql grouped by every
    distinct amount instead of by the tiers, with no warning."""
    view = tmp_path / "v.view.lkml"
    view.write_text(
        "view: orders {\n  sql_table_name: orders ;;\n"
        "  dimension: amount_tier { type: tier tiers: [0, 100, 500] style: integer\n"
        "    sql: ${TABLE}.amount ;; }\n"
        "  dimension: status { sql: ${TABLE}.status ;; }\n"
        "  measure: revenue { type: sum sql: ${TABLE}.amount ;; }\n}\n"
    )
    doc, warnings = import_lookml(view)
    assert doc["metrics"]["revenue"]["dimensions"] == [{"name": "status"}]
    assert any("skipped dimension 'orders.amount_tier': type 'tier'" in w for w in warnings)


_TIMEFRAME_VIEW = """
view: orders {
  sql_table_name: orders ;;
  dimension: region { sql: ${TABLE}.region ;; }
  dimension_group: ordered {
    type: time
    timeframes: [raw, date, week, month, day_of_week]
    sql: ${TABLE}.ordered_at ;;
  }
  measure: active_weeks { type: count_distinct sql: ${ordered_week} ;; }
}
"""


def _imported_layer(tmp_path, kind: str, database: str):
    """The same imported view, pointed at a real database of that dialect."""
    view = tmp_path / "orders.view.lkml"
    view.write_text(_TIMEFRAME_VIEW)
    doc, _ = import_lookml(view)
    doc["source"] = {"type": kind, "database": str(tmp_path / database)}
    doc["relations"]["orders"] = {"table": "orders"}
    root = tmp_path / kind
    root.mkdir()
    (root / "metrics.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    return SemanticLayer(DashboardStore(root))


def test_an_imported_timeframe_measure_runs_on_sqlite(tmp_path):
    """#628: the importer baked `DATE_TRUNC('week', ...)` into the measure, so an
    imported metric that resolves a timeframe was unrunnable anywhere without a
    postgres-style DATE_TRUNC — SQLite answered `no such function: DATE_TRUNC` at
    query time, long after the import reported no warnings."""
    rows = [
        ((date(2026, 1, 1) + timedelta(days=i)).isoformat(), "eu" if i % 2 else "us")
        for i in range(60)
    ]
    con = sqlite3.connect(tmp_path / "app.db")
    con.execute("CREATE TABLE orders (ordered_at TEXT, region TEXT)")
    con.executemany("INSERT INTO orders VALUES (?, ?)", rows)
    con.commit()
    con.close()
    duck = duckdb.connect(str(tmp_path / "app.duckdb"))
    duck.execute("CREATE TABLE orders (ordered_at TIMESTAMP, region TEXT)")
    duck.executemany("INSERT INTO orders VALUES (?, ?)", rows)
    duck.close()

    registry = ExecutionRegistry(max_workers=1)
    try:
        answers = {}
        for kind, database in (("sqlite", "app.db"), ("duckdb", "app.duckdb")):
            layer = _imported_layer(tmp_path, kind, database)
            resolved = layer.resolve("active_weeks")
            sql, bind = compile_metric(resolved, MetricQuery(dimensions=("region",)), "qmark")
            assert "SQLDASH_TRUNC" not in sql, sql
            result = registry.run_sync(resolved.source, resolved.base_dir, sql, bind, 100)
            answers[kind] = sorted((r[0], r[1]) for r in result.rows)
    finally:
        registry.shutdown()
    assert answers["sqlite"] == [("eu", 9), ("us", 9)], answers
    assert answers["sqlite"] == answers["duckdb"]


def test_export_renders_the_trunc_macro_as_date_trunc(tmp_path):
    """A LookML `sql:` block is warehouse SQL, so the neutral macro has to become
    a real function on the way out — exporting `SQLDASH_TRUNC` would hand Looker
    a function no warehouse defines."""
    pytest.importorskip("lkml")
    view = tmp_path / "orders.view.lkml"
    view.write_text(_TIMEFRAME_VIEW)
    doc, _ = import_lookml(view)
    doc["source"] = {"type": "duckdb", "attach_files": True}
    doc["relations"]["orders"] = {"table": "orders"}
    (tmp_path / "metrics.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    store = DashboardStore(tmp_path)
    text, _ = export_lookml(SemanticLayer(store), store)
    assert "sql: DATE_TRUNC('week', ordered_at) ;;" in text, text
    assert "SQLDASH_TRUNC" not in text, text


def test_export_renders_the_trunc_macro_in_a_relation_sql(tmp_path):
    """A relation's `sql:` becomes the view's derived_table, and it is a macro site
    the compiler expands, so leaving it raw exported a view Looker cannot run."""
    pytest.importorskip("lkml")
    (tmp_path / "metrics.yaml").write_text(
        "source: {type: duckdb, attach_files: true}\n"
        "relations:\n"
        "  weekly:\n"
        "    sql: \"SELECT SQLDASH_TRUNC('week', ordered_at) AS wk, amount FROM orders\"\n"
        "metrics:\n"
        "  weekly_revenue:\n"
        "    relation: weekly\n"
        "    expr: SUM(amount)\n"
    )
    store = DashboardStore(tmp_path)
    text, _ = export_lookml(SemanticLayer(store), store)
    assert "sql: SELECT DATE_TRUNC('week', ordered_at) AS wk, amount FROM orders ;;" in text, text
    assert "SQLDASH_TRUNC" not in text, text


def test_export_spells_the_macro_for_the_project_dialect(tmp_path):
    """Looker runs against whatever warehouse the connection points at, and the
    only evidence of which one that is sits in the metric's own source. Exporting
    one hardcoded dialect gave a BigQuery project a `DATE_TRUNC` that is
    argument-reversed there."""
    (tmp_path / "metrics.yaml").write_text(
        "source: {type: bigquery, project: p, database: d}\n"
        "metrics:\n"
        "  active_weeks:\n"
        "    table: orders\n"
        "    expr: \"COUNT(DISTINCT SQLDASH_TRUNC('week', ordered_at))\"\n"
    )
    store = DashboardStore(tmp_path)
    text, _ = export_lookml(SemanticLayer(store), store)
    assert "sql: TIMESTAMP_TRUNC(ordered_at, ISOWEEK) ;;" in text, text
    assert "DATE_TRUNC" not in text, text


_WINDOWED = (
    "metrics:\n"
    "  revenue:\n"
    "    relation: orders\n"
    "    expr: SUM(amount)\n"
    "    time_dimension: {name: order_date, grain: day}\n"
    "  trailing_28d_revenue:\n"
    "    title: Trailing 28-day revenue\n"
    "    description: Sum of revenue over the last 28 days, per time bucket\n"
    "    relation: orders\n"
    "    expr: SUM(amount)\n"
    "    window: 28 days\n"
    "    time_dimension: {name: order_date, grain: day}\n"
    "  cumulative_revenue:\n"
    "    title: Cumulative revenue\n"
    "    description: Running total of revenue over time\n"
    "    relation: orders\n"
    "    expr: SUM(amount)\n"
    "    cumulative: true\n"
    "    time_dimension: {name: order_date, grain: day}\n"
)

_NOTE = "(sqldash: this measure is the per-bucket sum; {} is not expressible in LookML)"


def test_export_lookml_states_the_window_loss_in_the_file_it_writes(tmp_path):
    """#623: the warning went to the exporter's stdout and stopped there, so the
    view a Looker user or a re-import reads promised a trailing window over a
    plain SUM. The measure now carries the semantics on `tags:` and says what
    Looker itself will compute in its `description:`."""
    text, warnings = _export(tmp_path, _WINDOWED)
    assert 'tags: ["sqldash:window=28 days"]' in text, text
    assert 'tags: ["sqldash:cumulative"]' in text, text
    assert _NOTE.format("a trailing window of 28 days") in text, text
    assert _NOTE.format("a running total") in text, text
    joined = "\n".join(warnings)
    assert "per-bucket sum in Looker" in joined
    assert "import lookml restores it" in joined


def test_a_window_or_cumulative_metric_survives_the_round_trip(tmp_path):
    """The round trip is sqldash's own, so the loss is avoidable rather than only
    announceable: `tags:` is a real LookML field parameter, so the view still
    validates in Looker and the import reads the semantics back."""
    doc, warnings = _round_trip(tmp_path, _WINDOWED)
    trailing = doc["metrics"]["trailing_28d_revenue"]
    assert trailing["window"] == "28 days"
    assert trailing["description"] == "Sum of revenue over the last 28 days, per time bucket"
    cumulative = doc["metrics"]["cumulative_revenue"]
    assert cumulative["cumulative"] is True
    assert cumulative["description"] == "Running total of revenue over time"
    assert "cumulative" not in doc["metrics"]["revenue"]
    assert "window" not in doc["metrics"]["revenue"]
    assert warnings == [], warnings
    MetricsFile.model_validate({**doc, "source": {"type": "duckdb"}})


def _windowed_layer(root, database: str, body: str) -> SemanticLayer:
    root.mkdir(parents=True, exist_ok=True)
    (root / "metrics.yaml").write_text(
        f"source: {{type: duckdb, database: {database}}}\n"
        "relations:\n  orders: {table: orders}\n" + body
    )
    return SemanticLayer(DashboardStore(root))


def test_a_round_tripped_window_metric_returns_the_original_numbers(tmp_path):
    """The bug in numbers: the round-tripped trailing metric answered with the
    all-time total and the cumulative one with per-bucket sums, under names and
    descriptions that still promised the original."""
    pytest.importorskip("lkml")
    rows = [((date(2026, 1, 1) + timedelta(days=i)).isoformat(), float(i + 1)) for i in range(90)]
    database = str(tmp_path / "app.duckdb")
    duck = duckdb.connect(database)
    duck.execute("CREATE TABLE orders (order_date TIMESTAMP, amount DOUBLE)")
    duck.executemany("INSERT INTO orders VALUES (?, ?)", rows)
    duck.close()

    original = _windowed_layer(tmp_path / "orig", database, _WINDOWED)
    text, _ = export_lookml(original, DashboardStore(tmp_path / "orig"))
    view = tmp_path / "orders.view.lkml"
    view.write_text(text)
    doc, _ = import_lookml(view)
    doc["source"] = {"type": "duckdb", "database": database}
    (tmp_path / "rt").mkdir()
    (tmp_path / "rt" / "metrics.yaml").write_text(yaml.safe_dump(doc, sort_keys=False))
    roundtripped = SemanticLayer(DashboardStore(tmp_path / "rt"))

    registry = ExecutionRegistry(max_workers=1)
    try:

        def answer(layer, name):
            resolved = layer.resolve(name)
            sql, bind = compile_metric(resolved, MetricQuery(grain="month"), "qmark")
            result = registry.run_sync(resolved.source, resolved.base_dir, sql, bind, 100)
            return sorted((str(r[0])[:10], round(float(r[1]), 2)) for r in result.rows)

        for name in ("trailing_28d_revenue", "cumulative_revenue"):
            assert answer(roundtripped, name) == answer(original, name), name
        plain = answer(original, "revenue")
        assert answer(original, "trailing_28d_revenue") != plain
        assert answer(original, "cumulative_revenue") != plain
        assert answer(roundtripped, "trailing_28d_revenue") != plain
        assert answer(roundtripped, "cumulative_revenue") != plain
    finally:
        registry.shutdown()


def _tagged_view(tmp_path, tags: str, *, time_group: bool = True):
    group = (
        "  dimension_group: ordered { type: time timeframes: [date] sql: ${TABLE}.d ;; }\n"
        if time_group
        else ""
    )
    view = tmp_path / "v.view.lkml"
    view.write_text(
        "view: orders {\n"
        "  sql_table_name: ORDERS ;;\n" + group + "  measure: revenue {\n"
        "    type: sum\n"
        "    sql: ${TABLE}.amount ;;\n"
        "  }\n"
        "  measure: trailing_28d {\n"
        "    type: sum\n"
        "    sql: ${TABLE}.amount ;;\n"
        '    description: "Sum over the last 28 days ' + _NOTE.format("a trailing window") + '"\n'
        f"    tags: [{tags}]\n"
        "  }\n"
        "}\n"
    )
    return view


def test_a_view_that_lost_the_tag_imports_a_metric_that_describes_itself_truthfully(tmp_path):
    """The tag is the carrier; the note is the fallback. A view hand-edited in
    Looker can lose the tag, and then the honest thing left is a plain SUM whose
    description says it is one, on every surface that reads the file."""
    doc, warnings = import_lookml(_tagged_view(tmp_path, '"analytics"'))
    metric = doc["metrics"]["trailing_28d"]
    assert "window" not in metric
    assert metric["description"].endswith("is not expressible in LookML)")
    assert warnings == [], warnings


@pytest.mark.parametrize(
    ("tags", "reason", "time_group"),
    [
        ('"sqldash:window=28 days", "sqldash:cumulative"', "cannot be combined", True),
        ('"sqldash:window=every other tuesday"', "not a window sqldash can read", True),
        ('"sqldash:rolling_median"', "this version cannot read", True),
        ('"sqldash:window=28 days"', "this view has none", False),
    ],
)
def test_a_tag_that_cannot_be_applied_skips_the_measure(tmp_path, tags, reason, time_group):
    """Same rule as a filter with no sqldash reading (#601): the tag says this
    measure is not the bare aggregate, so importing it as one writes a larger
    number under a name that still promises the original. A missing metric is the
    better failure."""
    doc, warnings = import_lookml(_tagged_view(tmp_path, tags, time_group=time_group))
    assert "trailing_28d" not in doc["metrics"]
    assert "revenue" in doc["metrics"]
    joined = "\n".join(warnings)
    assert "skipped measure 'orders.trailing_28d'" in joined, joined
    assert reason in joined, joined


_FILTERED_METRICS = """
source: {type: duckdb, attach_files: true}
relations:
  orders: {table: orders}
metrics:
  paid_revenue: {relation: orders, expr: SUM(amount), filters: ["status = 'paid'"]}
  open_or_paid: {relation: orders, expr: SUM(amount), filters: ["status IN ('paid', 'open')"]}
  not_refunded:
    relation: orders
    expr: SUM(amount)
    filters: ["(NOT status = 'refunded' OR status IS NULL)"]
  known_status: {relation: orders, expr: SUM(amount), filters: ["status IS NOT NULL"]}
  big_revenue: {relation: orders, expr: SUM(amount), filters: ["amount > 100"]}
  west_revenue: {relation: orders, expr: SUM(amount), filters: ["region LIKE 'us-%'"]}
  active_revenue: {relation: orders, expr: SUM(amount), filters: ["is_active"]}
  inactive_revenue: {relation: orders, expr: SUM(amount), filters: ["NOT (is_active)"]}
  revenue: {relation: orders, expr: SUM(amount)}
"""

_FILTERED_VALUES = {
    "paid_revenue": 450,
    "open_or_paid": 480,
    "not_refunded": 490,
    "known_status": 530,
    "big_revenue": 350,
    "west_revenue": 310,
    "active_revenue": 360,
    "inactive_revenue": 180,
    "revenue": 540,
}


def test_export_carries_a_measure_filter_instead_of_blaming_the_format(tmp_path):
    """`export lookml` dropped every `filters:` and said LookML measures do not
    carry them, which is the field `import lookml` had read since #601: the round
    trip turned paid revenue into total revenue under the same name (#627)."""
    (tmp_path / "metrics.yaml").write_text(_FILTERED_METRICS)
    store = DashboardStore(tmp_path)
    text, warnings = export_lookml(SemanticLayer(store), store)
    assert 'filters: [status: "paid"]' in text, text
    assert 'filters: [status: "paid, open"]' in text, text
    assert 'filters: [status: "-refunded"]' in text, text
    assert 'filters: [status: "-NULL"]' in text, text
    assert 'filters: [amount: ">100"]' in text, text
    assert 'filters: [region: "us-%"]' in text, text
    assert 'filters: [is_active: "yes"]' in text, text
    assert 'filters: [is_active: "no"]' in text, text
    assert "dimension: status" in text, text
    assert "type: number" in text, text
    assert "type: yesno" in text, text
    assert not any("do not carry" in w for w in warnings), warnings
    assert not warnings, warnings


def test_a_filtered_metric_keeps_its_number_through_an_export_import_round_trip(tmp_path):
    """The number is the point: a filter that survives the round trip in spelling
    but not in rows would be the same bug with better-looking LookML."""
    pytest.importorskip("lkml")
    (tmp_path / "metrics.yaml").write_text(_FILTERED_METRICS)
    store = DashboardStore(tmp_path)
    text, warnings = export_lookml(SemanticLayer(store), store)
    assert not warnings, warnings
    view = tmp_path / "roundtrip.view.lkml"
    view.write_text(text)
    doc, import_warnings = import_lookml(view)
    assert not import_warnings, import_warnings
    MetricsFile.model_validate({**doc, "source": {"type": "duckdb"}})

    conn = duckdb.connect()
    conn.execute(
        "CREATE TABLE orders AS SELECT * FROM (VALUES "
        "('paid', 100, 'us-west', TRUE), ('paid', 200, 'eu-west', TRUE), "
        "('paid', 150, 'us-east', FALSE), ('refunded', 50, 'us-west', TRUE), "
        "('open', 30, 'eu-west', FALSE), (NULL, 10, 'us-west', TRUE)) "
        "t(status, amount, region, is_active)"
    )

    def value(metric):
        where = " AND ".join(f"({f})" for f in metric.get("filters", [])) or "TRUE"
        return conn.execute(f"SELECT {metric['expr']} FROM orders WHERE {where}").fetchone()[0]

    authored = yaml.safe_load(_FILTERED_METRICS)["metrics"]
    for name, expected in _FILTERED_VALUES.items():
        assert value(authored[name]) == expected, name
        assert value(doc["metrics"][name]) == expected, name


@pytest.mark.parametrize(
    ("filters", "reason"),
    [
        ("[\"status != 'refunded'\"]", "not a field/value comparison"),
        ('["amount <> 5"]', "not a field/value comparison"),
        ("[\"status = 'a,b'\"]", "not a field/value comparison"),
        ("[\"LOWER(status) = 'paid'\"]", "not a field/value comparison"),
        ("[\"status = 'NULL'\"]", "not a field/value comparison"),
        ("[\"region LIKE 'us_%'\"]", "not a field/value comparison"),
        ('["amount > 100 AND amount < 500"]', "not a field/value comparison"),
        ('["amount > (SELECT AVG(amount) FROM orders)"]', "not a field/value comparison"),
        ("[\"status = 'paid'\", \"status = 'open'\"]", "already carries another"),
    ],
)
def test_a_filter_with_no_lookml_reading_warns_about_the_term_not_the_format(
    tmp_path, filters, reason
):
    """The old warning said the format could not hold a filter, so there was
    nothing for the user to change. The remaining warning names the term."""
    (tmp_path / "metrics.yaml").write_text(
        "source: {type: duckdb, attach_files: true}\n"
        "metrics:\n"
        f"  odd: {{table: orders, expr: SUM(amount), filters: {filters}}}\n"
    )
    store = DashboardStore(tmp_path)
    text, warnings = export_lookml(SemanticLayer(store), store)
    assert "measure: odd" in text, text
    assert any(
        "metric 'odd' filter" in w and reason in w and "counts rows the metric excludes" in w
        for w in warnings
    ), warnings
    assert not any("do not carry" in w for w in warnings), warnings


def test_an_exported_filter_goes_through_the_macro_seam(tmp_path):
    """A filter is author SQL leaving sqldash, so it has to be expanded like
    every other `sql:` body — otherwise the term never matches the dimension it
    is about and the filter is dropped as unreadable (#628)."""
    (tmp_path / "metrics.yaml").write_text(
        "source: {type: duckdb, attach_files: true}\n"
        "metrics:\n"
        "  january_revenue:\n"
        "    table: orders\n"
        "    expr: SUM(amount)\n"
        "    dimensions: [{name: month, expr: \"SQLDASH_TRUNC('month', ordered_at)\"}]\n"
        "    filters: [\"SQLDASH_TRUNC('month', ordered_at) = '2024-01-01'\"]\n"
    )
    store = DashboardStore(tmp_path)
    text, warnings = export_lookml(SemanticLayer(store), store)
    assert 'filters: [month: "2024-01-01"]' in text, text
    assert "SQLDASH_TRUNC" not in text, text
    assert not warnings, warnings


def test_a_filter_on_a_dropped_dimension_is_dropped_with_it(tmp_path):
    """A `filters:` line naming a field the view does not define is LookML Looker
    refuses, so a dimension lost to a name collision takes its filters with it."""
    (tmp_path / "metrics.yaml").write_text(
        "source: {type: duckdb, attach_files: true}\n"
        "metrics:\n"
        "  a: {table: orders, expr: SUM(x), time_dimension: {name: d, expr: dt, grain: day}}\n"
        "  b: {table: orders, expr: SUM(y), filters: [\"d = 'x'\"]}\n"
    )
    store = DashboardStore(tmp_path)
    text, warnings = export_lookml(SemanticLayer(store), store)
    assert "filters: [" not in text, text
    assert any(
        "measure 'b' filters on 'd'" in w and "counts rows the metric excludes" in w
        for w in warnings
    ), warnings


def test_two_metrics_reading_one_dimension_as_different_types_keep_the_first(tmp_path):
    """`type:` is per dimension and per view, but filters are per measure. A
    numeric bound written onto a dimension another measure filters as a string
    makes that measure's filter unreadable on the way back in."""
    (tmp_path / "metrics.yaml").write_text(
        "source: {type: duckdb, attach_files: true}\n"
        "metrics:\n"
        '  by_bound: {table: orders, expr: SUM(amount), filters: ["code > 100"]}\n'
        "  by_text: {table: orders, expr: SUM(amount), filters: [\"code = 'A1'\"]}\n"
    )
    store = DashboardStore(tmp_path)
    text, warnings = export_lookml(SemanticLayer(store), store)
    assert 'filters: [code: ">100"]' in text, text
    assert 'code: "A1"' not in text, text
    assert any(
        "metric 'by_text' filter" in w and "already read as number" in w for w in warnings
    ), warnings


def test_a_filter_on_a_parenthesised_dimension_finds_it(tmp_path):
    """`import lookml` wraps a dimension's sql to splice it into a filter, so a
    dimension already written as `(amount + 1)` comes back as
    `((amount + 1)) > 100`. Comparing that to the dimension verbatim dropped the
    filter on the way out again."""
    pytest.importorskip("lkml")
    view = tmp_path / "orders.view.lkml"
    view.write_text(
        "view: orders {\n  sql_table_name: orders ;;\n"
        "  dimension: tier { type: number sql: (amount + 1) ;; }\n"
        '  measure: big { type: sum sql: ${TABLE}.amount ;; filters: [tier: ">100"] }\n}\n'
    )
    doc, warnings = import_lookml(view)
    assert doc["metrics"]["big"]["filters"] == ["((amount + 1)) > 100"], doc
    assert not warnings, warnings
    (tmp_path / "metrics.yaml").write_text(
        yaml.safe_dump({**doc, "source": {"type": "duckdb", "attach_files": True}})
    )
    store = DashboardStore(tmp_path)
    text, warnings = export_lookml(SemanticLayer(store), store)
    assert 'filters: [tier: ">100"]' in text, text
    assert not warnings, warnings
    again = tmp_path / "roundtrip.view.lkml"
    again.write_text(text)
    assert import_lookml(again)[0]["metrics"]["big"]["filters"] == ["((amount + 1)) > 100"]
