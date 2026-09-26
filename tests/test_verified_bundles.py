import re
from pathlib import Path

import duckdb
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from sqldash.cli import app
from sqldash.models.agents import AgentsFile
from sqldash.params import ParamError
from sqldash.project.store import DashboardStore, yaml
from sqldash.semantics.agents import (
    AgentLayer,
    WorkspaceAgentLayer,
    prepare_verified,
    render_prompt,
)
from sqldash.semantics.bind import bind_metric
from sqldash.semantics.cortex import render_yaml
from sqldash.semantics.cortex_agent import build_cortex_agent
from sqldash.semantics.layer import SemanticError, SemanticLayer


@pytest.fixture
def project(tmp_path):
    metrics = {
        "source": {"type": "snowflake", "account": "a", "database": "DB", "schema": "PUBLIC"},
        "relations": {"orders": {"table": "ORDERS"}},
        "metrics": {
            name: {
                "relation": "orders",
                "expr": expr,
                "dimensions": [{"name": "region", "expr": "sales_region"}],
                "time_dimension": {"name": "ordered", "expr": "order_date", "grain": "day"},
            }
            for name, expr in {"revenue": "SUM(amount)", "order_count": "COUNT(*)"}.items()
        },
    }
    agents = {
        "tools": {
            "health": {
                "description": "Revenue and orders",
                "params": {"region": {"type": "select", "options": ["US", "EU"], "default": "US"}},
                "queries": [
                    {
                        "metric": "revenue",
                        "filters": {"region": "{{ region }}"},
                        "start": "2026-02-01",
                        "end": "2026-02-28",
                        "compare": "previous_period",
                    },
                    {"metric": "order_count", "filters": {"region": "{{ region }}"}},
                ],
            },
        },
        "agents": {
            "finance": {
                "description": "Finance",
                "instructions": "Use metrics",
                "metrics": ["revenue", "order_count"],
                "dimensions": ["region", "ordered"],
                "tools": ["health"],
                "sample_questions": ["How is revenue?"],
                "verified": [
                    {
                        "name": "us_health",
                        "question": (
                            "US revenue in February versus the previous period, "
                            "and all-time orders?"
                        ),
                        "tool": "health",
                    }
                ],
            },
        },
    }
    store = DashboardStore(tmp_path)
    layer = SemanticLayer(store)

    def write():
        (tmp_path / "metrics.yaml").write_text(render_yaml(metrics))
        (tmp_path / "agents.yaml").write_text(render_yaml(agents))

    write()
    return agents, metrics, store, layer, write


def test_whole_bundle_sql_matches_governed_queries(project):
    agents, _, store, layer, _ = project
    agent = AgentLayer(store, layer).resolve("finance")
    assert AgentLayer(store, layer).check_references()[0] == []
    assert "Verified examples" in render_prompt(agent)
    _, view, deployment, warnings = build_cortex_agent(
        layer, store, "finance", schema="DEST.AGENTS"
    )
    (example,) = view["verified_queries"]
    assert example["question"] == agents["agents"]["finance"]["verified"][0]["question"]
    assert "verified_at" not in example
    assert "verified_by" not in example
    assert "'previous_rows'" in example["sql"]
    assert "$$order_count$$" in example["sql"]
    assert "DEST.AGENTS.finance_metrics" in example["sql"]
    assert "us_health" in deployment
    assert not any("us_health' omitted" in w for w in warnings)

    connection = duckdb.connect()
    connection.execute("CREATE TABLE ORDERS(amount INTEGER, sales_region VARCHAR, order_date DATE)")
    connection.execute(
        "INSERT INTO ORDERS VALUES (10, 'US', '2026-01-15'), "
        "(20, 'US', '2026-02-10'), (50, 'EU', '2026-02-10')"
    )
    connection.execute(
        "CREATE VIEW logical_orders AS SELECT amount AS revenue, 1 AS order_count, "
        "sales_region AS region, order_date AS ordered FROM ORDERS"
    )
    connection.execute("CREATE MACRO AGG(x) AS SUM(x)")
    try:
        compiled = re.findall(
            r"sqldash_verified_\d+(?:_previous)? AS \(\n(.*?)\n\)", example["sql"], re.S
        )
        prepared = prepare_verified(agent, agent.definition.verified[0])
        bounds = [prepared[0][0], prepared[0][2], prepared[1][0]]
        assert len(compiled) == len(bounds)
        for sql, bound in zip(compiled, bounds, strict=True):
            logical_sql = sql.replace("DEST.AGENTS.finance_metrics", "logical_orders AS orders")
            actual = connection.execute(logical_sql).fetchall()
            runtime_sql = re.sub(r":\d+", "?", bound.sql)
            assert actual == connection.execute(runtime_sql, bound.bind).fetchall()
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ("unknown_tool", "one of the agent's tools"),
        ("sql_tool", "metric bundle"),
        ("duplicate", "unique"),
        ("blank", "blank"),
        ("extra", "Extra inputs"),
    ],
)
def test_invalid_schema(project, change, match):
    agents, _, _, _, _ = project
    definition = agents["agents"]["finance"]
    example = definition["verified"][0]
    if change == "unknown_tool":
        example["tool"] = "missing"
    elif change == "sql_tool":
        agents["tools"]["health"] = {"description": "SQL", "sql": "SELECT 1"}
    elif change == "duplicate":
        definition["verified"].append(dict(example))
    elif change == "blank":
        example["question"] = "  "
    else:
        example["typo"] = True
    with pytest.raises(ValidationError, match=match):
        AgentsFile.model_validate(agents)


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ("metric_scope", "outside the agent's allow-list"),
        ("dimension_scope", "dimensions outside"),
        ("unknown_arg", "unknown arguments"),
        ("select", "must be one of"),
        ("missing_arg", "missing argument"),
        ("unknown_metric", "no metric"),
        ("unknown_dimension", "unknown filter dimension"),
        ("no_time", "no time_dimension"),
        ("compare_no_window", "needs start and end"),
    ],
)
def test_invalid_examples_fail_lint_and_export_without_output(project, change, match, tmp_path):
    agents, metrics, store, layer, write = project
    agent = agents["agents"]["finance"]
    tool = agents["tools"]["health"]
    if change == "metric_scope":
        agent["metrics"] = ["revenue"]
    elif change == "dimension_scope":
        agent["dimensions"] = ["region"]
    elif change == "unknown_arg":
        agent["verified"][0]["args"] = {"regoin": "US"}
    elif change == "select":
        agent["verified"][0]["args"] = {"region": "APAC"}
    elif change == "missing_arg":
        del tool["params"]["region"]["default"]
    elif change == "unknown_metric":
        tool["queries"][0]["metric"] = "missing"
        agent["metrics"] = []
    elif change == "unknown_dimension":
        tool["queries"][0]["filters"] = {"typo": "{{ region }}"}
        agent["dimensions"] = []
    elif change == "no_time":
        del metrics["metrics"]["revenue"]["time_dimension"]
    else:
        del tool["queries"][0]["start"]
        del tool["queries"][0]["end"]
    write()
    errors, _ = AgentLayer(store, layer).check_references()
    assert any(re.search(match, e) for e in errors), errors
    with pytest.raises(
        SemanticError
        if change not in {"unknown_arg", "select", "missing_arg", "compare_no_window"}
        else ValueError,
        match=match,
    ):
        build_cortex_agent(layer, store, "finance")
    output = tmp_path / "output.sql"
    result = CliRunner().invoke(
        app, ["export", "cortex-agent", "finance", str(store.root), "--out", str(output)]
    )
    assert result.exit_code == 1
    assert not output.exists()


@pytest.mark.parametrize("flag", ["cumulative", "window"])
def test_lossy_bundle_is_omitted_whole(project, flag):
    _agents, metrics, store, layer, write = project
    metrics["metrics"]["order_count"][flag] = {"cumulative": True, "window": "7 days"}[flag]
    write()
    _, view, _, warnings = build_cortex_agent(layer, store, "finance")
    assert not view.get("verified_queries")
    assert any("verified 'us_health' omitted" in w for w in warnings)


def test_authored_verification_metadata_and_hostile_values(project):
    agents, _, store, layer, write = project
    payload = "O'Brien $$ \\ :1 ? $"
    tool = agents["tools"]["health"]
    tool["params"]["region"] = {"default": payload}
    example = agents["agents"]["finance"]["verified"][0]
    example.update(verified_by="Ada", verified_at=1772645863)
    write()
    _, view, _, _ = build_cortex_agent(layer, store, "finance")
    exported = view["verified_queries"][0]
    assert exported["verified_by"] == "Ada"
    assert exported["verified_at"] == 1772645863
    assert "O''Brien $$ \\\\ :1 ? $" in exported["sql"]


def test_workspace_verified_examples_keep_project_local_references(project):
    _, _, store, layer, _ = project
    workspace = WorkspaceAgentLayer({"acme": AgentLayer(store, layer)})
    agent = workspace.resolve("acme/finance")
    assert "call `acme__health`" in render_prompt(agent)
    assert len(prepare_verified(agent, agent.definition.verified[0])) == 2
    assert workspace.check_references()[0] == []


def test_verified_dates_cannot_drift_between_exports(project):
    agents, _, store, layer, write = project
    agents["tools"]["health"]["queries"][0]["start"] = "-30d"
    write()
    errors, _ = AgentLayer(store, layer).check_references()
    assert any("absolute start/end dates" in e for e in errors)
    with pytest.raises(ParamError, match="absolute start/end dates"):
        build_cortex_agent(layer, store, "finance")


def test_scoped_example_with_grouping_and_in_filter(project):
    agents, _, store, layer, write = project
    agents["tools"]["health"]["params"]["region"] = {"default": ["US", "EU"]}
    agents["tools"]["health"]["queries"][0].update(dimensions=["region"], grain="month")
    write()
    _, view, _, _ = build_cortex_agent(layer, store, "finance")
    sql = view["verified_queries"][0]["sql"]
    assert "orders.region IN ($$US$$, $$EU$$)" in sql
    assert "DATE_TRUNC('month', orders.ordered)" in sql
    assert "GROUP BY 1, 2" in sql


def test_cli_spec_and_view_include_verified_examples(project, tmp_path):
    _, _, store, _, _ = project
    out, view_out = tmp_path / "spec.yaml", tmp_path / "arbitrary-view.yaml"
    result = CliRunner().invoke(
        app,
        [
            "export",
            "cortex-agent",
            "finance",
            str(store.root),
            "--spec-only",
            "--out",
            str(out),
            "--view-out",
            str(view_out),
            "--schema",
            "DEST.AGENTS",
        ],
    )
    assert result.exit_code == 0, result.output
    view = yaml.load(view_out.read_text())
    spec = yaml.load(out.read_text())
    assert len(view["verified_queries"]) == 1
    assert spec["tool_resources"]["Analyst"]["semantic_view"] in view["verified_queries"][0]["sql"]


def test_documented_examples_share_a_working_contract(tmp_path):
    docs = Path(__file__).resolve().parents[1] / "docs"
    blocks = re.findall(r"```yaml\n(.*?)```", (docs / "agents.md").read_text(), re.S)
    agents = yaml.load(blocks[0])
    definition = agents["agents"]["finance_analyst"]
    for block in blocks[1:]:
        data = yaml.load(block)
        if "verified" in data:
            definition["verified"] = data["verified"]
        if "agents" in data:
            definition.update(data["agents"]["finance_analyst"])
    metrics = re.findall(r"```yaml\n(.*?)```", (docs / "semantic-layer.md").read_text(), re.S)[0]
    (tmp_path / "metrics.yaml").write_text(metrics)
    (tmp_path / "agents.yaml").write_text(render_yaml(agents))
    store = DashboardStore(tmp_path)
    layer = SemanticLayer(store)
    assert AgentLayer(store, layer).check_references()[0] == []
    for case in definition["evals"]:
        expected = case["expect"]
        if expected.get("tool") == "query_metric":
            args = {k: v for k, v in expected["args"].items() if k != "compare"}
            assert bind_metric(layer, **args).sql
    _, view, _, _ = build_cortex_agent(layer, store, "finance_analyst")
    assert len(view["verified_queries"]) == len(definition["verified"])


@pytest.mark.parametrize("value", ["not-a-number", float("nan"), float("inf")])
def test_verified_number_arguments_fail_before_export(project, value):
    agents, _, store, layer, write = project
    agents["tools"]["health"]["params"]["region"] = {"type": "number", "default": value}
    write()
    errors, _ = AgentLayer(store, layer).check_references()
    assert any("must be a finite number" in error for error in errors)
    with pytest.raises(ParamError, match="must be a finite number"):
        build_cortex_agent(layer, store, "finance")


def test_verified_numeric_strings_use_mcp_coercion(project):
    agents, _, store, layer, write = project
    agents["tools"]["health"]["params"]["region"] = {"type": "number"}
    agents["agents"]["finance"]["verified"][0]["args"] = {"region": "10.5"}
    write()
    _, view, _, _ = build_cortex_agent(layer, store, "finance")
    assert "orders.region = 10.5" in view["verified_queries"][0]["sql"]


def test_a_filtered_metric_is_verified_through_the_views_folded_expr(project):
    """The export folds a metric's filters into the view's expr, so the verified
    SQL reads the filtered number from the view. Applying the filter again as a
    WHERE named a column the view does not expose."""
    _, metrics, store, layer, write = project
    metrics["metrics"]["order_count"]["filters"] = ["amount > 0"]
    write()
    _, view, _, warnings = build_cortex_agent(layer, store, "finance")
    [verified] = view["verified_queries"]
    assert "amount > 0" not in verified["sql"], verified["sql"]
    exprs = {m["name"]: m["expr"] for t in view["tables"] for m in t["metrics"]}
    assert "CASE WHEN (amount > 0)" in exprs["order_count"], exprs
    assert not [w for w in warnings if "verified 'us_health' omitted" in w], warnings


def test_authored_dollar_quoted_filter_never_reaches_literal_substitution(project):
    """The filter stays in the view, where nothing binds arguments, so a `:1`
    inside the author's dollar quote is never replaced by an argument."""
    _, metrics, store, layer, write = project
    metrics["metrics"]["revenue"]["filters"] = ["note = $$x:1$$"]
    write()
    _, view, _, _ = build_cortex_agent(layer, store, "finance")
    exprs = {m["name"]: m["expr"] for t in view["tables"] for m in t["metrics"]}
    assert "$$x:1$$" in exprs["revenue"], exprs
    for verified in view.get("verified_queries", []):
        assert "note =" not in verified["sql"], verified["sql"]


def test_verified_example_survives_a_dimension_that_uses_the_trunc_macro(project):
    """The view carries author SQL as the export seam wrote it, so the projection
    check has to compare against the exported form. Comparing raw text dropped
    every example over a macro dimension, with a warning blaming the view."""
    _, metrics, store, layer, write = project
    # Both metrics: they share one table, and a table keeps the first spelling of
    # a dimension name, so leaving the other on `sales_region` drops that leg for
    # a reason that has nothing to do with the macro.
    for definition in metrics["metrics"].values():
        definition["dimensions"] = [
            {"name": "region", "expr": "SQLDASH_TRUNC('month', signed_up_at)"}
        ]
    write()
    _, view, _, warnings = build_cortex_agent(layer, store, "finance")
    assert not [w for w in warnings if "omitted: dimension 'region'" in w], warnings
    assert len(view["verified_queries"]) == 1, warnings
    assert "SQLDASH_TRUNC" not in render_yaml(view), render_yaml(view)


def test_verified_example_survives_a_session_time_dimension(project):
    """The view's time dimension is the exported `CAST(.. AS TIMESTAMP_LTZ)`, so
    the projection check compares against that, not the raw column."""
    _, metrics, store, layer, write = project
    for definition in metrics["metrics"].values():
        definition["time_dimension"]["timezone"] = "session"
    write()
    _, view, _, warnings = build_cortex_agent(layer, store, "finance")
    assert view["tables"][0]["time_dimensions"][0]["expr"] == "CAST(order_date AS TIMESTAMP_LTZ)"
    assert not [w for w in warnings if "us_health' omitted" in w], warnings
    assert len(view["verified_queries"]) == 1, warnings
