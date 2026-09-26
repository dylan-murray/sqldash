import json
from contextlib import asynccontextmanager

import anyio
import pytest
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams
from pydantic import ValidationError
from typer.testing import CliRunner

from sqldash import mcp_server
from sqldash.cli import app
from sqldash.connectors.base import ConnectorError
from sqldash.connectors.engine import EngineConnector
from sqldash.lint import lint_project
from sqldash.mcp_server import create_mcp_server
from sqldash.models.agents import AgentsFile
from sqldash.project.store import DashboardStore, yaml
from sqldash.scaffold import create_demo
from sqldash.semantics import SemanticError, SemanticLayer
from sqldash.semantics.agents import (
    INSTRUCTIONS_WARN_CHARS,
    AgentLayer,
    findings_for_agent,
    parse_agents_file,
    render_prompt,
    select_option,
)
from sqldash.semantics.context import export_context

runner = CliRunner()

TOOL = {"description": "t", "sql": "SELECT 1 WHERE r = {{ region }}", "params": {"region": {}}}
AGENT = {"description": "d", "instructions": "i"}


@pytest.fixture(scope="module")
def demo_dir(tmp_path_factory):
    target = tmp_path_factory.mktemp("agents-demo")
    create_demo(target)
    return target


def _layer(root):
    store = DashboardStore(root)
    return AgentLayer(store, SemanticLayer(store))


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"tools": {"t": {"description": "x"}}}, "exactly one of 'queries' or 'sql'"),
        ({"tools": {"run_sql": {"description": "x", "sql": "SELECT 1"}}}, "built-in MCP tool"),
        ({"tools": {"t": {"description": "x", "sql": "SELECT {{ a }}"}}}, "undeclared param"),
        (
            {"tools": {"t": {"description": "x", "sql": "SELECT 1", "params": {"a": {}}}}},
            "never uses",
        ),
        ({"agents": {"fin": {**AGENT, "tools": ["nope"]}}}, "unknown tool 'nope'"),
        ({"agents": {"fin": {**AGENT, "metrics": ["a", "a"]}}}, "unique"),
        (
            {"tools": {"t": {"description": "x", "sql": "{% if a %}x{% endif %}"}}},
            "cannot use {% if %}",
        ),
        (
            {"tools": {"t": {"description": "x", "sql": "SELECT 1", "params": {}, "extra": 1}}},
            "extra",
        ),
    ],
)
def test_agents_file_rejects_bad_shapes(data, message):
    with pytest.raises(ValidationError, match=message):
        AgentsFile.model_validate(data)


def test_agents_file_accepts_a_bundle_with_param_refs():
    af = AgentsFile.model_validate(
        {
            "tools": {
                "t": {
                    "description": "x",
                    "params": {"region": {"type": "select", "options": ["us"]}},
                    "queries": [{"metric": "revenue", "filters": {"region": "{{ region }}"}}],
                }
            },
            "agents": {"fin": {**AGENT, "tools": ["t"]}},
        }
    )
    assert af.tools["t"].placeholders() == {"region"}
    assert af.tools["t"].required_params() == ["region"]


def test_parse_folds_errors_into_semantic_error():
    with pytest.raises(SemanticError, match=r"agents\.yaml: invalid YAML"):
        parse_agents_file("agents: [\n")
    with pytest.raises(SemanticError, match=r"agents\.yaml must be a YAML mapping"):
        parse_agents_file("- a\n")
    assert parse_agents_file("").agents == {}


def test_agents_yaml_is_not_a_dashboard(demo_dir):
    store = DashboardStore(demo_dir)
    assert "agents" not in store.discover()


def test_demo_agent_resolves_and_renders(demo_dir):
    agent = _layer(demo_dir).resolve("finance_analyst")
    assert [t.name for t in agent.tools] == ["revenue_health", "top_categories"]
    prompt = render_prompt(agent)
    assert prompt.startswith("# Finance analyst\n")
    assert "- `revenue` (Revenue)" in prompt
    assert "`cumulative_revenue`" not in prompt
    assert "Only group or filter by: region, category." in prompt
    assert "`revenue_health(region: select[us, eu, apac])`" in prompt
    assert "Do not write SQL." in prompt
    assert "how did revenue by region do vs the previous period?" in prompt


def test_unknown_agent_lists_the_available_ones(demo_dir):
    with pytest.raises(SemanticError, match="available agents: finance_analyst"):
        _layer(demo_dir).resolve("nope")


def test_check_references_reports_bad_pointers(tmp_path):
    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "agents.yaml").write_text(
        "tools:\n"
        "  bundle:\n"
        "    description: b\n"
        "    queries: [{metric: nope}, {metric: revenue, dimensions: [colour]}]\n"
        "agents:\n"
        "  a:\n"
        "    description: d\n"
        "    instructions: i\n"
        "    metrics: [revenue, ghost]\n"
        "    dimensions: [region, planet]\n"
        "    tools: [bundle]\n"
        "    sql: true\n"
    )
    errors, warnings = _layer(tmp_path).check_references()
    assert any("unknown metric 'nope'" in e for e in errors)
    assert any("no dimension 'colour'" in e for e in errors)
    assert any("unknown metric 'ghost'" in e for e in errors)
    assert any("dimension 'planet'" in e for e in errors)
    assert any("raw SQL" in w for w in warnings)
    assert any("no sample_questions" in w for w in warnings)


def test_check_references_warns_on_uses_and_empty_allow_list(tmp_path):
    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "agents.yaml").write_text(
        "agents:\n"
        "  a:\n"
        "    description: d\n"
        "    instructions: i\n"
        "    metrics: [ghost]\n"
        "    sample_questions: [q]\n"
        "    uses: [{server: linear, for: tickets}]\n"
    )
    errors, warnings = _layer(tmp_path).check_references()
    assert any("unknown metric 'ghost'" in e for e in errors)
    assert any("no metrics it can query" in w for w in warnings)
    assert any("host-side server 'linear'" in w for w in warnings)


def test_check_references_warns_on_long_instructions(tmp_path):
    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "agents.yaml").write_text(
        "agents:\n"
        "  a:\n"
        "    description: d\n"
        f"    instructions: {'x' * (INSTRUCTIONS_WARN_CHARS + 1)}\n"
        "    sample_questions: [q]\n"
    )
    _, warnings = _layer(tmp_path).check_references()
    assert any("instructions are" in w for w in warnings)


def test_strict_lint_probes_sql_tools(tmp_path):
    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "agents.yaml").write_text(
        "tools:\n"
        "  broken:\n"
        "    description: b\n"
        "    sql: SELECT nope FROM orders\n"
        "agents:\n"
        "  a:\n"
        "    description: d\n"
        "    instructions: i\n"
        "    tools: [broken]\n"
        "    sample_questions: [q]\n"
    )
    store = DashboardStore(tmp_path)
    layer = SemanticLayer(store)
    quiet = lint_project(store, layer)
    assert not any("SQL fails against the source" in f.message for f in quiet)
    findings = lint_project(store, layer, check_sql=True)
    assert any(f.level == "error" and "SQL fails against the source" in f.message for f in findings)


def test_strict_lint_names_the_warehouse_reason_a_sql_tool_fails(tmp_path, monkeypatch):
    """Snowflake puts the reason on a later line. The tool probe kept only the
    first, so the finding read `SQL compilation error:` and named no column."""
    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "agents.yaml").write_text(
        "tools:\n"
        "  broken:\n"
        "    description: b\n"
        "    sql: SELECT nope FROM orders\n"
        "agents:\n"
        "  a:\n"
        "    description: d\n"
        "    instructions: i\n"
        "    tools: [broken]\n"
        "    sample_questions: [q]\n"
    )
    real = EngineConnector.execute

    def snowflake_shaped(self, sql, bind, row_limit, cancel_token):
        if "nope" in sql:
            raise ConnectorError(
                "000904 (42000): SQL compilation error:\n"
                "error line 1 at position 7\n"
                "invalid identifier 'NOPE'"
            )
        return real(self, sql, bind, row_limit, cancel_token)

    monkeypatch.setattr(EngineConnector, "execute", snowflake_shaped)
    store = DashboardStore(tmp_path)
    findings = lint_project(store, SemanticLayer(store), check_sql=True)
    assert [f.message for f in findings if f.file == "agents.yaml"] == [
        "tool 'broken': SQL fails against the source: 000904 (42000): SQL compilation "
        "error: error line 1 at position 7 invalid identifier 'NOPE'"
    ]


def test_strict_lint_accepts_a_trailing_semicolon_on_tool_sql(tmp_path):
    """Hand-written tool SQL often ends with `;`. The probe wraps in a subquery,
    so a leftover semicolon was a parse error while run_tool still returned rows."""
    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "agents.yaml").write_text(
        "tools:\n"
        "  regions:\n"
        "    description: r\n"
        "    params: {region: {default: us}}\n"
        "    sql: SELECT region FROM orders WHERE region = {{ region }};\n"
        "agents:\n"
        "  a:\n"
        "    description: d\n"
        "    instructions: i\n"
        "    tools: [regions]\n"
        "    sample_questions: [q]\n"
    )
    store = DashboardStore(tmp_path)
    findings = lint_project(store, SemanticLayer(store), check_sql=True)
    assert not any("SQL fails against the source" in f.message for f in findings)


def test_strict_lint_skips_write_sql_tools(tmp_path):
    """DELETE is a legitimate tool body. Wrapping it in SELECT * FROM (...) is a
    parser error; executing it would write. --strict must do neither."""
    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "agents.yaml").write_text(
        "tools:\n"
        "  wipe:\n"
        "    description: w\n"
        "    params: {region: {default: us}}\n"
        "    sql: DELETE FROM orders WHERE region = {{ region }}\n"
        "  broken:\n"
        "    description: b\n"
        "    sql: SELECT nope FROM orders\n"
        "agents:\n"
        "  a:\n"
        "    description: d\n"
        "    instructions: i\n"
        "    tools: [wipe, broken]\n"
        "    sample_questions: [q]\n"
    )
    store = DashboardStore(tmp_path)
    findings = lint_project(store, SemanticLayer(store), check_sql=True)
    messages = [f.message for f in findings if f.file == "agents.yaml"]
    assert not any("Parser Error" in m or "syntax error" in m.lower() for m in messages)
    assert not any("wipe" in m and "SQL fails" in m for m in messages)
    assert any("broken" in m and "SQL fails against the source" in m for m in messages)


def test_strict_lint_reports_an_unreachable_source_once(tmp_path, monkeypatch):
    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "agents.yaml").write_text(
        "tools:\n"
        "  one:\n"
        "    description: a\n"
        "    sql: SELECT 1\n"
        "  two:\n"
        "    description: b\n"
        "    sql: SELECT 2\n"
        "agents:\n"
        "  a:\n"
        "    description: d\n"
        "    instructions: i\n"
        "    tools: [one, two]\n"
        "    sample_questions: [q]\n"
    )

    def boom(self, sql, bind, row_limit, cancel_token):
        raise ConnectorError("connection failed: could not connect to server")

    monkeypatch.setattr(EngineConnector, "execute", boom)
    store = DashboardStore(tmp_path)
    findings = lint_project(store, SemanticLayer(store), check_sql=True)
    probe = [
        f
        for f in findings
        if "cannot probe sql tools" in f.message or "SQL fails against the source" in f.message
    ]
    assert len(probe) == 1
    assert "cannot probe sql tools" in probe[0].message
    assert "connection failed" in probe[0].message


def _demo_with_source(tmp_path, source: str):
    create_demo(tmp_path)
    metrics = tmp_path / ".sqldash" / "metrics.yaml"
    text = metrics.read_text()
    authored = "source:\n  type: duckdb\n  attach_files: true\n"
    assert authored in text
    metrics.write_text(text.replace(authored, source))


@pytest.mark.parametrize(
    ("source", "reason"),
    [
        (
            "source:\n  type: postgres\n  host: h\n  database: d\n  username: u\n"
            '  password: "${env:SQLDASH_TEST_UNSET_PW}"\n',
            "SQLDASH_TEST_UNSET_PW",
        ),
        ("source:\n  type: duckdb\n  url: nosuchdialect://h/d\n", "nosuchdialect"),
    ],
    ids=["unset-env-var", "duckdb-url-unloadable-dialect"],
)
def test_strict_lint_reports_an_unresolvable_source_as_a_finding(
    tmp_path, monkeypatch, source, reason
):
    monkeypatch.delenv("SQLDASH_TEST_UNSET_PW", raising=False)
    _demo_with_source(tmp_path, source)

    result = runner.invoke(app, ["lint", "--strict", str(tmp_path)])

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "agents.yaml" in result.stdout
    assert "cannot probe sql tools" in result.stdout
    assert reason in result.stdout
    assert "file(s) checked" in result.stdout


def test_findings_for_agent_does_not_attribute_another_agents_allow_list(tmp_path):
    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "agents.yaml").write_text(
        "tools:\n"
        "  rev_tool:\n"
        "    description: r\n"
        "    queries: [{metric: revenue}]\n"
        "agents:\n"
        "  a:\n"
        "    description: d\n"
        "    instructions: i\n"
        "    metrics: [order_count]\n"
        "    tools: [rev_tool]\n"
        "    sample_questions: [q]\n"
        "  b:\n"
        "    description: d\n"
        "    instructions: i\n"
        "    metrics: [revenue]\n"
        "    tools: [rev_tool]\n"
        "    sample_questions: [q]\n"
        "  ab:\n"
        "    description: d\n"
        "    instructions: i\n"
        "    metrics: [ghost]\n"
        "    sample_questions: [q]\n"
    )
    layer = _layer(tmp_path)
    errors, warnings = layer.check_references()
    a_notes = findings_for_agent(layer.resolve("a"), errors, warnings)
    b_notes = findings_for_agent(layer.resolve("b"), errors, warnings)
    ab_notes = findings_for_agent(layer.resolve("ab"), errors, warnings)
    assert any("outside the agent's metrics allow-list" in m for _, m in a_notes)
    assert not any("agent 'a'" in m for _, m in b_notes)
    assert any("unknown metric 'ghost'" in m for _, m in ab_notes)
    assert not any("ghost" in m for _, m in a_notes)


def test_lint_project_covers_agents(tmp_path, demo_dir):
    store = DashboardStore(demo_dir)
    findings = lint_project(store, SemanticLayer(store))
    assert not [f for f in findings if f.file == "agents.yaml"]

    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "agents.yaml").write_text("agents:\n  a: {description: d}\n")
    store = DashboardStore(tmp_path)
    findings = lint_project(store, SemanticLayer(store))
    assert [f.level for f in findings if f.file == "agents.yaml"] == ["error"]
    assert "instructions" in next(f.message for f in findings if f.file == "agents.yaml")


def test_context_export_describes_agents(demo_dir):
    store = DashboardStore(demo_dir)
    text = export_context(SemanticLayer(store), store)
    assert "## Agents" in text
    assert "### `finance_analyst` — Finance analyst" in text
    assert "- tool `revenue_health(region: select[us, eu, apac])`" in text
    assert text.index("## Agents") < text.index("## Authoring sqldash YAML")


def test_cli_agent_list_and_show(demo_dir):
    result = runner.invoke(app, ["agent", "list", str(demo_dir), "--json"])
    assert result.exit_code == 0, result.output
    agents = json.loads(result.output)["agents"]
    assert [a["name"] for a in agents] == ["finance_analyst"]
    assert agents[0]["metrics"] == ["revenue", "order_count", "avg_order_value"]

    result = runner.invoke(app, ["agent", "show", "finance_analyst", "-t", str(demo_dir), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["prompt"].startswith("# Finance analyst")
    assert payload["tool_definitions"][1]["kind"] == "sql"
    assert payload["findings"] == []

    result = runner.invoke(
        app, ["agent", "show", "finance_analyst", "-t", str(demo_dir), "--prompt"]
    )
    assert result.exit_code == 0
    assert result.output.startswith("# Finance analyst\n")

    result = runner.invoke(app, ["agent", "show", "nope", "-t", str(demo_dir)])
    assert result.exit_code == 1
    assert "available agents" in result.output


def test_agent_show_prints_lint_findings(tmp_path):
    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "agents.yaml").write_text(
        "agents:\n"
        "  a:\n"
        "    description: d\n"
        "    instructions: i\n"
        "    metrics: [ghost]\n"
        "    sample_questions: [q]\n"
    )
    result = runner.invoke(app, ["agent", "show", "a", "-t", str(tmp_path)])
    assert result.exit_code == 1
    assert "unknown metric 'ghost'" in result.output
    payload = json.loads(
        runner.invoke(app, ["agent", "show", "a", "-t", str(tmp_path), "--json"]).output
    )
    assert payload["findings"]
    assert any("ghost" in f["message"] for f in payload["findings"])
    prompted = runner.invoke(app, ["agent", "show", "a", "-t", str(tmp_path), "--prompt"])
    assert prompted.exit_code == 1
    assert prompted.output.startswith("#")
    assert "unknown metric 'ghost'" in (prompted.stderr or prompted.output)


@asynccontextmanager
async def connected(server):
    low = server._lowlevel_server
    async with (
        create_client_server_memory_streams() as (client_streams, server_streams),
        anyio.create_task_group() as tg,
    ):
        tg.start_soon(lambda: low.run(*server_streams, low.create_initialization_options()))
        try:
            async with ClientSession(*client_streams) as client:
                await client.initialize()
                yield client
        finally:
            tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_mcp_serves_prompts_and_tools(demo_dir):
    server = create_mcp_server(demo_dir)
    async with connected(server) as client:
        prompts = (await client.list_prompts()).prompts
        assert [p.name for p in prompts] == ["finance_analyst"]
        assert prompts[0].arguments in (None, [])
        got = await client.get_prompt("finance_analyst", {})
        assert got.messages[0].content.text.startswith("# Finance analyst")

        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert tools["revenue_health"].input_schema["required"] == ["region"]
        assert (
            "Signature: revenue_health(region: select[us, eu, apac])"
            in tools["revenue_health"].description
        )

        result = await client.call_tool("revenue_health", {"region": "us"})
        payload = json.loads(result.content[0].text)
        assert [r["metric"] for r in payload["results"]] == [
            "revenue",
            "order_count",
            "avg_order_value",
        ]
        assert all(r["row_count"] == 1 and "compare" in r for r in payload["results"])

        result = await client.call_tool("top_categories", {"region": "eu"})
        payload = json.loads(result.content[0].text)
        assert payload["columns"][0]["name"] == "category"
        assert payload["row_count"] >= 1
        assert "?" in payload["sql"]
        assert "'eu'" not in payload["sql"]

        result = await client.call_tool("top_categories", {"region": "mars"})
        assert json.loads(result.content[0].text) == {
            "error": "top_categories: 'region' must be one of us, eu, apac"
        }
        result = await client.call_tool("top_categories", {})
        assert result.is_error


@pytest.mark.anyio
async def test_bundle_filter_with_an_explicit_op_and_a_list_is_refused(tmp_path):
    """#668: an author-written `{op: "!=", value: [...]}` in a bundle compiled to IN,
    so the tool answered with the regions it was written to exclude."""
    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "agents.yaml").write_text(
        "tools:\n"
        "  outside_na:\n"
        "    description: r\n"
        "    queries:\n"
        "      - {metric: revenue, dimensions: [region],\n"
        "         filters: {region: {op: '!=', value: [us]}}}\n"
        "  listed:\n"
        "    description: r\n"
        "    queries: [{metric: revenue, dimensions: [region], filters: {region: [eu, us]}}]\n"
    )
    async with connected(create_mcp_server(tmp_path)) as client:
        refused = json.loads((await client.call_tool("outside_na", {})).content[0].text)
        assert "op '!=' with a list value" in refused.get("error", ""), refused
        listed = json.loads((await client.call_tool("listed", {})).content[0].text)
        rows = listed["results"][0]["rows"]
        assert sorted(r[0] for r in rows) == ["eu", "us"], listed


@pytest.mark.anyio
async def test_broken_agents_file_keeps_the_metrics_surface(tmp_path):
    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "agents.yaml").write_text("agents: [\n")
    server = create_mcp_server(tmp_path)
    async with connected(server) as client:
        assert (await client.list_prompts()).prompts == []
        assert "query_metric" in {t.name for t in (await client.list_tools()).tools}


@pytest.mark.anyio
@pytest.mark.parametrize("name", ["class", "from", "None", "_x", "model_config"])
async def test_tool_param_named_like_a_python_reserved_word_is_served(tmp_path, name):
    """A param is any identifier, but mcp derives the argument model from a Python
    signature: `class` is not a valid parameter, `_x` is refused by mcp and
    `model_config` by pydantic. `sqldash mcp` exited 1 before answering initialize,
    while lint stayed green (#514). The MCP name is the declared name."""
    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "agents.yaml").write_text(
        "tools:\n"
        "  pick:\n"
        "    description: p\n"
        "    params:\n"
        f"      {name}: {{description: the value}}\n"
        "      floor: {type: number, default: 2}\n"
        f"    sql: SELECT {{{{ {name} }}}} AS picked, {{{{ floor }}}} AS floor\n"
        "agents:\n"
        "  a:\n"
        "    description: d\n"
        "    instructions: i\n"
        "    tools: [pick]\n"
        "    sample_questions: [q]\n"
    )
    store = DashboardStore(tmp_path)
    assert not [f for f in lint_project(store, SemanticLayer(store)) if f.level == "error"]
    server = create_mcp_server(tmp_path)
    async with connected(server) as client:
        assert [p.name for p in (await client.list_prompts()).prompts] == ["a"]
        tools = {t.name: t for t in (await client.list_tools()).tools}
        schema = tools["pick"].input_schema
        assert list(schema["properties"]) == [name, "floor"]
        assert schema["required"] == [name]
        assert schema["properties"]["floor"]["type"] == "number"
        assert f"Signature: pick({name}: text, floor: number = 2)" in tools["pick"].description

        result = await client.call_tool("pick", {name: "hi", "floor": 5})
        payload = json.loads(result.content[0].text)
        assert payload["rows"] == [["hi", 5.0]]

        defaulted = json.loads((await client.call_tool("pick", {name: "yo"})).content[0].text)
        assert defaulted["rows"] == [["yo", 2]]

        stand_in = await client.call_tool("pick", {"p0": "hi"})
        assert json.loads(stand_in.content[0].text) == {
            "error": f"unknown argument 'p0' for pick — valid arguments: {name}, floor"
        }
        assert (await client.call_tool("pick", {})).is_error


@pytest.mark.anyio
async def test_select_with_numeric_options_is_callable_both_ways(tmp_path):
    """tools/list advertised a select with options [1, 2, 3, 4] as `type: string`
    with the numeric default 1; pydantic refused `1` and the options check
    refused `"1"`, so only omitting the argument worked (#599)."""
    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "agents.yaml").write_text(
        "tools:\n"
        "  numeric_select:\n"
        "    description: n\n"
        "    params:\n"
        "      quarter: {type: select, options: [1, 2, 3, 4], default: 1}\n"
        "      region: {type: select, options: [us, eu]}\n"
        "    sql: SELECT {{ quarter }} AS q, {{ region }} AS r\n"
        "agents:\n"
        "  a:\n"
        "    description: d\n"
        "    instructions: i\n"
        "    tools: [numeric_select]\n"
        "    sample_questions: [q]\n"
    )
    server = create_mcp_server(tmp_path)
    async with connected(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        properties = tools["numeric_select"].input_schema["properties"]
        assert properties["quarter"]["type"] == "integer"
        assert properties["quarter"]["enum"] == [1, 2, 3, 4]
        assert properties["quarter"]["default"] == 1
        assert properties["region"]["type"] == "string"
        assert properties["region"]["enum"] == ["us", "eu"]

        async def verdict(arguments):
            result = await client.call_tool("numeric_select", arguments)
            assert not result.is_error, (arguments, result.content)
            return json.loads(result.content[0].text)

        for quarter, expected in [(1, 1), ("1", 1), (2, 2), ("3", 3)]:
            payload = await verdict({"quarter": quarter, "region": "eu"})
            assert payload["rows"] == [[expected, "eu"]], quarter
        assert (await verdict({"region": "us"}))["rows"] == [[1, "us"]]
        for arguments in [{"quarter": 5, "region": "us"}, {"quarter": "x", "region": "us"}]:
            assert await verdict(arguments) == {
                "error": "numeric_select: 'quarter' must be one of 1, 2, 3, 4"
            }
        assert await verdict({"quarter": 1, "region": 3}) == {
            "error": "numeric_select: 'region' must be one of us, eu"
        }


def test_select_option_binds_the_declared_option():
    missing = select_option([], "x")
    assert select_option([1, 2], "2") == 2
    assert select_option([1.5, 2.0], 2) == 2.0
    assert select_option([1.5, 2.0], "1.5") == 1.5
    assert select_option(["1", "2"], 1) == "1"
    assert type(select_option(["1", 1], "1")) is str
    assert type(select_option(["1", 1], 1)) is int
    assert select_option([1, 2], True) is missing
    assert select_option([True, False], 1) is missing
    assert select_option([1, 2], "nan") is missing
    assert select_option([1, 2, 3, 4], "1.0") is missing
    assert select_option([1000, 2000], "1e3") is missing
    assert select_option([1, 2], " 1") is missing
    assert select_option([1, 2], "-1") is missing
    assert select_option([-1, 2], "-1") == -1
    assert select_option([1.5, 2.0], "2.0") == 2.0
    assert select_option([1.5, 2.0], "2") == 2.0
    big, neighbour = 1234567890123456789, 1234567890123456768
    assert select_option([big], str(big)) == big
    assert select_option([neighbour, big], str(big)) == big


def _yaml_options(text):
    return yaml.load(f"options: {text}")["options"]


@pytest.mark.parametrize(
    ("text", "schema"),
    [
        pytest.param("['us', 'eu']", {"enum": ["us", "eu"], "type": "string"}, id="single-quoted"),
        pytest.param('["us", "eu"]', {"enum": ["us", "eu"], "type": "string"}, id="double-quoted"),
        pytest.param("[us, eu]", {"enum": ["us", "eu"], "type": "string"}, id="plain"),
        pytest.param("[1, 2]", {"enum": [1, 2], "type": "integer"}, id="integers"),
    ],
)
def test_a_quoted_yaml_select_still_advertises_its_options(text, schema):
    """ruamel loads a quoted scalar as a str subclass; an exact-type lookup
    dropped the enum and type for every quoted select."""
    advertised = mcp_server._options_schema(_yaml_options(text))
    assert advertised == schema
    assert [type(v) for v in advertised["enum"]] == [type(v) for v in schema["enum"]]


def test_a_quoted_yaml_option_binds_by_its_real_type():
    """With `[1, '1']` from YAML, "1" names the quoted option and 1 the integer,
    in either order; an exact-type check made that depend on the order."""
    for text in ["[1, '1']", "['1', 1]"]:
        options = _yaml_options(text)
        assert type(select_option(options, "1")) is not int, text
        assert select_option(options, "1") == "1"
        assert type(select_option(options, 1)) is int, text


@pytest.mark.anyio
async def test_one_unbuildable_tool_or_prompt_keeps_the_rest_served(tmp_path, monkeypatch, capsys):
    """Only a whole broken agents.yaml was skipped; a single tool or prompt the
    framework refused to build raised out of server construction and took every
    metric, dashboard and agent down with it (#514)."""
    create_demo(tmp_path)
    build_tool = mcp_server._agent_tool
    build_prompt = mcp_server.Prompt.from_function

    def refuse_tool(tool, registry, row_limit):
        if tool.name == "top_categories":
            raise ValueError("refused tool")
        return build_tool(tool, registry, row_limit)

    def refuse_prompt(fn, name=None, **kwargs):
        if name == "finance_analyst":
            raise ValueError("refused prompt")
        return build_prompt(fn, name=name, **kwargs)

    monkeypatch.setattr(mcp_server, "_agent_tool", refuse_tool)
    monkeypatch.setattr(mcp_server.Prompt, "from_function", refuse_prompt)
    server = create_mcp_server(tmp_path)
    err = capsys.readouterr().err
    assert "skipped tool 'top_categories' from agents.yaml: refused tool" in err
    assert "skipped agent 'finance_analyst' from agents.yaml: refused prompt" in err
    async with connected(server) as client:
        assert (await client.list_prompts()).prompts == []
        names = {t.name for t in (await client.list_tools()).tools}
        assert {"query_metric", "list_metrics", "revenue_health"} <= names
        assert "top_categories" not in names
        result = await client.call_tool("revenue_health", {"region": "us"})
        assert not result.is_error


@pytest.mark.anyio
async def test_number_tool_argument_is_exact_and_finite(tmp_path):
    """A `float` annotation let min_amount="nan" match no rows and "-inf" every row,
    and rounded 9007199254740993 to ...992 before the tool saw it."""
    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "agents.yaml").write_text(
        "tools:\n"
        "  big:\n"
        "    description: b\n"
        "    params:\n"
        "      min_amount: {type: number}\n"
        "    sql: SELECT {{ min_amount }} AS v\n"
        "agents:\n"
        "  a:\n"
        "    description: d\n"
        "    instructions: i\n"
        "    tools: [big]\n"
        "    sample_questions: [q]\n"
    )
    server = create_mcp_server(tmp_path)
    async with connected(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert tools["big"].input_schema["properties"]["min_amount"]["type"] == "number"
        for given, bound in [
            (9007199254740993, 9007199254740993),
            ("9007199254740993", 9007199254740993),
            ("12.5", 12.5),
            (3, 3),
        ]:
            result = await client.call_tool("big", {"min_amount": given})
            assert json.loads(result.content[0].text)["rows"] == [[bound]]
        for given in ["nan", "-inf", "inf", "abc"]:
            result = await client.call_tool("big", {"min_amount": given})
            assert json.loads(result.content[0].text) == {
                "error": f"big: 'min_amount' must be a finite number, got {given!r}"
            }
