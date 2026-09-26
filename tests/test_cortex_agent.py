import io
import re

import duckdb
import pytest
from ruamel.yaml import YAML
from typer.testing import CliRunner

from sqldash import workspace
from sqldash.cli import app
from sqldash.project.store import DashboardStore
from sqldash.semantics import SemanticError, SemanticLayer
from sqldash.semantics.cortex_agent import build_cortex_agent

METRICS = """
source: {type: snowflake, account: a, database: DB, schema: PUBLIC, username: u}
relations:
  orders: {table: ORDERS}
metrics:
  revenue:
    relation: orders
    expr: SUM(amount)
    dimensions: [{name: region}, {name: category}]
    time_dimension: {name: order_date, grain: day}
  order_count:
    relation: orders
    expr: COUNT(*)
    dimensions: [{name: private_segment}]
"""
AGENTS = """
agents:
  finance:
    title: Finance analyst
    description: Answers finance questions
    instructions: Use governed metrics
    response: Be concise
    metrics: [revenue]
    dimensions: [region]
    sample_questions: [What is revenue?]
"""


@pytest.fixture
def project(tmp_path):
    (tmp_path / "metrics.yaml").write_text(METRICS)
    (tmp_path / "agents.yaml").write_text(AGENTS)
    store = DashboardStore(tmp_path)
    return SemanticLayer(store), store


def test_scoped_view_and_agent_spec(project):
    layer, store = project
    spec, view, sql, warnings = build_cortex_agent(layer, store, "finance", model="test-model")
    assert spec["models"] == {"orchestration": "test-model"}
    assert spec["instructions"] == {
        "orchestration": "Use governed metrics",
        "response": "Be concise",
        "sample_questions": [{"question": "What is revenue?"}],
    }
    assert spec["tool_resources"]["Analyst"]["semantic_view"] == "DB.PUBLIC.finance_metrics"
    (table,) = view["tables"]
    assert [m["name"] for m in table["metrics"]] == ["revenue"]
    assert [d["name"] for d in table["dimensions"]] == ["region"]
    assert not table.get("time_dimensions")
    assert not view.get("verified_queries")
    assert "private_segment" not in sql
    assert "CREATE OR REPLACE AGENT DB.PUBLIC.finance" in sql
    assert "SYSTEM$CREATE_SEMANTIC_VIEW_FROM_YAML" in sql
    assert any("verified queries omitted" in w for w in warnings)
    assert len(layer.resolve("revenue").definition.dimensions) == 2


def test_empty_allowlists_keep_whole_view(project):
    layer, store = project
    (store.root / "agents.yaml").write_text(
        AGENTS.replace("metrics: [revenue]", "metrics: []").replace(
            "dimensions: [region]", "dimensions: []"
        )
    )
    _, view, _, _ = build_cortex_agent(layer, store, "finance")
    assert len(view["tables"][0]["metrics"]) == 2
    assert len(view["tables"][0]["dimensions"]) == 3
    assert view["tables"][0]["time_dimensions"][0]["name"] == "order_date"


@pytest.mark.parametrize(
    ("before", "after", "match"),
    [
        ("metrics: [revenue]", "metrics: [missing_metric]", "missing_metric"),
        ("dimensions: [region]", "dimensions: [missing_dim]", "unknown dimensions"),
    ],
)
def test_invalid_allowlists_fail(project, before, after, match):
    layer, store = project
    (store.root / "agents.yaml").write_text(AGENTS.replace(before, after))
    with pytest.raises(SemanticError, match=match):
        build_cortex_agent(layer, store, "finance")


def test_unsupported_fields_warn(project):
    layer, store = project
    (store.root / "agents.yaml").write_text(
        "tools:\n  totals:\n    description: Get totals\n    queries: [{metric: revenue}]\n"
        + AGENTS
        + "    tools: [totals]\n    sql: true\n"
        "    uses: [{server: crm, for: accounts}]\n"
        "    evals: [{question: Hi, answer_has: [Hello]}]\n"
    )
    spec, _, _, warnings = build_cortex_agent(layer, store, "finance")
    assert len(spec["tools"]) == 1
    for expected in ("tool 'totals' omitted", "uses", "evals omitted", "sql: true omitted"):
        assert any(expected in w for w in warnings)


@pytest.mark.parametrize(
    "payload",
    [
        "Use governed metrics",
        "O'Brien $$; DROP AGENT x; -- \\n",
        "Quote: '\nNew line, literal \\n, tab \t and Unicode £",
        "Backslash before quote: \\' $$",
        "Amounts in $",
        "$$",
    ],
)
def test_sql_literals_cannot_end_spec(project, payload):
    layer, store = project
    data = YAML().load(AGENTS)
    for field in ("instructions", "description", "title"):
        data["agents"]["finance"][field] = payload
    buffer = io.StringIO()
    YAML().dump(data, buffer)
    (store.root / "agents.yaml").write_text(buffer.getvalue())
    spec, view, sql, _ = build_cortex_agent(layer, store, "finance")
    literal = r"(?:\$\$(?:(?!\$\$).)*\$\$|'(?:''|\\.|[^'\\])*')"
    match = re.fullmatch(
        rf"CALL SYSTEM\$CREATE_SEMANTIC_VIEW_FROM_YAML\(\s*"
        rf"(?P<schema>{literal}),\s*(?P<view>{literal})\s*\);\s*"
        rf"CREATE OR REPLACE AGENT DB.PUBLIC.finance\s*"
        rf"COMMENT = (?P<comment>{literal})\s*"
        rf"PROFILE = (?P<profile>{literal})\s*"
        rf"FROM SPECIFICATION\s*(?P<spec>{literal});\s*",
        sql,
        re.DOTALL,
    )
    assert match, sql
    decoded = {}
    with duckdb.connect() as conn:
        for name, value in match.groupdict().items():
            # DuckDB E strings use Snowflake's backslash escaping for single-quoted literals.
            expression = "E" + value if value.startswith("'") else value
            statements = conn.extract_statements(f"SELECT {expression}")
            assert len(statements) == 1
            decoded[name] = conn.execute(statements[0]).fetchone()[0]
    assert decoded["schema"] == "DB.PUBLIC"
    assert decoded["comment"] == payload
    assert YAML().load(decoded["profile"]) == {"display_name": payload}
    assert YAML().load(decoded["view"]) == view
    assert YAML().load(decoded["spec"]) == spec
    assert spec["instructions"]["orchestration"] == payload


def test_invalid_schema_fails(project):
    layer, store = project
    with pytest.raises(SemanticError, match="identifier"):
        build_cortex_agent(layer, store, "finance", schema="DB.S;DROP")


@pytest.mark.parametrize("spec_only", [True, False])
def test_cli_files_and_errors(project, tmp_path, spec_only):
    _, store = project
    out, view = tmp_path / "agent.yaml", tmp_path / "view.yaml"
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "export",
            "cortex-agent",
            "finance",
            str(store.root),
            *(["--spec-only"] if spec_only else []),
            "--out",
            str(out),
            "--view-out",
            str(view),
            "--schema",
            "DEST.AGENTS",
        ],
    )
    assert result.exit_code == 0, result.output
    document = YAML().load(view.read_text())
    assert view.stem != document["name"]
    if spec_only:
        assert f"create semantic view DEST.AGENTS.{document['name']}" in result.output
        spec = YAML().load(out.read_text())
        assert spec["tool_resources"]["Analyst"]["semantic_view"] == (
            f"DEST.AGENTS.{document['name']}"
        )
    else:
        assert view.read_text() in out.read_text()
        assert f"semantic_view: DEST.AGENTS.{document['name']}" in out.read_text()
    result = runner.invoke(app, ["export", "cortex-agent", "missing", str(store.root)])
    assert result.exit_code == 1
    assert "no agent named" in result.output
    result = runner.invoke(
        app,
        [
            "export",
            "cortex-agent",
            "finance",
            str(store.root),
            "--out",
            str(out),
            "--view-out",
            str(out),
        ],
    )
    assert result.exit_code != 0


def test_scoped_export_omits_dashboard_sql(project):
    layer, store = project
    (store.root / "overview.yaml").write_text(
        "title: Overview\n"
        "source: {type: snowflake, account: a, database: DB, schema: PUBLIC, username: u}\n"
        "queries: {secret: 'SELECT private_segment FROM ORDERS'}\n"
        "tiles: [{title: Secret segments, query: secret}]\n"
    )
    _, view, sql, _ = build_cortex_agent(layer, store, "finance")
    assert not view.get("verified_queries")
    assert "SELECT private_segment" not in sql
    (store.root / "agents.yaml").write_text(
        AGENTS.replace("metrics: [revenue]", "metrics: []").replace(
            "dimensions: [region]", "dimensions: []"
        )
    )
    _, view, _, _ = build_cortex_agent(layer, store, "finance")
    assert view["verified_queries"][0]["sql"] == "SELECT private_segment FROM ORDERS"


def test_workspace_fallback_requires_project_target(project, tmp_path, monkeypatch):
    _, store = project
    monkeypatch.setattr(workspace, "registry_path", lambda: tmp_path / "repos.yaml")
    workspace.add_repo(str(store.root), name="finance_repo")
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    output = empty / "agent.sql"
    result = CliRunner().invoke(
        app, ["export", "cortex-agent", "finance", "--out", str(output)], catch_exceptions=False
    )
    assert result.exit_code == 1
    assert "requires a single project" in result.output
    assert "pass a project path" in result.output
    assert not output.exists()
    result = CliRunner().invoke(
        app, ["export", "cortex-agent", "finance", str(store.root)], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("schema", [None, "DEST.AGENTS"])
def test_no_snowflake_metrics_reports_source_error(project, schema):
    layer, store = project
    (store.root / "metrics.yaml").write_text(
        METRICS.replace(
            "{type: snowflake, account: a, database: DB, schema: PUBLIC, username: u}",
            "{type: duckdb}",
        )
    )
    with pytest.raises(SemanticError, match=r"no exportable metrics.*snowflake"):
        build_cortex_agent(layer, store, "finance", schema=schema)


@pytest.mark.parametrize("schema", ["DB.TABLE", "SELECT.PUBLIC"])
def test_reserved_destination_identifier_fails(project, schema):
    layer, store = project
    with pytest.raises(SemanticError, match="reserved"):
        build_cortex_agent(layer, store, "finance", schema=schema)


def test_reserved_agent_identifier_fails(project):
    layer, store = project
    (store.root / "agents.yaml").write_text(AGENTS.replace("finance:", "table:"))
    with pytest.raises(SemanticError, match="reserved"):
        build_cortex_agent(layer, store, "table")


def test_dimension_scope_survives_two_included_metrics_on_shared_table(project):
    layer, store = project
    (store.root / "agents.yaml").write_text(
        AGENTS.replace("metrics: [revenue]", "metrics: [revenue, order_count]")
    )
    _, view, sql, _ = build_cortex_agent(layer, store, "finance")
    (table,) = view["tables"]
    assert {m["name"] for m in table["metrics"]} == {"revenue", "order_count"}
    assert [d["name"] for d in table["dimensions"]] == ["region"]
    assert not table.get("time_dimensions")
    assert "private_segment" not in sql


@pytest.mark.parametrize("schema", [None, "DEST.AGENTS"])
def test_cross_account_metrics_are_rejected_even_with_destination_schema(project, schema):
    layer, store = project
    (store.root / "other.yaml").write_text(
        "title: Other account\n"
        "source: {type: snowflake, account: b, database: DB, schema: PUBLIC, username: u}\n"
        "metrics:\n"
        "  other_revenue: {table: ORDERS, expr: SUM(amount), dimensions: [{name: region}]}\n"
        "tiles: [{metric: other_revenue}]\n"
    )
    (store.root / "agents.yaml").write_text(
        AGENTS.replace("metrics: [revenue]", "metrics: [revenue, other_revenue]")
    )
    with pytest.raises(SemanticError, match="one Snowflake account"):
        build_cortex_agent(layer, store, "finance", schema=schema)
    (store.root / "agents.yaml").write_text(AGENTS)
    _, view, _, _ = build_cortex_agent(layer, store, "finance", schema=schema)
    assert len(view["tables"]) == 1


@pytest.mark.parametrize("schema", [None, "DEST.AGENTS"])
def test_unrestricted_export_rejects_verified_sql_from_another_account(project, schema):
    layer, store = project
    (store.root / "other.yaml").write_text(
        "title: Other account\n"
        "source: {type: snowflake, account: b, database: DB, schema: PUBLIC, username: u}\n"
        "tiles: [{title: Other revenue, sql: 'SELECT SUM(amount) FROM ORDERS'}]\n"
    )
    _, scoped_view, _, _ = build_cortex_agent(layer, store, "finance", schema=schema)
    assert not scoped_view.get("verified_queries")
    (store.root / "agents.yaml").write_text(
        AGENTS.replace("metrics: [revenue]", "metrics: []").replace(
            "dimensions: [region]", "dimensions: []"
        )
    )
    with pytest.raises(SemanticError, match="one Snowflake account"):
        build_cortex_agent(layer, store, "finance", schema=schema)
