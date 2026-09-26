from sqldash.project.store import DashboardStore
from sqldash.scaffold import create_demo
from sqldash.semantics import SemanticLayer
from sqldash.semantics.context import export_context


def test_context_export(tmp_path):
    create_demo(tmp_path)
    store = DashboardStore(tmp_path)
    text = export_context(SemanticLayer(store), store)
    assert "### `revenue` — Revenue" in text
    assert "`SUM(amount)`" in text
    assert "default grain: day" in text
    assert "```sql" in text
    assert "query_metric" in text


def test_context_export_includes_metric_tiles(tmp_path):
    create_demo(tmp_path)
    store = DashboardStore(tmp_path)
    text = export_context(SemanticLayer(store), store)
    assert "metric tiles" in text
    assert "total_revenue → `revenue`" in text


def test_context_includes_authoring_reference(tmp_path):
    create_demo(tmp_path)
    store = DashboardStore(tmp_path / ".sqldash")
    text = export_context(SemanticLayer(store), store)
    assert "Authoring sqldash YAML" in text
    assert "{% if param %}" in text
    assert "no `connection:` key" in text
    assert "validate_dashboard" in text


def test_context_export_does_not_present_an_ambiguous_metric_as_canonical(tmp_path):
    """export context is the file agents are told to prefer over improvising, and
    it printed one of two conflicting definitions as THE definition for a name
    every resolve path refuses. #103.
    """
    for n in ("a", "b"):
        (tmp_path / f"{n}.yaml").write_text(
            f"title: Dash {n}\n"
            "source: {type: duckdb, database: ':memory:'}\n"
            "metrics:\n  inline_rev: {sql: 'SELECT 1 AS amount', expr: 'SUM(amount)'}\n"
            "tiles: [{id: w, metric: inline_rev}]\n"
        )
    store = DashboardStore(tmp_path)
    text = export_context(SemanticLayer(store), store)
    assert "Ambiguous — not resolvable by this name" in text
    assert "--dashboard a" in text


def test_context_export_names_running_totals_and_windows(tmp_path):
    """Both share revenue's `SUM(amount)`, so the definition line alone reads
    as the plain metric (#605)."""
    create_demo(tmp_path)
    store = DashboardStore(tmp_path)
    text = export_context(SemanticLayer(store), store)
    sections = {s.split("`")[0]: s for s in text.split("### `")[1:]}
    assert "- cumulative: a running total" in sections["cumulative_revenue"]
    assert "- window: trailing 28 days" in sections["trailing_28d_revenue"]
    assert "cumulative" not in sections["revenue"]
    assert "window" not in sections["revenue"]


_MACRO_AND_PLAIN = """
source: {type: duckdb, database: ':memory:'}
metrics:
  active_weeks:
    table: orders
    expr: "COUNT(DISTINCT SQLDASH_TRUNC('week', ordered_at))"
  plain_revenue:
    table: orders
    expr: SUM(amount)
"""


def test_context_says_the_trunc_macro_is_not_runnable_sql(tmp_path):
    """This file is read by agents that then write SQL. The definition shown is
    the author's text, not compiled SQL, so an expr the warehouse cannot run has
    to say so — and a metric without one gets no note."""
    (tmp_path / "metrics.yaml").write_text(_MACRO_AND_PLAIN)
    store = DashboardStore(tmp_path)
    text = export_context(SemanticLayer(store), store)
    macro_section = text.split("### `active_weeks`")[1].split("###")[0]
    plain_section = text.split("### `plain_revenue`")[1].split("###")[0]
    assert "SQLDASH_TRUNC" in macro_section
    assert "not a warehouse function" in macro_section, macro_section
    assert "- note:" not in plain_section, plain_section
