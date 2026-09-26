import json
import os
import stat
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import pytest
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams
from pydantic import ValidationError
from typer.testing import CliRunner

from sqldash.cli import app
from sqldash.execution import ExecutionRegistry
from sqldash.mcp_server import create_mcp_server, registry_for
from sqldash.models.agents import EvalCase, ExpectSpec
from sqldash.project.store import DashboardStore
from sqldash.scaffold import create_demo
from sqldash.semantics import SemanticLayer
from sqldash.semantics.agents import AgentLayer, WorkspaceAgentLayer
from sqldash.semantics.evals import (
    PROMPT_ENV,
    TRACE_ENV,
    Figure,
    TraceEvent,
    args_match,
    grade,
    ground_truth,
    numbers_in,
    read_trace,
    run_case,
)

runner = CliRunner()


@pytest.fixture(scope="module")
def demo_dir(tmp_path_factory):
    target = tmp_path_factory.mktemp("evals-demo")
    create_demo(target)
    return target


@pytest.fixture(scope="module")
def registry():
    reg = ExecutionRegistry(max_workers=2)
    yield reg
    reg.shutdown()


def _agent(root, name="finance_analyst"):
    store = DashboardStore(root)
    return AgentLayer(store, SemanticLayer(store)).resolve(name)


@pytest.fixture
def workspace_agent(demo_dir):
    store = DashboardStore(demo_dir)
    layer = WorkspaceAgentLayer({"acme": AgentLayer(store, SemanticLayer(store))})
    assert layer.check_references()[0] == []
    return layer.resolve("acme/finance_analyst")


def test_workspace_custom_tool_ground_truth(workspace_agent, demo_dir, registry):
    expect = workspace_agent.definition.evals[1].expect
    truth = ground_truth(workspace_agent, expect, registry, 100)
    local = ground_truth(_agent(demo_dir), expect, registry, 100)
    assert truth["results"] == local["results"]


@pytest.mark.parametrize(
    ("tool", "is_error", "passed"),
    [
        ("acme__revenue_health", False, True),
        ("other__revenue_health", False, False),
        ("revenue_health", False, False),
        ("acme__revenue_health", True, False),
    ],
)
def test_workspace_custom_tool_trace(workspace_agent, tool, is_error, passed):
    case = workspace_agent.definition.evals[1]
    truth = {"results": [{"rows": [[1234.5]]}]}
    result = grade(
        workspace_agent,
        case,
        "eu revenue: 1234.5",
        truth=truth,
        events=[TraceEvent(tool, {"region": "eu"}, truth, is_error)],
    )
    assert result.passed is passed, result.failures
    if not passed:
        assert result.failures[0].startswith("expected a call to acme__revenue_health(")


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"question": "q"}, "grades nothing"),
        ({"question": "q", "expect": {}}, "exactly one of 'tool' or 'refuses"),
        ({"question": "q", "expect": {"tool": "x", "refuses": True}}, "exactly one"),
        ({"question": "q", "expect": {"refuses": True, "args": {"a": 1}}}, "cannot carry args"),
    ],
)
def test_eval_case_shapes(data, message):
    with pytest.raises(ValidationError, match=message):
        EvalCase.model_validate(data)


def test_static_eval_checks(tmp_path):
    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "agents.yaml").write_text(
        "tools:\n"
        "  t:\n"
        "    description: t\n"
        "    params: {region: {}}\n"
        "    sql: SELECT {{ region }}\n"
        "agents:\n"
        "  a:\n"
        "    description: d\n"
        "    instructions: i\n"
        "    metrics: [revenue]\n"
        "    tools: [t]\n"
        "    sample_questions: [q]\n"
        "    evals:\n"
        "      - {question: q1, expect: {tool: nope}}\n"
        "      - {question: q2, expect: {tool: t, args: {planet: mars}}}\n"
        "      - {question: q3, expect: {tool: query_metric, args: {name: order_count}}}\n"
        "      - {question: q4, expect: {tool: query_metric, args: {dimensions: [colour]}}}\n"
        "      - {question: q5, expect: {tool: run_sql}, answer_has: [orders]}\n"
        "      - {question: q6, expect: {tool: t, args: {region: us}}}\n"
        "      - {question: q7, expect: {tool: query_metric,"
        " args: {name: revenue, compare: yoy}}}\n"
    )
    store = DashboardStore(tmp_path)
    errors, _ = AgentLayer(store, SemanticLayer(store)).check_references()
    messages = "\n".join(errors)
    assert "eval 1 expects unknown tool 'nope'" in messages
    assert "eval 2 passes 'planet' to t" in messages
    assert "eval 3 expects query_metric on 'order_count', outside" in messages
    assert (
        "eval 4 expects dimension 'colour', which any allowed metric does not declare" in messages
    )
    assert "eval 5 expects run_sql but the agent has sql: false" in messages
    assert "eval 6" not in messages
    assert "eval 7 expects a compare without start and end" in messages
    path = tmp_path / ".sqldash" / "agents.yaml"
    path.write_text(
        path.read_text() + "      - {question: q8, expect: {tool: query_metric,"
        " args: {name: revenue, dashboard: demo, compare: yoy}}}\n"
    )
    errors, _ = AgentLayer(store, SemanticLayer(store)).check_references()
    assert not any("eval 8" in e for e in errors)


def test_args_match_is_a_subset_with_unordered_lists():
    assert args_match({"name": "revenue"}, {"name": "revenue", "grain": "day"})
    assert args_match({"dimensions": ["region"]}, {"dimensions": ["category", "region"]})
    assert args_match({"filters": {"region": "us"}}, {"filters": {"region": "us", "x": 1}})
    assert not args_match({"name": "revenue"}, {"name": "orders"})
    assert not args_match({"dimensions": ["region"]}, {"dimensions": ["category"]})
    assert args_match({"limit": 5}, {"limit": 5.0})


def test_ground_truth_runs_the_expected_call(demo_dir, registry):
    agent = _agent(demo_dir)
    truth = ground_truth(agent, agent.definition.evals[0].expect, registry, 100)
    assert [row[0] for row in truth["rows"]] == ["apac", "eu", "us"]
    assert truth["compare"]["mode"] == "previous_period"
    truth = ground_truth(agent, agent.definition.evals[1].expect, registry, 100)
    assert [r["metric"] for r in truth["results"]] == ["revenue", "order_count", "avg_order_value"]
    assert ground_truth(agent, ExpectSpec(tool="list_metrics"), registry, 100) is None
    with pytest.raises(ValueError, match="no tool 'ghost'"):
        ground_truth(agent, ExpectSpec(tool="ghost"), registry, 100)


def test_ground_truth_scopes_to_the_dashboard_like_the_mcp_tool(demo_dir, registry):
    from sqldash.semantics.bind import bind_metric

    agent = _agent(demo_dir)
    scoped = ground_truth(
        agent,
        ExpectSpec(tool="query_metric", args={"name": "revenue", "dashboard": "demo"}),
        registry,
        100,
    )
    dash, _, _ = agent.layer.store.load("demo")
    like_mcp = registry.run_bound(
        bind_metric(agent.layer, "revenue", scope="demo", dash=dash, params={}), 100
    ).rows
    unscoped = ground_truth(
        agent, ExpectSpec(tool="query_metric", args={"name": "revenue"}), registry, 100
    )
    assert scoped["rows"] == like_mcp
    assert scoped["rows"] != unscoped["rows"]


def test_ground_truth_refuses_bad_args_with_a_verdict(demo_dir, registry, tmp_path):
    agent = _agent(demo_dir)
    with pytest.raises(ValueError, match=r"needs args\.name"):
        ground_truth(
            agent, ExpectSpec(tool="query_metric", args={"dimensions": ["region"]}), registry, 100
        )
    with pytest.raises(ValueError, match="limit must be a number"):
        ground_truth(
            agent,
            ExpectSpec(tool="query_metric", args={"name": "revenue", "limit": [1]}),
            registry,
            100,
        )
    with pytest.raises(ValueError, match="limit must be positive"):
        ground_truth(
            agent,
            ExpectSpec(tool="query_metric", args={"name": "revenue", "limit": 0}),
            registry,
            100,
        )
    case = EvalCase(
        question="q", expect=ExpectSpec(tool="query_metric", args={"dimensions": ["region"]})
    )
    host = _write_runner(tmp_path / "h.py", "print('x')")
    result = run_case(agent, case, host, registry, timeout=60)
    assert result.failures == [
        "cannot compute the expected result: a query_metric eval needs args.name"
    ]


@pytest.mark.parametrize("broken", [False, True])
def test_cli_eval_dashboard_errors_are_case_verdicts(tmp_path, broken):
    create_demo(tmp_path)
    root = tmp_path / ".sqldash"
    if broken:
        (root / "bad.yaml").write_text("title: [broken\n")
    path = root / "agents.yaml"
    path.write_text(
        path.read_text()
        + "      - question: bad dashboard\n"
        + "        expect: {tool: query_metric, args: {name: revenue, dashboard: bad}}\n"
        + "      - {question: refuse afterwards, expect: {refuses: true}}\n"
    )
    host = _write_runner(tmp_path / "host.py", 'print("I cannot do that.")')
    result = runner.invoke(
        app,
        ["agent", "eval", "finance_analyst", "-t", str(tmp_path), "--runner", host, "--json"],
    )
    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["passed"] is False
    failed = payload["cases"][3]
    assert failed["passed"] is False
    assert failed["failures"][0].startswith("cannot compute the expected result:")
    assert ("invalid YAML" if broken else "no dashboard named 'bad'") in failed["failures"][0]
    assert payload["cases"][4]["passed"] is True


def test_static_check_flags_a_query_metric_eval_without_a_name(tmp_path):
    create_demo(tmp_path)
    path = tmp_path / ".sqldash" / "agents.yaml"
    path.write_text(
        path.read_text()
        + "      - {question: q, expect: {tool: query_metric, args: {dimensions: [region]}}}\n"
    )
    store = DashboardStore(tmp_path)
    errors, _ = AgentLayer(store, SemanticLayer(store)).check_references()
    assert any("eval 4 expects query_metric without args.name" in e for e in errors)


def test_errors_for_agent_is_repo_aware(demo_dir):
    from dataclasses import replace

    from sqldash.semantics.agents import errors_for_agent

    agent = _agent(demo_dir)
    messages = [
        "agent 'finance_analyst' eval 1 expects unknown tool 'nope'",
        "agent 'finance_analyst' allows unknown metric 'ghost'",
        "agent 'finance_analyst_2' eval 1 expects unknown tool 'nope'",
        "tool 'x' queries unknown metric 'y'",
    ]
    assert errors_for_agent(agent, messages) == messages[:2]
    scoped = replace(agent, name="acme/finance_analyst", repo="acme")
    workspace = [
        "acme: agent 'finance_analyst' eval 4 expects unknown tool 'nope'",
        "other: agent 'finance_analyst' eval 4 expects unknown tool 'nope'",
    ]
    assert errors_for_agent(scoped, workspace) == workspace[:1]


def test_figures_survive_a_sentence_final_period():
    from sqldash.semantics.evals import numbers_in_text

    assert numbers_in_text("Revenue was $95,282.") == {Figure(95282.0)}
    assert numbers_in_text("The count is 1,204.") == {Figure(1204.0)}
    assert numbers_in_text("us $106,092. eu $82,630.") == {Figure(106092.0), Figure(82630.0)}
    assert numbers_in_text("up 26.1%.") == {Figure(26.1, percent=True)}
    assert numbers_in_text("version 1.2.3 and 3 regions") == set()


@pytest.mark.parametrize(
    ("text", "value", "scale"),
    [
        ("the table holds 6.2M rows", 6_200_000, 1e6),
        ("US revenue was $107.9K.", 107_900, 1e3),
        ("about 1.5k orders", 1_500, 1e3),
        ("a 3B market", 3_000_000_000, 1e9),
        ("burned $1.2bn", 1_200_000_000, 1e9),
        ("worth 2T", 2_000_000_000_000, 1e12),
        ("6 million rows", 6_000_000, 1e6),
        ("12 Thousand orders", 12_000, 1e3),
        ("down -$5.5M", -5_500_000, 1e6),
        ("EU €74.9K", 74_900, 1e3),
        ("1.2e3 orders", 1_200, 1e3),
        ("6.2e+06", 6_200_000, 1e6),
    ],
)
def test_a_magnitude_suffix_is_part_of_the_figure(text, value, scale):
    from sqldash.semantics.evals import numbers_in_text

    assert numbers_in_text(text) == {Figure(value, scale=scale)}


def test_suffix_parsing_does_not_invent_figures():
    from sqldash.semantics.evals import numbers_in_text

    assert numbers_in_text("grew 2.5x, not 3x") == {Figure(2.5)}
    assert numbers_in_text("12 millionaire") == {Figure(12.0)}
    for prose in (
        "I am gpt-4.1 on llama-3-70B",
        "ticket SKU-380, build v1.2.3b",
        "a 6.2Mb file, 0x1F, 4x4, 3 regions",
    ):
        assert numbers_in_text(prose) == set(), prose


def test_grade_reads_figures_before_a_period(demo_dir):
    agent = _agent(demo_dir)
    case = agent.definition.evals[0]
    good = grade(agent, case, "apac $0. eu $154,719. us $95,282.", truth=TRUTH)
    assert good.passed, good.failures
    assert good.warnings == []
    refuse = agent.definition.evals[2]
    assert not grade(agent, refuse, "I cannot do that. It would affect 1,204.").passed


@pytest.mark.parametrize("figure", ["6.2M", "6 million", "$1.2bn", "1.2e3", "6200000"])
def test_a_refusal_that_recites_an_abbreviated_figure_fails(demo_dir, figure):
    agent = _agent(demo_dir)
    refuse = agent.definition.evals[2]
    graded = grade(agent, refuse, f"No, the orders table holds {figure} rows; I won't drop it.")
    assert not graded.passed
    assert graded.failures[0].startswith("expected a refusal but the answer reports figures: ")


def test_abbreviated_answers_are_graded_like_spelled_out_ones(demo_dir):
    agent = _agent(demo_dir)
    case = agent.definition.evals[0]
    short = grade(agent, case, "us $95.3K, eu $154.7K, apac flat.", truth=TRUTH)
    assert short.passed, short.failures
    assert short.warnings == []
    off = grade(agent, case, "us $96.3K, eu $150K, apac flat.", truth=TRUTH)
    assert off.failures[0].startswith("answer reports none of the figures query_metric")
    padded = grade(agent, case, "us $95,282, eu $154,719, apac flat; ads cost $1.2M.", truth=TRUTH)
    assert padded.passed, padded.failures
    assert padded.warnings == ["figures in the answer that no result contains: 1.2e+06"]


@pytest.mark.parametrize("answer", ["us 1.5e-400, eu 1e999", "us 1.5e-99, eu 1e99"])
def test_an_extreme_exponent_cannot_break_grading(demo_dir, answer):
    agent = _agent(demo_dir)
    graded = grade(agent, agent.definition.evals[0], f"{answer}, apac flat", truth=TRUTH)
    assert graded.failures[0].startswith("answer reports none of the figures query_metric")


def test_static_check_flags_an_unknown_dashboard(tmp_path):
    create_demo(tmp_path)
    path = tmp_path / ".sqldash" / "agents.yaml"
    path.write_text(
        path.read_text() + "      - {question: q, expect: {tool: query_metric,"
        " args: {name: revenue, dashboard: bad, compare: yoy}}}\n"
        + "      - {question: q, expect: {tool: query_metric,"
        " args: {name: revenue, dashboard: demo}}}\n"
    )
    store = DashboardStore(tmp_path)
    errors, _ = AgentLayer(store, SemanticLayer(store)).check_references()
    assert any("eval 4 scopes to unknown dashboard 'bad'" in e for e in errors)
    assert not any("eval 5" in e for e in errors)
    errors, _ = AgentLayer(store, SemanticLayer(store)).check_references("acme")
    assert any("eval 4 scopes to unknown dashboard 'bad'" in e for e in errors)


def test_exact_values_are_sourced_at_any_precision():
    from sqldash.semantics.evals import sourced

    assert sourced(0.261, {0.261})
    assert sourced(0.26, {0.261})
    assert sourced(95281.6, {95281.6})
    assert not sourced(0.262, {0.261})


def test_an_abbreviated_figure_is_rounded_in_its_own_unit():
    from sqldash.semantics.evals import sourced

    assert sourced(107_900, {107_889.79}, scale=1e3)
    assert sourced(6_200_000, {6_213_456}, scale=1e6)
    assert sourced(6_200_000, {6_200_000})
    assert not sourced(107_800, {107_889.79}, scale=1e3)
    assert not sourced(107_900, {107_889.79})


def test_static_check_uses_the_named_metrics_dimensions(tmp_path):
    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "metrics.yaml").write_text(
        "source: {type: duckdb, attach_files: true}\n"
        "relations: {orders: {table: orders}}\n"
        "metrics:\n"
        "  revenue: {relation: orders, expr: SUM(amount), dimensions: [{name: region}]}\n"
        "  order_count: {relation: orders, expr: COUNT(*), dimensions: [{name: category}]}\n"
    )
    (tmp_path / ".sqldash" / "agents.yaml").write_text(
        "agents:\n"
        "  a:\n"
        "    description: d\n"
        "    instructions: i\n"
        "    sample_questions: [q]\n"
        "    evals:\n"
        "      - {question: q, expect: {tool: query_metric,"
        " args: {name: revenue, dimensions: [category]}}}\n"
    )
    store = DashboardStore(tmp_path)
    errors, _ = AgentLayer(store, SemanticLayer(store)).check_references()
    assert any(
        "eval 1 expects dimension 'category', which metric 'revenue' does not declare" in e
        for e in errors
    )


def test_read_trace_treats_a_non_object_line_as_unreadable(tmp_path):
    trace = tmp_path / "t.jsonl"
    trace.write_text('{"tool": "x", "arguments": {}}\n[1, 2, 3]\n"str"\n')
    events = read_trace(trace)
    assert [e.tool for e in events] == ["x", "?", "?"]
    assert all(e.is_error for e in events[1:])


def test_an_expected_result_without_figures_cannot_pass_on_nothing(demo_dir):
    agent = _agent(demo_dir)
    case = EvalCase(
        question="q",
        expect=ExpectSpec(
            tool="query_metric", args={"name": "revenue", "filters": {"region": "asia"}}
        ),
    )
    graded = grade(agent, case, "I have no idea", truth={"rows": [[None]]})
    assert not graded.passed
    assert graded.failures[0].startswith("query_metric returns no figures for these args")
    with_text = EvalCase.model_construct(question="q", expect=case.expect, answer_has=["no orders"])
    assert grade(agent, with_text, "there were no orders in asia", truth={"rows": [[None]]}).passed


def test_static_check_requires_a_tools_required_params(tmp_path):
    create_demo(tmp_path)
    path = tmp_path / ".sqldash" / "agents.yaml"
    path.write_text(path.read_text() + "      - {question: q, expect: {tool: revenue_health}}\n")
    store = DashboardStore(tmp_path)
    errors, _ = AgentLayer(store, SemanticLayer(store)).check_references()
    assert any("eval 4 calls revenue_health without required param(s): region" in e for e in errors)


@pytest.mark.anyio
async def test_an_unwritable_trace_does_not_break_the_call(demo_dir, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    server = create_mcp_server(demo_dir, trace=blocker / "trace.jsonl")
    async with connected(server) as client:
        result = await client.call_tool("list_metrics", {})
        assert not result.is_error
        assert "metrics" in json.loads(result.content[0].text)


def test_percent_scaling_needs_a_percent_sign():
    from sqldash.semantics.evals import numbers_in_text, sourced

    assert numbers_in_text("up 26.1% to 50") == {Figure(26.1, percent=True), Figure(50.0)}
    assert sourced(26.1, {0.261}, percent=True)
    assert not sourced(50, {0.5})
    assert sourced(50, {0.5}, percent=True)


def test_read_trace_survives_a_corrupt_line(tmp_path):
    trace = tmp_path / "t.jsonl"
    trace.write_text('{"tool": "list_metrics", "arguments": {}}\n{not json\n')
    events = read_trace(trace)
    assert [e.tool for e in events] == ["list_metrics", "?"]
    assert events[1].is_error
    assert events[1].result["error"].startswith("unreadable trace line")


@pytest.mark.parametrize("tool", ["get_schema", "run_sql"])
def test_tool_without_ground_truth_needs_answer_has(tool):
    with pytest.raises(ValidationError, match="grades nothing on its own"):
        EvalCase(question="q", expect=ExpectSpec(tool=tool))
    EvalCase(question="q", expect=ExpectSpec(tool=tool), answer_has=["orders"])


@pytest.mark.parametrize("answer", ["I cannot help with that.", "The count is 1,204."])
def test_run_sql_without_answer_has_cannot_pass_without_a_trace(demo_dir, answer):
    agent = _agent(demo_dir)
    agent.definition.sql = True
    case = EvalCase.model_construct(
        question="count orders",
        expect=ExpectSpec(tool="run_sql", args={"sql": "SELECT COUNT(*) FROM orders"}),
        answer_has=[],
    )
    result = grade(agent, case, answer)
    assert not result.passed
    assert result.failures == [
        "run_sql has no computed ground truth and no trace was produced, "
        "so there is nothing to grade; add answer_has"
    ]


def _event(tool, arguments, result=None, is_error=False):
    return TraceEvent(tool, arguments, result, is_error)


TRUTH = {
    "rows": [["us", 95281.6], ["eu", 154719.34]],
    "compare": {"rows": [["us", 80000.0], ["eu", 120000.0]], "delta": 0.19},
}


def test_grade_from_the_answer_alone(demo_dir):
    agent = _agent(demo_dir)
    case = agent.definition.evals[0]
    good = grade(agent, case, "us $95,282 and eu 154,719.34; apac flat", truth=TRUTH)
    assert good.passed, good.failures
    assert good.evidence == "answer"
    assert good.warnings == []

    wrong = grade(agent, case, "us is up 12% to 4,200; eu and apac flat", truth=TRUTH)
    assert not wrong.passed
    assert wrong.failures[0].startswith("answer reports none of the figures query_metric")
    assert wrong.warnings == ["figures in the answer that no result contains: 12, 4200"]

    missing_text = grade(agent, case, "revenue was 95,282", truth=TRUTH)
    assert "answer does not contain 'eu'" in missing_text.failures

    previous_only = grade(agent, case, "us 80,000 and eu 120,000, apac flat", truth=TRUTH)
    assert previous_only.failures == [
        "answer reports none of the figures query_metric actually returns (e.g. 95281.6, 154719)"
    ]
    assert previous_only.warnings == []


@pytest.mark.parametrize("workspace", [False, True])
def test_grade_with_a_trace_checks_the_call_too(demo_dir, workspace_agent, workspace):
    agent = workspace_agent if workspace else _agent(demo_dir)
    case = agent.definition.evals[0]
    called = [
        _event(
            "query_metric",
            {
                "name": "revenue",
                "dimensions": ["region"],
                "start": "-30d",
                "end": "today",
                "compare": "previous_period",
            },
            TRUTH,
        )
    ]
    good = grade(agent, case, "us 95,282 eu 154,719 apac 0.0", truth=TRUTH, events=called)
    assert good.passed, good.failures
    assert good.evidence == "trace+answer"

    other = grade(
        agent, case, "us 95,282 eu apac", truth=TRUTH, events=[_event("list_metrics", {})]
    )
    assert other.failures == [
        'expected a call to query_metric({"name": "revenue", "dimensions": ["region"], '
        '"start": "-30d", "end": "today", "compare": "previous_period"}); saw list_metrics({})'
    ]
    errored = [_event("query_metric", called[0].arguments, {"error": "x"}, is_error=True)]
    assert not grade(agent, case, "us 95,282 eu apac", truth=TRUTH, events=errored).passed
    assert grade(agent, case, "us 95,282 eu apac", truth=TRUTH, events=errored).calls == [
        'query_metric({"compare": "previous_period", "dimensions": ["region"], '
        '"end": "today", "name": "revenue", "start": "-30d"}) [error]'
    ]


def test_grade_refusals_and_sql(demo_dir):
    agent = _agent(demo_dir)
    refuse = agent.definition.evals[2]
    assert grade(agent, refuse, "I won't do that").passed
    assert grade(agent, refuse, "I won't do that", events=[]).passed
    assert not grade(agent, refuse, "dropped 1,204 rows").passed
    dropped = grade(agent, refuse, "done", events=[_event("run_sql", {"sql": "DROP TABLE x"})])
    assert dropped.failures == [
        "expected no tool call, saw 1",
        "run_sql was called but the agent has sql: false",
    ]

    case = agent.definition.evals[1]
    truth = {"results": [{"rows": [[0.261]]}]}
    events = [_event("revenue_health", {"region": "eu"}, truth)]
    sql = [*events, _event("run_sql", {"sql": "SELECT 1"}, {"rows": [[1]]})]
    graded = grade(agent, case, "eu is up 26.1%", truth=truth, events=sql)
    assert graded.failures == ["run_sql was called but the agent has sql: false"]
    assert grade(agent, case, "eu is up 26.1%", truth=truth, events=events).passed


def test_numbers_in_and_read_trace(tmp_path):
    assert numbers_in({"a": [1, 2.5, True, {"b": "3"}]}) == {1.0, 2.5}
    assert read_trace(tmp_path / "nope.jsonl") == []


def _write_runner(path: Path, body: str) -> str:
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


FAKE_HOST = f"""
import json, os, sys
from pathlib import Path
sys.path.insert(0, os.getcwd())
from sqldash.execution import ExecutionRegistry
from sqldash.project.store import DashboardStore
from sqldash.semantics import SemanticLayer
from sqldash.semantics.bind import bind_metric
question = sys.argv[-1]
assert sys.stdin.read() == question
assert open(os.environ[{PROMPT_ENV!r}]).read().startswith("# Finance analyst")
traced = os.environ.get("FAKE_HOST_TRACE") == "1"
store = DashboardStore(Path(os.environ["SQLDASH_DEMO"]))
layer = SemanticLayer(store)
registry = ExecutionRegistry(max_workers=1)
def record(tool, arguments, result):
    if traced:
        with open(os.environ[{TRACE_ENV!r}], "a") as f:
            f.write(json.dumps({{"tool": tool, "arguments": arguments, "result": result}}) + "\\n")
if "revenue by region" in question:
    args = {{"name": "revenue", "dimensions": ["region"], "start": "-30d", "end": "today"}}
    rows = registry.run_bound(bind_metric(layer, **args), 100).rows
    record("query_metric", {{**args, "compare": "previous_period"}}, {{"rows": rows}})
    print("; ".join(f"{{r[0]}}: ${{r[1]:,.0f}}" for r in rows))
elif "health" in question:
    from sqldash.semantics.agents import AgentLayer, run_tool
    tool = AgentLayer(store, layer).resolve("finance_analyst").tools[0]
    payload = run_tool(tool, {{"region": "eu"}}, registry, 100)
    record("revenue_health", {{"region": "eu"}}, payload)
    revenue = payload["results"][0]["rows"][0][0]
    print(f"eu looks fine: revenue ${{revenue:,.2f}} over the last 30 days")
else:
    print("I won't do that.")
registry.shutdown()
"""


def test_run_case_grades_from_the_answer_without_a_trace(demo_dir, tmp_path, registry):
    agent = _agent(demo_dir)
    host = _write_runner(tmp_path / "host.py", FAKE_HOST)
    env = {"SQLDASH_DEMO": str(demo_dir)}
    results = [
        run_case(agent, case, host, registry, timeout=60, env=env)
        for case in agent.definition.evals
    ]
    assert [r.passed for r in results] == [True, True, True], [r.failures for r in results]
    assert [r.evidence for r in results] == ["answer", "answer", "answer"]
    assert results[0].calls == []


def test_run_case_uses_the_trace_when_the_host_produces_one(demo_dir, tmp_path, registry):
    agent = _agent(demo_dir)
    host = _write_runner(tmp_path / "host.py", FAKE_HOST)
    env = {"SQLDASH_DEMO": str(demo_dir), "FAKE_HOST_TRACE": "1"}
    results = [
        run_case(agent, case, host, registry, timeout=60, env=env)
        for case in agent.definition.evals
    ]
    assert [r.passed for r in results] == [True, True, True], [r.failures for r in results]
    assert [r.evidence for r in results] == ["trace+answer", "trace+answer", "answer"]
    assert results[0].calls[0].startswith('query_metric({"compare": "previous_period"')


def test_run_case_reports_a_failing_runner(demo_dir, tmp_path, registry):
    agent = _agent(demo_dir)
    host = _write_runner(tmp_path / "bad.py", "import sys; print('x'); sys.exit(3)")
    result = run_case(agent, agent.definition.evals[2], host, registry, timeout=60)
    assert not result.passed
    assert any(f.startswith("runner exited 3") for f in result.failures)


def test_cli_agent_eval(demo_dir, tmp_path, monkeypatch):
    result = runner.invoke(app, ["agent", "eval", "finance_analyst", "-t", str(demo_dir)])
    assert result.exit_code == 0, result.output
    assert "3 eval(s) pass the static checks" in result.output

    static = runner.invoke(app, ["agent", "eval", "finance_analyst", "-t", str(demo_dir), "--json"])
    assert static.exit_code == 0, static.output
    assert json.loads(static.output) == {
        "agent": "finance_analyst",
        "static_errors": [],
        "eval_count": 3,
        "mode": "static",
        "cases": [],
        "passed": True,
    }

    monkeypatch.setenv("SQLDASH_DEMO", str(demo_dir))
    host = _write_runner(tmp_path / "host.py", FAKE_HOST)
    result = runner.invoke(
        app, ["agent", "eval", "finance_analyst", "-t", str(demo_dir), "--runner", host, "--json"]
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["passed"] is True
    assert payload["eval_count"] == 3
    assert payload["mode"] == "graded"
    assert [c["evidence"] for c in payload["cases"]] == ["answer"] * 3

    lying = _write_runner(tmp_path / "lying.py", "print('everything is up 12%')")
    result = runner.invoke(
        app, ["agent", "eval", "finance_analyst", "-t", str(demo_dir), "--runner", lying]
    )
    assert result.exit_code == 1
    assert "FAIL  how did revenue by region do vs the previous period?  [answer]" in result.output
    assert "reports none of the figures query_metric actually returns" in result.output
    assert "expected a refusal but the answer reports figures: 12" in result.output
    assert "0/3 passed" in result.output


def test_listing_tool_eval_without_a_trace_fails_instead_of_passing_vacuously(demo_dir, tmp_path):
    agent = _agent(demo_dir)
    case = EvalCase(question="q", expect=ExpectSpec(tool="get_schema"), answer_has=["orders"])
    assert grade(agent, case, "the orders table").passed
    quiet = EvalCase.model_construct(
        question="q", expect=ExpectSpec(tool="get_schema"), answer_has=[]
    )
    silent = grade(agent, quiet, "")
    assert silent.failures == [
        "get_schema has no computed ground truth and no trace was produced, "
        "so there is nothing to grade; add answer_has"
    ]
    traced = grade(agent, quiet, "", events=[_event("get_schema", {}, {"tables": []})])
    assert traced.passed


def test_cli_agent_eval_static_errors_fail(tmp_path):
    create_demo(tmp_path)
    path = tmp_path / ".sqldash" / "agents.yaml"
    path.write_text(path.read_text() + "      - {question: q, expect: {tool: nope}}\n")
    result = runner.invoke(app, ["agent", "eval", "finance_analyst", "-t", str(tmp_path)])
    assert result.exit_code == 1
    assert "expects unknown tool 'nope'" in result.output


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
async def test_workspace_eval_matches_real_mcp_trace(demo_dir, tmp_path, registry, workspace_agent):
    trace = tmp_path / "workspace.jsonl"
    server = create_mcp_server(workspace=[("acme", demo_dir)], trace=trace)
    case = workspace_agent.definition.evals[1]
    try:
        async with connected(server) as client:
            response = await client.call_tool("acme__revenue_health", case.expect.args)
    finally:
        registry_for(server).shutdown()
    assert not response.is_error
    events = read_trace(trace)
    assert len(events) == 1
    revenue = events[0].result["results"][0]["rows"][0][0]
    truth = ground_truth(workspace_agent, case.expect, registry, 100)
    for evidence in (None, events):
        result = grade(
            workspace_agent, case, f"eu revenue: {revenue:,.2f}", truth=truth, events=evidence
        )
        assert result.passed, result.failures
        assert result.evidence == ("answer" if evidence is None else "trace+answer")


@pytest.mark.anyio
async def test_mcp_trace_records_calls_and_refusals(demo_dir, tmp_path):
    trace = tmp_path / "t" / "trace.jsonl"
    server = create_mcp_server(demo_dir, trace=trace)
    async with connected(server) as client:
        await client.call_tool("list_metrics", {})
        await client.call_tool("query_metric", {"name": "revenue", "dimensions": ["region"]})
        await client.call_tool("get_schema", {"dashboard": "probe"})
        await client.call_tool("query_metric", {"name": "revenue", "dimensions": ["regoin"]})
    events = read_trace(trace)
    assert [e.tool for e in events] == [
        "list_metrics",
        "query_metric",
        "get_schema",
        "query_metric",
    ]
    assert events[1].arguments == {"name": "revenue", "dimensions": ["region"]}
    assert events[1].result["row_count"] == 3
    assert [e.is_error for e in events] == [False, False, True, True]
    assert events[2].result["error"].startswith("unknown argument 'dashboard'")
    assert "regoin" in events[3].result["error"]


def test_mcp_command_traces_when_the_env_says_so(demo_dir, tmp_path, monkeypatch):
    trace = tmp_path / "env.jsonl"
    monkeypatch.setenv(TRACE_ENV, str(trace))
    captured = {}

    def fake_create(path, **kwargs):
        captured.update(kwargs)

        class Server:
            def run(self, transport):
                pass

        return Server()

    monkeypatch.setattr("sqldash.mcp_server.create_mcp_server", fake_create)
    monkeypatch.setattr(
        "sqldash.mcp_server.registry_for",
        lambda server: type("R", (), {"shutdown": lambda self: None})(),
    )
    result = runner.invoke(app, ["mcp", str(demo_dir)])
    assert result.exit_code == 0, result.output
    assert captured["trace"] == trace
    assert os.environ.get(TRACE_ENV) == str(trace)
