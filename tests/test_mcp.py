import json
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
import duckdb
import pytest
from mcp import StdioServerParameters
from mcp.client.session import ClientSession
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_client_server_memory_streams
from typer.testing import CliRunner

import sqldash.mcp_server as server_module
from sqldash.cli import app as cli_app
from sqldash.mcp_server import create_mcp_server, mcp_result
from sqldash.scaffold import create_demo
from sqldash.semantics import SemanticError
from sqldash.sqlguard import SIDE_EFFECT_FUNCTIONS, SNOWFLAKE_SYSTEM_READS


@pytest.fixture(scope="module")
def demo_dir(tmp_path_factory):
    target = tmp_path_factory.mktemp("mcp-demo")
    create_demo(target)
    return target


@asynccontextmanager
async def connected_session(server):
    """In-memory ClientSession over MCPServer._lowlevel_server.

    mcp 2.x dropped create_connected_server_and_client_session and FastMCP._mcp_server.
    The streams + low-level run loop are the replacement.
    """
    low = server._lowlevel_server
    async with create_client_server_memory_streams() as (client_streams, server_streams):
        client_read, client_write = client_streams
        server_read, server_write = server_streams

        async def run_server():
            await low.run(
                server_read,
                server_write,
                low.create_initialization_options(),
            )

        async with anyio.create_task_group() as tg:
            tg.start_soon(run_server)
            try:
                async with ClientSession(
                    read_stream=client_read, write_stream=client_write
                ) as client:
                    await client.initialize()
                    yield client
            finally:
                tg.cancel_scope.cancel()


async def call(server, tool, arguments=None):
    async with connected_session(server) as client:
        result = await client.call_tool(tool, arguments or {})
        assert not result.is_error, result.content
        return json.loads(result.content[0].text)


async def tool_names(server):
    async with connected_session(server) as client:
        tools = await client.list_tools()
        return {t.name for t in tools.tools}


def test_mcp_result_turns_a_domain_error_into_a_payload():
    @mcp_result
    def boom():
        raise SemanticError("nope")

    assert boom() == {"error": "nope"}


def test_mcp_result_lets_a_programming_error_raise():
    @mcp_result
    def boom():
        raise TypeError("bug in the tool")

    with pytest.raises(TypeError, match="bug in the tool"):
        boom()


@pytest.mark.anyio
async def test_metrics_only_surface_by_default(demo_dir):
    server = create_mcp_server(demo_dir)
    names = await tool_names(server)
    assert "run_sql" not in names
    assert {"list_metrics", "get_metric", "query_metric", "list_sources", "get_dashboards"} <= names


@pytest.mark.anyio
async def test_list_and_get_metric(demo_dir):
    server = create_mcp_server(demo_dir)
    listed = await call(server, "list_metrics")
    revenue = next(m for m in listed["metrics"] if m["name"] == "revenue")
    assert revenue["time_dimension"]["grain"] == "day"
    assert revenue["time_dimension"]["default_grain"] == "day"
    assert {m["name"] for m in listed["metrics"]} == {
        "revenue",
        "order_count",
        "avg_order_value",
        "cumulative_revenue",
        "trailing_28d_revenue",
    }
    detail = await call(server, "get_metric", {"name": "revenue"})
    assert detail["expr"] == "SUM(amount)"
    assert detail["relation"] == {"table": "orders"}
    assert "password" not in json.dumps(detail)


@pytest.mark.anyio
async def test_query_metric_round_trip(demo_dir):
    server = create_mcp_server(demo_dir)
    result = await call(
        server,
        "query_metric",
        {"name": "revenue", "dimensions": ["region"], "filters": {"region": ["us", "eu"]}},
    )
    assert result["sql"].startswith('SELECT region AS "region", SUM(amount) AS "revenue"')
    regions = {row[0] for row in result["rows"]}
    assert regions == {"us", "eu"}


@pytest.mark.anyio
async def test_query_metric_bad_dimension_is_agent_friendly(demo_dir):
    server = create_mcp_server(demo_dir)
    async with connected_session(server) as client:
        result = await client.call_tool(
            "query_metric", {"name": "revenue", "dimensions": ["region; DROP TABLE x"]}
        )
        assert not result.is_error
        payload = json.loads(result.content[0].text)
        assert "valid dimensions" in payload["error"]


@pytest.mark.anyio
async def test_query_metric_limit_clamped(demo_dir):
    server = create_mcp_server(demo_dir, row_limit=5)
    result = await call(
        server, "query_metric", {"name": "revenue", "grain": "day", "limit": 10_000}
    )
    assert result["row_count"] <= 5


@pytest.mark.anyio
async def test_run_sql_negative_limit_is_an_error(demo_dir):
    """A negative cap used to return empty rows with truncated:true. #440."""
    server = create_mcp_server(demo_dir, allow_sql=True)
    result = await call(server, "run_sql", {"sql": "SELECT 1 AS n", "limit": -1})
    assert "error" in result
    assert "limit must be >= 0" in result["error"]
    assert "truncated" not in result
    assert "rows" not in result


@pytest.mark.anyio
async def test_run_sql_gated_behind_flag(demo_dir):
    server = create_mcp_server(demo_dir, allow_sql=True)
    names = await tool_names(server)
    assert "run_sql" in names
    result = await call(server, "run_sql", {"sql": "SELECT COUNT(*) AS n FROM orders"})
    assert result["rows"][0][0] > 0
    async with connected_session(server) as client:
        multi = await client.call_tool("run_sql", {"sql": "SELECT 1; DROP TABLE orders"})
        assert multi.is_error or "one statement" in multi.content[0].text


@pytest.mark.anyio
async def test_run_sql_keeps_big_integers_as_json_numbers(demo_dir):
    """Only the web UI gets big integers as text; an agent reads exact JSON numbers."""
    server = create_mcp_server(demo_dir, allow_sql=True)
    result = await call(
        server,
        "run_sql",
        {"sql": "SELECT 12345678901234567890::HUGEINT AS huge, '-1E-10'::DECIMAL(38,10) AS d"},
    )
    assert result["rows"] == [[12345678901234567890, "-1E-10"]]


@pytest.mark.anyio
async def test_run_sql_refuses_copy_to_and_writes_nothing(demo_dir, tmp_path):
    """The row cap bounds what comes back, not what a statement does: COPY TO
    used to dump every row of the table to any path the server's user can write."""
    server = create_mcp_server(demo_dir, allow_sql=True)
    target = tmp_path / "stolen.csv"
    result = await call(
        server, "run_sql", {"sql": f"COPY (SELECT * FROM orders) TO '{target}' (HEADER)"}
    )
    assert "read-only" in result["error"]
    assert "COPY" in result["error"]
    assert not target.exists()


@pytest.mark.anyio
async def test_run_sql_cannot_read_files_outside_the_project(demo_dir, tmp_path):
    """run_sql shares the pooled connection with the HTTP route, so the #622
    confinement has to hold for an agent too."""
    server = create_mcp_server(demo_dir, allow_sql=True)
    secret = tmp_path / "profiles.yaml"
    secret.write_text("acme:\n  password: test-fixture-not-a-real-secret\n")
    result = await call(server, "run_sql", {"sql": f"SELECT content FROM read_text('{secret}')"})
    assert "test-fixture-not-a-real-secret" not in json.dumps(result)
    assert "external_access: true" in result["error"]


@pytest.mark.anyio
async def test_run_sql_refuses_explain_analyze_of_a_setting_change(demo_dir):
    """EXPLAIN ANALYZE runs the statement it explains, so a SET under it is a
    side effect that plain EXPLAIN never has. #297 review."""
    server = create_mcp_server(demo_dir, allow_sql=True)
    before = await call(server, "run_sql", {"sql": "SELECT current_setting('memory_limit') AS m"})
    refused = await call(server, "run_sql", {"sql": "EXPLAIN ANALYZE SET memory_limit = '1GB'"})
    assert "SET" in refused["error"]
    after = await call(server, "run_sql", {"sql": "SELECT current_setting('memory_limit') AS m"})
    assert after["rows"] == before["rows"]


@pytest.mark.anyio
async def test_run_sql_refuses_enable_logging_and_keeps_serving(demo_dir):
    """Logging to a path outside the project aborted the process on the pool's
    rollback; inside it, every later statement was written to csv."""
    server = create_mcp_server(demo_dir, allow_sql=True)
    logs = demo_dir / "agent-logs"
    for path in ("/tmp/sqldash-outside", logs):
        sql = f"SELECT * FROM enable_logging(storage='file', storage_path='{path}')"
        result = await call(server, "run_sql", {"sql": sql})
        assert "enable_logging() is refused" in result["error"]
    after = await call(server, "run_sql", {"sql": "SELECT COUNT(*) AS n FROM orders"})
    assert after["rows"][0][0] > 0
    assert not logs.exists()


@pytest.mark.anyio
async def test_run_sql_refuses_create_table_as(demo_dir):
    server = create_mcp_server(demo_dir, allow_sql=True)
    result = await call(
        server, "run_sql", {"sql": "CREATE OR REPLACE TABLE stash AS SELECT * FROM orders"}
    )
    assert "read-only" in result["error"]
    assert "CREATE" in result["error"]
    result = await call(server, "run_sql", {"sql": "SELECT COUNT(*) FROM stash"})
    assert "stash" in result["error"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "sql",
    [
        "-- what the agent was thinking\nSELECT COUNT(*) AS n FROM orders",
        "/* block */ SELECT COUNT(*) AS n FROM orders",
        "WITH x AS (SELECT COUNT(*) AS n FROM orders) SELECT * FROM x",
        "SELECT COUNT(*) AS n FROM orders WHERE 'insert into' <> 'copy'",
    ],
)
async def test_run_sql_still_runs_reads(demo_dir, sql):
    server = create_mcp_server(demo_dir, allow_sql=True)
    result = await call(server, "run_sql", {"sql": sql})
    assert "error" not in result
    assert result["rows"][0][0] > 0


@pytest.mark.parametrize(
    "sql",
    [
        "WITH x AS (SELECT 1) INSERT INTO t SELECT * FROM x",
        "WITH x AS (INSERT INTO t VALUES (1) RETURNING *) SELECT * FROM x",
        "EXPLAIN ANALYZE DELETE FROM orders",
        "EXPLAIN ANALYZE SET memory_limit = '1GB'",
        "EXPLAIN ANALYZE PRAGMA enable_profiling",
        "EXPLAIN ANALYZE CALL pragma_version()",
        "EXPLAIN ANALYZE INSTALL httpfs",
        "EXPLAIN ANALYZE LOAD httpfs",
        "EXPLAIN ANALYZE CHECKPOINT",
        "REPLACE INTO orders VALUES (1)",
        "SELECT * INTO stash FROM orders",
        "ATTACH 'other.db'",
        "EXPORT DATABASE 'out'",
        "INSTALL httpfs",
        "LOAD httpfs",
        "PRAGMA enable_profiling",
        "SET memory_limit = '1GB'",
        "CALL pragma_version()",
        "UPDATE orders SET amount = 0",
        "DELETE FROM orders",
        "DROP TABLE orders",
        "SELECT 1; DROP TABLE orders",
        "",
    ],
)
def test_read_only_violation_names_the_offending_keyword(sql):
    verdict = server_module.read_only_violation(sql)
    assert verdict is not None
    assert "run_sql" in verdict


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "  (SELECT 1) UNION (SELECT 2)",
        "FROM orders",
        "SHOW TABLES",
        "DESCRIBE orders",
        "EXPLAIN SELECT 1",
        "VALUES (1)",
        "TABLE orders",
        'SELECT * FROM orders WHERE "copy" = 1',
        "SELECT * FROM pragma_table_info('orders')",
        "SELECT REPLACE(region, 'u', 'x') AS r FROM orders",
        "EXPLAIN ANALYZE SELECT 1",
        "SELECT ';' AS semi",
        "SELECT 'a -- b; DROP' AS note",
        "SELECT 1 -- ; DROP TABLE orders",
        "SELECT 1 /* ; DROP TABLE orders */",
        "SELECT 1;",
    ],
)
def test_read_only_violation_lets_reads_through(sql):
    assert server_module.read_only_violation(sql) is None


SIDE_EFFECT_CALLS = [
    "SELECT * FROM enable_logging(storage='file', storage_path='/tmp/x')",
    "SELECT * FROM disable_logging()",
    "SELECT * FROM truncate_duckdb_logs()",
    "SELECT write_log('hi')",
    "SELECT * FROM enable_profiling()",
    "SELECT * FROM disable_profiling()",
    "SELECT * FROM checkpoint()",
    "SELECT * FROM force_checkpoint()",
    "SELECT nextval('seq')",
    "SELECT setseed(0.5)",
    "SELECT * FROM arrow_scan(0, 0, 0)",
    "SELECT * FROM arrow_scan_dumb(0, 0, 0)",
    "SELECT * FROM pandas_scan(0)",
    "SELECT * FROM python_map_function(t, 0, 0)",
    "SELECT * FROM query('SELECT * FROM enable_profiling()')",
    "SELECT * FROM json_execute_serialized_sql('{}')",
]


@pytest.mark.parametrize("sql", SIDE_EFFECT_CALLS)
@pytest.mark.parametrize(
    "shape",
    [
        "{}",
        "WITH x AS ({}) SELECT * FROM x",
        "SELECT * FROM ({}) AS sub",
        "SELECT 1 WHERE EXISTS ({})",
        "EXPLAIN {}",
    ],
)
def test_a_side_effecting_function_is_refused_on_every_path(sql, shape):
    """These run behind a SELECT opener. enable_logging to a path outside the
    project aborted the whole server on the pool's rollback."""
    name = sql.split("(")[0].split()[-1]
    statement = shape.format(sql)
    verdict = server_module.read_only_violation(statement)
    assert verdict is not None
    assert f"{name}() is refused because it" in verdict
    assert verdict.startswith("run_sql is read-only; ")
    tile = server_module.read_only_violation(statement, surface="tile SQL", scan_body=False)
    assert tile is not None
    assert f"{name}()" in tile


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM ENABLE_LOGGING()",
        "SELECT * FROM Enable_Profiling ()",
        "SELECT * FROM enable_logging /* why */ ()",
        "SELECT * FROM enable_logging -- why\n()",
        "SELECT * FROM enable_logging\n\t()",
        'SELECT * FROM "enable_logging"()',
        'SELECT * FROM "Enable_Logging" /* x */ ()',
        'SELECT * FROM system."main"."enable_logging"()',
        "SELECT * FROM system.main.enable_logging()",
        "SELECT (0.5).setseed()",
        "SELECT * FROM orders WHERE 1 = (SELECT count(*) FROM checkpoint())",
    ],
)
def test_the_function_check_sees_through_case_quoting_and_comments(sql):
    verdict = server_module.read_only_violation(sql)
    assert verdict is not None
    assert "() is refused because it" in verdict


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT checkpoint_at FROM orders",
        "SELECT enable_logging_flag, query_count FROM orders",
        "SELECT query_count(1) FROM orders",
        "SELECT my_checkpoint() FROM orders",
        'SELECT "query" FROM orders',
        "SELECT 'enable_logging()' AS note",
        "SELECT 1 -- checkpoint()",
        "SELECT 1 /* nextval('s') */",
        "SELECT $$ query('x') $$ AS note",
        "SELECT * FROM (SELECT 1) query(a)",
        "SELECT * FROM (SELECT 1, 2) AS query(a, b)",
        'SELECT * FROM (SELECT 1) "query"(a)',
        "WITH query(a) AS (SELECT 1) SELECT * FROM query",
    ],
)
def test_lookalike_names_are_not_refused(sql):
    assert server_module.read_only_violation(sql) is None
    assert server_module.read_only_violation(sql, surface="tile SQL", scan_body=False) is None


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM query('SELECT 1')",
        "SELECT * FROM query(  )",
        "SELECT * FROM query('SEL' || 'ECT 1')",
        "SELECT * FROM query(getvariable('s'))",
        "SELECT * FROM query($1)",
        'SELECT * FROM query("SELECT 42")',
        'SELECT * FROM query("x")',
        'SELECT * FROM (SELECT 1 AS "b c") AS query("b c")',
        "SELECT * FROM query('SELECT 1', \"x\")",
        "SELECT * FROM query(\"x\", 'SELECT 1')",
    ],
)
def test_a_query_call_with_anything_but_a_column_list_is_refused(sql):
    assert "query() is refused" in server_module.read_only_violation(sql)


def test_the_refused_list_is_pinned():
    assert set(SIDE_EFFECT_FUNCTIONS) == {
        "enable_logging",
        "disable_logging",
        "truncate_duckdb_logs",
        "write_log",
        "enable_profiling",
        "disable_profiling",
        "checkpoint",
        "force_checkpoint",
        "nextval",
        "setseed",
        "arrow_scan",
        "arrow_scan_dumb",
        "pandas_scan",
        "python_map_function",
        "query",
        "json_execute_serialized_sql",
    }


SNOWFLAKE_SYSTEM_CALLS = [
    "SELECT SYSTEM$ABORT_SESSION(1)",
    "SELECT SYSTEM$ABORT_TRANSACTION(1)",
    "SELECT SYSTEM$CANCEL_ALL_QUERIES(1)",
    "SELECT SYSTEM$CANCEL_QUERY('01b2c3d4-0000-0000-0000-000000000000')",
    "SELECT SYSTEM$WAIT(10)",
    "SELECT SYSTEM$USER_TASK_CANCEL_ONGOING_EXECUTIONS('t')",
    "SELECT SYSTEM$SET_RETURN_VALUE('x')",
    "SELECT SYSTEM$A_FUNCTION_SNOWFLAKE_ADDS_NEXT_YEAR()",
    'SELECT "SYSTEM$ABORT_SESSION"(1)',
    "SELECT system$abort_session /* id */ (1)",
    "SELECT * FROM orders WHERE 1 = (SELECT SYSTEM$CANCEL_ALL_QUERIES(1))",
]


@pytest.mark.parametrize("sql", SNOWFLAKE_SYSTEM_CALLS)
@pytest.mark.parametrize("shape", ["{}", "WITH x AS ({}) SELECT * FROM x", "EXPLAIN {}"])
def test_a_snowflake_system_function_that_is_not_a_read_is_refused(sql, shape):
    """SYSTEM$ABORT_SESSION through /api/run ended a session of the source's user,
    and SHOW FUNCTIONS does not list every SYSTEM$ function, so only reviewed
    reads run."""
    statement = shape.format(sql)
    for verdict in (
        server_module.read_only_violation(statement),
        server_module.read_only_violation(statement, surface="tile SQL", scan_body=False),
    ):
        assert verdict is not None
        assert "() is refused because it" in verdict
        assert "system$" in verdict


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT SYSTEM$TYPEOF(amount) AS t FROM orders",
        "SELECT SYSTEM$CLUSTERING_INFORMATION('orders') AS info",
        "SELECT PARSE_JSON(SYSTEM$EXPLAIN_PLAN_JSON('SELECT 1')) AS plan",
        "SELECT SYSTEM$STREAM_HAS_DATA('s') AS pending",
        "SELECT system$abort_flag, cancel_query FROM orders",
        "SELECT 'SYSTEM$ABORT_SESSION(1)' AS note",
        "SELECT * FROM IDENTIFIER('orders')",
        "SELECT * FROM IDENTIFIER($table_name) WHERE region IN ('us')",
    ],
)
def test_snowflake_reads_and_lookalikes_still_run(sql):
    assert server_module.read_only_violation(sql) is None
    assert server_module.read_only_violation(sql, surface="tile SQL", scan_body=False) is None


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT IDENTIFIER('SYSTEM$ABORT_SESSION')(1)",
        "SELECT identifier ( 'SYSTEM$WAIT' ) /* x */ (10)",
        "SELECT * FROM TABLE(IDENTIFIER($fn)(1))",
    ],
)
def test_a_function_called_through_identifier_is_refused(sql):
    """IDENTIFIER('SYSTEM$TYPEOF')(1) runs on Snowflake, so the name the guard
    checks can hide in a literal."""
    verdict = server_module.read_only_violation(sql)
    assert verdict == (
        "run_sql is read-only; IDENTIFIER(...)() is refused because it names the "
        "called function in a string the guard cannot check"
    )


def test_the_snowflake_system_reads_are_pinned():
    assert set(SNOWFLAKE_SYSTEM_READS) == {
        "system$allowlist",
        "system$allowlist_privatelink",
        "system$behavior_change_bundle_status",
        "system$client_version_info",
        "system$clustering_depth",
        "system$clustering_information",
        "system$clustering_ratio",
        "system$current_user_task_name",
        "system$estimate_automatic_clustering_costs",
        "system$estimate_query_acceleration",
        "system$estimate_search_optimization_costs",
        "system$explain_json_to_text",
        "system$explain_plan_json",
        "system$external_table_pipe_status",
        "system$get_compute_pool_status",
        "system$get_directory_table_status",
        "system$get_predecessor_return_value",
        "system$get_service_status",
        "system$get_tag",
        "system$get_tag_allowed_values",
        "system$get_tag_on_current_column",
        "system$get_tag_on_current_table",
        "system$get_task_graph_config",
        "system$last_change_commit_time",
        "system$pipe_status",
        "system$show_active_behavior_change_bundles",
        "system$stage_pipe_status",
        "system$stream_get_table_timestamp",
        "system$stream_has_data",
        "system$tag_value_contains_on_current_column",
        "system$tag_value_contains_on_current_table",
        "system$task_runtime_info",
        "system$typeof",
    }


def test_every_duckdb_table_function_has_been_reviewed():
    """Table functions carry no side-effect flag, so enable_logging() was only
    caught by reading the list. A DuckDB upgrade that adds one fails here until
    it is either refused in sqlguard or added to the snapshot as a read."""
    snapshot = json.loads((Path(__file__).parent / "duckdb_table_functions.json").read_text())
    installed = {
        name
        for (name,) in duckdb.connect()
        .execute(
            "SELECT DISTINCT function_name FROM duckdb_functions() WHERE function_type = 'table'"
        )
        .fetchall()
    }
    reviewed = set(snapshot["table_functions"])
    assert installed == reviewed, (
        f"new: {sorted(installed - reviewed)}, gone: {sorted(reviewed - installed)}"
    )


def test_the_refused_list_covers_the_installed_duckdb():
    """Derived from duckdb_functions(): a table function taking a POINTER reads raw
    memory, and a scalar flagged has_side_effects either changes state or is one of
    the reviewed reads below. A DuckDB upgrade that adds one fails here until it is
    sorted into one list or the other."""
    reviewed_reads = {
        "current_connection_id",
        "current_query",
        "current_query_id",
        "current_transaction_id",
        "currval",
        "error",
        "gen_random_uuid",
        "random",
        "sleep_ms",
        "stats",
        "uuid",
        "uuidv4",
        "uuidv7",
    }
    conn = duckdb.connect()
    try:
        pointers = {
            name
            for (name,) in conn.execute(
                "SELECT DISTINCT function_name FROM duckdb_functions() "
                "WHERE function_type = 'table' AND list_contains(parameter_types, 'POINTER')"
            ).fetchall()
        }
        flagged = {
            name
            for (name,) in conn.execute(
                "SELECT DISTINCT function_name FROM duckdb_functions() WHERE has_side_effects"
            ).fetchall()
        }
        known = {
            name
            for (name,) in conn.execute(
                "SELECT DISTINCT function_name FROM duckdb_functions()"
            ).fetchall()
        }
    finally:
        conn.close()
    refused = set(SIDE_EFFECT_FUNCTIONS)
    assert pointers <= refused
    assert flagged - reviewed_reads <= refused
    assert refused <= known


def test_authored_sql_refuses_a_write_opener_but_not_a_copy_column():
    """Tile SQL is opener-only so `SELECT k AS copy` still runs. #500."""
    assert (
        server_module.read_only_violation("DELETE FROM orders", surface="tile SQL", scan_body=False)
        == "tile SQL is a write statement"
    )
    assert (
        server_module.read_only_violation(
            "COPY (SELECT 1) TO 'out.csv'", surface="tile SQL", scan_body=False
        )
        == "tile SQL is a write statement"
    )
    assert (
        server_module.read_only_violation(
            "SELECT k AS copy FROM orders", surface="tile SQL", scan_body=False
        )
        is None
    )
    assert server_module.read_only_violation("SELECT k AS copy FROM orders") is not None
    assert (
        server_module.read_only_violation("SELEKT oops FROM", surface="tile SQL", scan_body=False)
        is None
    )
    assert (
        server_module.read_only_violation(
            "{% if region %}DELETE FROM orders{% endif %}",
            surface="tile SQL",
            scan_body=False,
        )
        == "tile SQL is a write statement"
    )
    assert (
        server_module.read_only_violation(
            "EXPLAIN ANALYZE DELETE FROM orders", surface="tile SQL", scan_body=False
        )
        == "tile SQL is a write statement"
    )
    assert (
        server_module.read_only_violation(
            "WITH gone AS (DELETE FROM orders RETURNING 1) SELECT * FROM gone",
            surface="tile SQL",
            scan_body=False,
        )
        == "tile SQL is a write statement"
    )
    assert (
        server_module.read_only_violation("EXPLAIN SELECT 1", surface="tile SQL", scan_body=False)
        is None
    )


@pytest.mark.parametrize(
    "sql",
    [
        "USE SCHEMA analytics.information_schema",
        "USE SECONDARY ROLES NONE",
        "use warehouse big_wh",
        "UNSET my_var",
        "RESET search_path",
        "DISCARD ALL",
        "{% if region %}USE DATABASE other{% endif %}",
    ],
)
def test_a_tile_that_changes_session_state_is_refused(sql):
    """A USE SCHEMA query ran on a pooled Snowflake session and every later
    caller on that session read the other schema."""
    word = sql.split()[0].upper() if not sql.startswith("{%") else "USE"
    assert server_module.read_only_violation(sql, surface="tile SQL", scan_body=False) == (
        "tile SQL changes session state that later queries on the pooled connection "
        f"would inherit; {word} statements are refused"
    )


def test_session_words_as_columns_still_run():
    sql = "SELECT use, reset, discard FROM orders"
    assert server_module.read_only_violation(sql, surface="tile SQL", scan_body=False) is None


@pytest.mark.parametrize(
    "sql",
    [
        "REPLACE INTO t VALUES (1, 'x')",
        "VACUUM",
        "FORCE CHECKPOINT",
        "ANALYZE orders",
        "ANALYSE orders",
        "UNLOAD ('SELECT * FROM orders') TO 's3://bucket/x'",
        "REINDEX orders",
        "GRANT SELECT ON orders TO PUBLIC",
        "GET @stage file:///tmp/",
        "DO $$ BEGIN DELETE FROM orders; END $$",
        "EXECUTE IMMEDIATE 'DROP TABLE orders'",
        "EXPLAIN ANALYZE VACUUM",
        "EXPLAIN ANALYZE VERBOSE FORCE CHECKPOINT",
        "{% if region %}VACUUM{% endif %}",
    ],
)
def test_a_tile_that_opens_with_a_write_is_refused(sql):
    """REPLACE INTO / VACUUM / FORCE CHECKPOINT tiles wrote the source on every view
    and lint stayed green: on the tile path only WRITE_WORDS openers were refused."""
    assert (
        server_module.read_only_violation(sql, surface="tile SQL", scan_body=False)
        == "tile SQL is a write statement"
    )
    assert server_module.read_only_violation(sql, surface="ad-hoc sql") is not None


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT k AS copy, REPLACE(k, 'a', 'b') AS vacuum FROM orders",
        "SELECT comment, start, get, lock, system FROM orders",
        "EXPLAIN ANALYZE SELECT 1",
        "EXPLAIN (ANALYZE, FORMAT JSON) SELECT 1",
        "EXPLAIN SELECT comment FROM orders",
        "SELEKT oops FROM",
    ],
)
def test_write_openers_do_not_refuse_reads(sql):
    assert server_module.read_only_violation(sql, surface="tile SQL", scan_body=False) is None


@pytest.mark.anyio
async def test_inline_metrics_fallback_without_metrics_yaml(tmp_path):
    (tmp_path / "solo.yaml").write_text(
        """
title: Solo
source: {type: duckdb, database: ':memory:'}
metrics:
  ticket_count: {sql: 'SELECT 1 AS n UNION ALL SELECT 2', expr: 'COUNT(*)'}
queries: {q: 'SELECT 1'}
tiles: [{id: w, metric: ticket_count}]
"""
    )
    server = create_mcp_server(tmp_path)
    listed = await call(server, "list_metrics")
    assert [m["name"] for m in listed["metrics"]] == ["ticket_count"]
    assert listed["metrics"][0]["origin"] == "dashboard"
    result = await call(server, "query_metric", {"name": "ticket_count"})
    assert result["rows"] == [[2]]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_get_schema_does_not_echo_connect_args_secrets(tmp_path):
    """DuckDB's connect() TypeError repeats kwargs by value. That used to leak
    through get_schema as a protocol error containing the plaintext secrets."""
    (tmp_path / "metrics.yaml").write_text(
        "source:\n"
        "  type: duckdb\n"
        "  connect_args:\n"
        "    private_key: CLEAN_PK_SECRET_99\n"
        "    session_token: CLEAN_TOK_SECRET_88\n"
        "relations:\n  orders: {table: orders}\n"
        "metrics:\n  m1: {relation: orders, expr: SUM(1)}\n"
    )
    server = create_mcp_server(tmp_path)
    async with connected_session(server) as client:
        result = await client.call_tool("get_schema", {})
    text = result.content[0].text
    assert not result.is_error, text
    assert "CLEAN_PK_SECRET_99" not in text
    assert "CLEAN_TOK_SECRET_88" not in text
    body = json.loads(text)
    assert "error" in body


@pytest.mark.anyio
async def test_get_schema_reports_an_unparseable_url_without_the_password(tmp_path):
    (tmp_path / "metrics.yaml").write_text(
        "source: {url: 'bad url//admin:HUNTER2_URL_SECRET@host/db'}\n"
        "relations:\n  orders: {table: orders}\n"
        "metrics:\n  m1: {relation: orders, expr: SUM(1)}\n"
    )
    server = create_mcp_server(tmp_path)
    async with connected_session(server) as client:
        result = await client.call_tool("get_schema", {})
    text = result.content[0].text
    assert not result.is_error, text
    assert "HUNTER2_URL_SECRET" not in text
    assert json.loads(text)["error"].startswith("cannot resolve dialect for source:")


@pytest.mark.anyio
async def test_get_schema_does_not_echo_interpolated_env_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_PW_REAL_SECRET", "envpw_super_secret_123")
    (tmp_path / "metrics.yaml").write_text(
        "source:\n"
        "  type: duckdb\n"
        "  connect_args:\n"
        "    private_key: ${env:TEST_PW_REAL_SECRET}\n"
        "relations:\n  orders: {table: orders}\n"
        "metrics:\n  m1: {relation: orders, expr: SUM(1)}\n"
    )
    server = create_mcp_server(tmp_path)
    async with connected_session(server) as client:
        result = await client.call_tool("get_schema", {})
    text = result.content[0].text
    assert not result.is_error, text
    assert "envpw_super_secret_123" not in text
    assert "error" in json.loads(text)


@pytest.mark.anyio
async def test_get_schema_connection_failure_is_a_verdict(tmp_path):
    """A real unreachable sqlite used to raise a protocol error from
    introspect()'s inspector fallback. Monkeypatching ConnectorError on
    _get_connector never exercised that path (#111 / #254). The file exists
    but is not a database: a missing path is refused before connecting (#515)."""
    (tmp_path / "x.db").write_text("this is not a sqlite database " * 8)
    (tmp_path / "metrics.yaml").write_text(
        "source: {type: sqlite, database: x.db}\n"
        "relations:\n  orders: {table: orders}\n"
        "metrics:\n  revenue: {relation: orders, expr: SUM(amount)}\n"
    )
    server = create_mcp_server(tmp_path)
    async with connected_session(server) as client:
        result = await client.call_tool("get_schema", {})
    assert not result.is_error, result.content
    body = json.loads(result.content[0].text)
    assert "error" in body
    assert "not a database" in body["error"].lower()


def _typo_duckdb_metrics(tmp_path):
    folder = tmp_path / ".sqldash"
    folder.mkdir()
    text = (
        "source: {type: duckdb, database: typo.duckdb}\n"
        "relations:\n  orders: {table: orders}\n"
        "metrics:\n  revenue: {title: Revenue, relation: orders, expr: SUM(amount)}\n"
    )
    (folder / "metrics.yaml").write_text(text)
    return text


@pytest.mark.anyio
async def test_get_schema_refuses_a_missing_duckdb_file(tmp_path):
    """get_schema connected first, so DuckDB created typo.duckdb and returned a
    green `tables: []` while `source test` said FAIL. #515."""
    _typo_duckdb_metrics(tmp_path)
    result = await call(create_mcp_server(tmp_path), "get_schema")
    assert result["error"].startswith("database file not found: typo.duckdb (resolved to ")
    assert "tables" not in result
    assert not list(tmp_path.rglob("*.duckdb"))


@pytest.mark.anyio
async def test_get_schema_gives_the_quoted_form_to_write_in_sql(tmp_path):
    """Names alone gave no hint that `my col` or `Mixed` must be quoted, so an
    agent writing SQL from get_schema got syntax or identifier errors."""
    with duckdb.connect(str(tmp_path / "q.duckdb")) as con:
        con.execute('CREATE TABLE "my table" (amount INT, "my col" INT, "Mixed" INT)')
    (tmp_path / "metrics.yaml").write_text(
        "source: {type: duckdb, database: q.duckdb}\n"
        "relations:\n  t: {table: t}\nmetrics:\n  m: {relation: t, expr: SUM(1)}\n"
    )
    result = await call(create_mcp_server(tmp_path), "get_schema")
    (table,) = result["tables"]
    assert table["sql"] == 'main."my table"'
    assert [(c["name"], c["sql"]) for c in table["columns"]] == [
        ("amount", "amount"),
        ("my col", '"my col"'),
        ("Mixed", '"Mixed"'),
    ]


@pytest.mark.anyio
async def test_validate_metrics_and_query_metric_refuse_a_missing_duckdb_file(tmp_path):
    """validate_metrics only said valid: false because its schema probe created an
    empty typo.duckdb to find no tables in. The verdict must come from the
    missing file, as it does for `sqldash lint`. #515."""
    text = _typo_duckdb_metrics(tmp_path)
    server = create_mcp_server(tmp_path)
    result = await call(server, "validate_metrics", {"yaml_text": text})
    assert result["valid"] is False, result
    assert any("database file not found: typo.duckdb" in e for e in result["errors"]), result
    assert not list(tmp_path.rglob("*.duckdb"))
    result = await call(server, "query_metric", {"name": "revenue"})
    assert "database file not found: typo.duckdb" in result["error"], result
    assert not list(tmp_path.rglob("*.duckdb"))


UNRESOLVABLE_SOURCES = {
    "env_password": (
        "type: postgres\nhost: localhost\ndatabase: proddb\nusername: reader\n"
        "password: ${env:SQLDASH_TEST_UNSET_PGPASS}\n",
        "SQLDASH_TEST_UNSET_PGPASS",
    ),
    "env_url": (
        "type: postgres\nurl: postgresql://reader:${env:SQLDASH_TEST_UNSET_PGPASS}@localhost/db\n",
        "SQLDASH_TEST_UNSET_PGPASS",
    ),
    "env_duckdb_database": (
        "type: duckdb\ndatabase: ${env:SQLDASH_TEST_UNSET_PGPASS}\n",
        "SQLDASH_TEST_UNSET_PGPASS",
    ),
    "missing_profile": (
        "type: postgres\nhost: localhost\ndatabase: proddb\nprofile: ghost\n",
        "profile 'ghost' not found",
    ),
    "malformed_profiles_file": (
        "type: postgres\nhost: localhost\ndatabase: proddb\nprofile: acme\n",
        "must be a mapping of profile name",
    ),
}


def _indent(block: str) -> str:
    return "".join(f"  {line}\n" for line in block.splitlines())


@pytest.mark.anyio
@pytest.mark.parametrize("case", sorted(UNRESOLVABLE_SOURCES))
async def test_unresolvable_source_is_the_same_verdict_on_every_tool(tmp_path, monkeypatch, case):
    """A SecretError raised while building the engine escaped get_schema as an
    isError crash with no structuredContent, while query_metric (whose worker
    catches everything) answered the same condition with {"error": ...} (#517)."""
    source, expected = UNRESOLVABLE_SOURCES[case]
    monkeypatch.delenv("SQLDASH_TEST_UNSET_PGPASS", raising=False)
    profiles = tmp_path / "profiles.yaml"
    if case == "malformed_profiles_file":
        profiles.write_text("- not a mapping\n")
    monkeypatch.setattr("sqldash.secrets.profiles_path", lambda: profiles)
    project = tmp_path / "project"
    project.mkdir()
    (project / "metrics.yaml").write_text(
        "source:\n" + _indent(source) + "relations:\n  orders: {table: public.orders}\n"
        "metrics:\n  revenue: {relation: orders, expr: SUM(amount)}\n"
    )
    (project / "d.yaml").write_text(
        "title: D\nsource:\n" + _indent(source) + "tiles: [{title: A, sql: 'SELECT 1 AS n'}]\n"
    )
    server = create_mcp_server(project, allow_sql=True)
    calls = [
        ("get_schema", {}),
        ("get_schema", {"source": "metrics.yaml"}),
        ("get_schema", {"source": "d.source"}),
        ("query_metric", {"name": "revenue"}),
        ("run_sql", {"sql": "SELECT 1"}),
    ]
    async with connected_session(server) as client:
        for tool, arguments in calls:
            result = await client.call_tool(tool, arguments)
            assert not result.is_error, (tool, arguments, result.content)
            assert result.structured_content is not None, (tool, arguments)
            assert json.loads(result.content[0].text) == result.structured_content
            assert expected in result.structured_content["error"], (tool, arguments)


@pytest.mark.anyio
async def test_duckdb_source_with_an_unloadable_url_is_the_same_verdict(tmp_path):
    """A `type: duckdb` source whose `url:` names another dialect built its engine
    outside the ConnectorError wrap every other type gets, so get_schema crashed
    with a protocol isError while query_metric answered {"error": ...} (#598)."""
    (tmp_path / "metrics.yaml").write_text(
        "source: {type: duckdb, url: 'nosuchdialect://u@127.0.0.1/db'}\n"
        "relations:\n  orders: {table: orders}\n"
        "metrics:\n  revenue: {relation: orders, expr: SUM(amount)}\n"
    )
    server = create_mcp_server(tmp_path)
    verdicts = []
    async with connected_session(server) as client:
        for tool, arguments in [
            ("get_schema", {}),
            ("get_schema", {"source": "metrics.yaml"}),
            ("query_metric", {"name": "revenue"}),
        ]:
            result = await client.call_tool(tool, arguments)
            assert not result.is_error, (tool, arguments, result.content)
            verdicts.append(result.structured_content)
    assert verdicts[0]["error"].startswith("cannot create engine for source: ")
    assert "nosuchdialect" in verdicts[0]["error"]
    assert verdicts == [verdicts[0]] * 3


@pytest.mark.anyio
async def test_get_schema(demo_dir):
    server = create_mcp_server(demo_dir)
    result = await call(server, "get_schema")
    assert result["source"] == "metrics.yaml"
    tables = {t["name"]: t for t in result["tables"]}
    assert "orders" in tables
    assert any(c["name"] == "region" for c in tables["orders"]["columns"])
    bad = await call(server, "get_schema", {"source": "nope"})
    assert "unknown source" in bad["error"]


@pytest.fixture(scope="module")
def two_repos(tmp_path_factory):
    base = tmp_path_factory.mktemp("mcp-ws")
    for repo in ("acme", "beta"):
        create_demo(base / repo)
    return [(repo, base / repo / ".sqldash") for repo in ("acme", "beta")]


@pytest.mark.anyio
async def test_workspace_mcp_namespaced(two_repos):
    server = create_mcp_server(workspace=two_repos)
    listed = await call(server, "list_metrics")
    names = {m["name"] for m in listed["metrics"]}
    assert {"acme/revenue", "beta/revenue"} <= names
    result = await call(server, "query_metric", {"name": "beta/revenue", "dimensions": ["region"]})
    assert {row[0] for row in result["rows"]} == {"us", "eu", "apac"}
    assert "revenue" in result["sql"]
    schema = await call(server, "get_schema", {"source": "acme/demo.source"})
    assert any(t["name"] == "orders" for t in schema["tables"])
    aliased = await call(server, "get_schema", {"source": "dashboard:acme/demo"})
    assert aliased["source"] == "acme/demo.source"


@pytest.mark.anyio
async def test_validate_metrics_good_and_bad(demo_dir):
    server = create_mcp_server(demo_dir)
    good = (
        'source: {type: duckdb, database: ":memory:"}\n'
        "relations:\n"
        "  events: {sql: \"SELECT 1 AS amount, 'us' AS region, DATE '2026-01-01' AS day\"}\n"
        "metrics:\n"
        "  total:\n"
        "    relation: events\n"
        "    expr: SUM(amount)\n"
        "    dimensions: [{name: region}]\n"
        "    time_dimension: {name: day, grain: day}\n"
    )
    result = await call(server, "validate_metrics", {"yaml_text": good})
    assert result["valid"] is True, result
    assert "SUM(amount)" in result["compiled_sql"]["total"]
    assert result["schema_checked"] is True

    bad_dim = good.replace("[{name: region}]", "[{name: nope_col}]")
    result = await call(server, "validate_metrics", {"yaml_text": bad_dim})
    assert result["valid"] is False
    assert any("nope_col" in f for f in result["schema_findings"])

    unparseable = "metrics: [not, a, mapping]"
    result = await call(server, "validate_metrics", {"yaml_text": unparseable})
    assert result["valid"] is False

    reserved = good.replace("  total:", "  trailing:")
    result = await call(server, "validate_metrics", {"yaml_text": reserved})
    assert result["valid"] is False
    assert any("metric name 'trailing' is a SQL reserved word" in e for e in result["errors"])


@pytest.mark.anyio
async def test_validate_dashboard(demo_dir):
    server = create_mcp_server(demo_dir)
    good = (
        "title: Candidate\n"
        'source: {type: duckdb, database: ":memory:"}\n'
        "tiles:\n"
        "  - title: Revenue\n"
        "    metric: revenue\n"
        "    size: 3x2\n"
    )
    result = await call(server, "validate_dashboard", {"yaml_text": good})
    assert result["valid"] is True, result
    assert result["tiles"][0]["kind"] == "metric"

    unknown = good.replace("metric: revenue", "metric: nope")
    result = await call(server, "validate_dashboard", {"yaml_text": unknown})
    assert result["valid"] is False
    assert any("nope" in e for e in result["errors"])

    broken = "title: X\nsource: {typ: duckdb}\ntiles: []\n"
    result = await call(server, "validate_dashboard", {"yaml_text": broken})
    assert result["valid"] is False


@pytest.mark.anyio
async def test_validate_metrics_catches_expr_typo(demo_dir):
    server = create_mcp_server(demo_dir)
    candidate = (
        'source: {type: duckdb, database: ":memory:", attach_files: true}\n'
        "relations:\n"
        "  orders: {table: orders}\n"
        "metrics:\n"
        "  aov: {relation: orders, expr: AVG(amout), dimensions: [{name: region}]}\n"
    )
    result = await call(server, "validate_metrics", {"yaml_text": candidate})
    assert result["valid"] is False
    assert any("amout" in f or "compiled SQL fails" in f for f in result["schema_findings"])


@pytest.mark.anyio
async def test_query_metric_runtime_failure_stays_in_band(demo_dir, monkeypatch):
    from sqldash.connectors.base import ConnectorError
    from sqldash.execution import ExecutionRegistry

    def boom(self, *args, **kwargs):
        raise ConnectorError("warehouse exploded")

    monkeypatch.setattr(ExecutionRegistry, "run_sync", boom)
    server = create_mcp_server(demo_dir)
    async with connected_session(server) as client:
        result = await client.call_tool("query_metric", {"name": "revenue"})
        assert not result.is_error
        payload = json.loads(result.content[0].text)
        assert "warehouse exploded" in payload["error"]


@pytest.mark.anyio
async def test_get_dashboards_surfaces_a_broken_file(tmp_path):
    """HTTP and CLI list broken dashboards as error rows. get_dashboards used
    iter_loaded() and hid them — the same file disappearing depending on
    which surface you asked."""
    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "broken.yaml").write_text("title: [unbalanced\n")
    listed = await call(create_mcp_server(tmp_path / ".sqldash"), "get_dashboards")
    names = {d["name"] for d in listed["dashboards"]}
    assert "demo" in names
    broken = next(d for d in listed["dashboards"] if d["name"] == "broken")
    assert "error" in broken


@pytest.mark.anyio
async def test_list_sources_includes_named_sources(tmp_path):
    (tmp_path / "multi.yaml").write_text(
        "title: Multi\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "sources:\n"
        "  extra: {type: duckdb, database: ':memory:'}\n"
        "  source: {type: duckdb, database: ':memory:'}\n"
        "tiles: [{title: A, sql: SELECT 1}]\n"
    )
    server = create_mcp_server(tmp_path)
    listed = await call(server, "list_sources")
    assert "multi.source" in listed["sources"]
    assert "multi.sources.extra" in listed["sources"]
    assert "multi.sources.source" in listed["sources"]
    assert "dashboard:multi" not in listed["sources"]


@pytest.mark.anyio
async def test_get_schema_auto_selects_lone_main_source(tmp_path):
    (tmp_path / "solo.yaml").write_text(
        "title: Solo\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "sources:\n"
        "  extra: {type: duckdb, database: ':memory:'}\n"
        "tiles: [{title: A, sql: 'SELECT 1 AS n'}]\n"
    )
    server = create_mcp_server(tmp_path)
    result = await call(server, "get_schema")
    assert result["source"] == "solo.source"


@pytest.mark.anyio
async def test_workspace_get_schema_auto_selects_lone_dashboard(tmp_path_factory):
    base = tmp_path_factory.mktemp("mcp-lone")
    for repo in ("acme", "beta"):
        create_demo(base / repo)
    (base / "beta" / ".sqldash" / "demo.yaml").unlink()
    server = create_mcp_server(
        workspace=[(repo, base / repo / ".sqldash") for repo in ("acme", "beta")]
    )
    result = await call(server, "get_schema")
    assert result["source"] == "acme/demo.source"


@pytest.mark.anyio
async def test_validate_dashboard_probes_tile_sql(demo_dir):
    """The agent-facing validator must be at least as strict as `sqldash lint`,
    including against the warehouse — these all used to return valid: true."""
    server = create_mcp_server(demo_dir)
    cases = {
        "no_such_table": 'title: T\nsource: {type: duckdb, database: ":memory:"}\n'
        'tiles: [{title: A, chart: table, sql: "SELECT * FROM no_such_table"}]\n',
        "bad column": "title: T\nsource: {type: duckdb, attach_files: true}\n"
        'tiles: [{title: A, chart: bar, sql: "SELECT regionn FROM orders"}]\n',
        "invalid SQL": 'title: T\nsource: {type: duckdb, database: ":memory:"}\n'
        'tiles: [{title: A, chart: table, sql: "SELEKT oops FROM"}]\n',
    }
    for label, text in cases.items():
        result = await call(server, "validate_dashboard", {"yaml_text": text})
        assert result["valid"] is False, f"{label} should not validate: {result}"
        assert any("fails against the source" in e for e in result["errors"]), result


@pytest.mark.anyio
async def test_validate_dashboard_names_a_write_tile_not_a_parser_error(demo_dir):
    """The probe wrapper used to call DELETE a parser error; runtime executed it. #500."""
    server = create_mcp_server(demo_dir)
    text = (
        "title: DML check\n"
        "source: {type: duckdb, attach_files: true}\n"
        "tiles:\n"
        "  - title: cleanup\n"
        "    chart: table\n"
        "    sql: |\n"
        "      DELETE FROM orders WHERE region = 'us'\n"
    )
    result = await call(server, "validate_dashboard", {"yaml_text": text})
    assert result["valid"] is False, result
    joined = " | ".join(result["errors"])
    assert "tile 'cleanup' is a write statement" in joined
    assert "syntax error" not in joined.lower()
    assert "fails against the source" not in joined


@pytest.mark.anyio
async def test_validate_dashboard_runs_the_lint_checks(demo_dir):
    server = create_mcp_server(demo_dir)
    text = (
        "title: X\n"
        "source: {type: duckdb, attach_files: true}\n"
        "tiles:\n"
        "  - title: A\n"
        "    metric: {name: revenue, dimensions: [nope_dim]}\n"
        "  - title: C\n"
        '    sql: "SELECT amount FROM orders WHERE x = {{ undefined_param }}"\n'
    )
    result = await call(server, "validate_dashboard", {"yaml_text": text})
    assert result["valid"] is False
    joined = " | ".join(result["errors"])
    assert "has no dimension 'nope_dim'" in joined
    assert "valid: category, region" in joined
    assert "undefined_param" in joined


@pytest.mark.anyio
async def test_validate_dashboard_returns_rendered_sql(demo_dir):
    server = create_mcp_server(demo_dir)
    text = (
        "title: T\n"
        "source: {type: duckdb, attach_files: true}\n"
        "filters: [{name: region, type: select, options: [us, eu]}]\n"
        "tiles:\n"
        "  - title: A\n"
        "    chart: table\n"
        "    sql: |\n"
        "      SELECT region FROM orders WHERE 1 = 1\n"
        "      {% if region %}AND region = {{ region }}{% endif %}\n"
    )
    result = await call(server, "validate_dashboard", {"yaml_text": text})
    assert result["valid"] is True, result
    assert result["sql_checked"] is True
    assert "SELECT region FROM orders" in result["rendered_sql"]["a"]


@pytest.mark.anyio
async def test_validate_dashboard_accepts_a_tile_inside_an_inactive_if_block(demo_dir):
    """`sqldash lint` passed this file while validate_dashboard said
    `Parser Error: syntax error at or near ")"` — the probe wrapped the blank
    render of an inactive block. Three surfaces, one verdict. #284."""
    server = create_mcp_server(demo_dir)
    text = (
        "title: Optional query\n"
        "source: {type: duckdb, attach_files: true}\n"
        "filters:\n"
        "  - {name: region, type: select, label: Region, options: [us, eu, apac]}\n"
        "tiles:\n"
        "  - title: Region total\n"
        "    size: 6x3\n"
        "    sql: |\n"
        "      {% if region %}SELECT region, SUM(amount) AS total\n"
        "      FROM orders WHERE region = {{ region }} GROUP BY 1{% endif %}\n"
    )
    result = await call(server, "validate_dashboard", {"yaml_text": text})
    assert result["valid"] is True, result["errors"]
    assert result["sql_checked"] is True
    assert result["rendered_sql"]["region_total"].strip() == ""

    result = await call(
        server, "validate_dashboard", {"yaml_text": text.replace("SUM(amount)", "SUM(amountt)")}
    )
    assert result["valid"] is False
    assert any("with filters active" in e for e in result["errors"]), result["errors"]


@pytest.mark.anyio
async def test_validate_dashboard_can_skip_the_warehouse(demo_dir):
    server = create_mcp_server(demo_dir)
    text = (
        'title: T\nsource: {type: duckdb, database: ":memory:"}\n'
        'tiles: [{title: A, chart: table, sql: "SELECT * FROM no_such_table"}]\n'
    )
    result = await call(server, "validate_dashboard", {"yaml_text": text, "check_sql": False})
    assert result["valid"] is True
    assert result["sql_checked"] is False


@pytest.mark.anyio
async def test_validate_dashboard_works_in_workspace_mode(two_repos):
    """WorkspaceLayer has no project_metrics(); reaching for it made every
    workspace validate_dashboard call a protocol-level crash."""
    server = create_mcp_server(workspace=two_repos)
    text = (
        "title: T\nsource: {type: duckdb, attach_files: true}\n"
        "tiles: [{title: A, metric: not_a_metric}]\n"
    )
    result = await call(server, "validate_dashboard", {"yaml_text": text})
    assert result["valid"] is False
    assert any("unknown metric" in e for e in result["errors"])


@pytest.mark.anyio
async def test_validate_dashboard_rejects_ambiguous_workspace_metric(two_repos):
    """Both repos define `revenue`, so the bare name does not resolve —
    WorkspaceLayer.resolve refuses it, so the validator must too."""
    server = create_mcp_server(workspace=two_repos)
    text = (
        "title: T\nsource: {type: duckdb, attach_files: true}\n"
        "tiles: [{title: A, metric: revenue}]\n"
    )
    result = await call(server, "validate_dashboard", {"yaml_text": text})
    assert result["valid"] is False, result
    assert any("more than one repo" in e for e in result["errors"]), result
    assert any("acme/revenue, beta/revenue" in e for e in result["errors"]), result


@pytest.mark.anyio
async def test_validate_dashboard_accepts_unambiguous_workspace_metric(tmp_path_factory):
    base = tmp_path_factory.mktemp("mcp-uniq")
    for repo in ("acme", "beta"):
        create_demo(base / repo)
    metrics = base / "acme" / ".sqldash" / "metrics.yaml"
    metrics.write_text(
        metrics.read_text() + "\n  only_here:\n    relation: orders\n    expr: COUNT(*)\n"
    )
    server = create_mcp_server(workspace=[(r, base / r / ".sqldash") for r in ("acme", "beta")])
    text = (
        "title: T\nsource: {type: duckdb, database: ':memory:'}\n"
        "tiles: [{title: A, metric: only_here}]\n"
    )
    result = await call(server, "validate_dashboard", {"yaml_text": text})
    assert result["valid"] is True, result


@pytest.mark.anyio
async def test_validate_dashboard_probes_metric_tiles(demo_dir):
    """A metric tile pointing at a missing table must fail here, not at first paint."""
    server = create_mcp_server(demo_dir)
    text = (
        'title: T\nsource: {type: duckdb, database: ":memory:"}\n'
        'metrics:\n  m: {table: no_such_table, expr: "SUM(x)"}\n'
        "tiles: [{title: A, metric: m}]\n"
    )
    result = await call(server, "validate_dashboard", {"yaml_text": text})
    assert result["valid"] is False, result
    assert any("fails against the source" in e for e in result["errors"]), result


@pytest.mark.anyio
async def test_validate_dashboard_probes_conditional_blocks(demo_dir):
    """A typo'd column inside {% if filter %} is invisible on the inactive path:
    the block is stripped before the probe sees it, so the dashboard validated
    clean and then crashed the moment a user selected that filter."""
    server = create_mcp_server(demo_dir)
    text = (
        "title: Cond\n"
        "source: {type: duckdb, attach_files: true}\n"
        "filters: [{name: region, type: select, options: [us, eu]}]\n"
        "tiles:\n"
        "  - title: Bad col in conditional\n"
        "    chart: table\n"
        "    sql: |\n"
        "      SELECT region FROM orders WHERE 1=1\n"
        "      {% if region %}AND nonexistent_column = {{ region }}{% endif %}\n"
    )
    result = await call(server, "validate_dashboard", {"yaml_text": text})
    assert result["valid"] is False, result
    assert any("with filters active" in e for e in result["errors"]), result
    assert any("nonexistent_column" in e for e in result["errors"]), result


@pytest.mark.anyio
async def test_validate_dashboard_allows_a_good_conditional(demo_dir):
    """The active-path probe must not reject a dashboard that is actually fine."""
    server = create_mcp_server(demo_dir)
    text = (
        "title: Cond OK\n"
        "source: {type: duckdb, attach_files: true}\n"
        "filters: [{name: region, type: select, options: [us, eu]}]\n"
        "tiles:\n"
        "  - title: Good conditional\n"
        "    chart: table\n"
        "    sql: |\n"
        "      SELECT region FROM orders WHERE 1=1\n"
        "      {% if region %}AND region = {{ region }}{% endif %}\n"
    )
    result = await call(server, "validate_dashboard", {"yaml_text": text})
    assert result["valid"] is True, result


@pytest.mark.anyio
async def test_validate_dashboard_reports_one_error_per_broken_tile(demo_dir):
    """A tile broken outside its conditional fails both probe passes; reporting
    both reads as two defects to an agent consuming errors[]."""
    server = create_mcp_server(demo_dir)
    text = (
        "title: Dup\n"
        'source: {type: duckdb, database: ":memory:"}\n'
        "filters: [{name: region, type: select, options: [us, eu]}]\n"
        "tiles:\n"
        "  - title: A\n"
        "    chart: table\n"
        "    sql: |\n"
        "      SELECT * FROM no_such_table WHERE 1=1\n"
        "      {% if region %}AND region = {{ region }}{% endif %}\n"
    )
    result = await call(server, "validate_dashboard", {"yaml_text": text})
    assert result["valid"] is False
    tile_errors = [e for e in result["errors"] if e.startswith("tile 'a'")]
    assert len(tile_errors) == 1, tile_errors


@pytest.mark.anyio
async def test_validate_dashboard_probes_options_sql(demo_dir):
    """options_sql runs against the warehouse at page load, so a bad column there
    breaks the dropdown on a dashboard that otherwise validates clean."""
    server = create_mcp_server(demo_dir)
    text = (
        "title: Opt\n"
        "source: {type: duckdb, attach_files: true}\n"
        "filters:\n"
        "  - {name: region, type: select, "
        'options_sql: "SELECT DISTINCT no_such_column FROM orders"}\n'
        'tiles: [{title: A, chart: table, sql: "SELECT region FROM orders"}]\n'
    )
    result = await call(server, "validate_dashboard", {"yaml_text": text})
    assert result["valid"] is False, result
    assert any("options_sql fails against the source" in e for e in result["errors"]), result


@pytest.mark.anyio
async def test_validate_dashboard_accepts_good_options_sql(demo_dir):
    server = create_mcp_server(demo_dir)
    text = (
        "title: Opt OK\n"
        "source: {type: duckdb, attach_files: true}\n"
        "filters:\n"
        '  - {name: region, type: select, options_sql: "SELECT DISTINCT region FROM orders"}\n'
        'tiles: [{title: A, chart: table, sql: "SELECT region FROM orders"}]\n'
    )
    result = await call(server, "validate_dashboard", {"yaml_text": text})
    assert result["valid"] is True, result


@pytest.mark.anyio
async def test_validate_dashboard_handles_options_sql_with_params(demo_dir):
    """bind_sql raises for a placeholder it has no value for, so probing an
    options_sql containing {{ params }} crashed the tool — when the linter had
    already judged the same YAML invalid with an actionable message."""
    server = create_mcp_server(demo_dir)
    text = (
        "title: Opt\n"
        "source: {type: duckdb, attach_files: true}\n"
        "filters:\n"
        "  - {name: region, type: select, "
        'options_sql: "SELECT region FROM orders WHERE region = {{ region }}"}\n'
        'tiles: [{title: A, chart: table, sql: "SELECT region FROM orders"}]\n'
    )
    result = await call(server, "validate_dashboard", {"yaml_text": text})
    assert result["valid"] is False, result
    assert any("cannot reference" in e for e in result["errors"]), result


@pytest.mark.anyio
async def test_validate_dashboard_survives_an_unresolvable_dialect(demo_dir):
    """paramstyle_for raises for a dialect SQLAlchemy cannot load, which crashed
    the tool. The linter already names the real problem, so the probe skips the
    source and the caller gets a verdict rather than a protocol error."""
    server = create_mcp_server(demo_dir)
    text = (
        "title: Bad dialect\n"
        "source: {type: notarealdb, host: h, database: d}\n"
        'tiles: [{title: A, chart: table, sql: "SELECT 1 AS n"}]\n'
    )
    result = await call(server, "validate_dashboard", {"yaml_text": text})
    assert result["valid"] is False, result
    assert any("unknown database type" in e for e in result["errors"]), result
    assert not any("cannot resolve the dialect" in e for e in result["errors"]), (
        "the linter already reported this; the probe should not repeat it"
    )


@pytest.mark.anyio
async def test_validate_metrics_agrees_with_the_linter(demo_dir):
    """Validation surfaces agree.

    A snowflake source with no `account` is an ERROR for `sqldash lint`, which
    fails CI on it. validate_metrics used to file that under `lint` and still
    report valid: true — so an agent told to trust `valid` would write the file
    and only find out from CI. The two surfaces must reach the same verdict.
    """
    from sqldash.lint import lint_source
    from sqldash.semantics.layer import parse_metrics_file

    broken = (
        "source:\n"
        "  type: snowflake\n"  # no account
        "  database: ANALYTICS\n"
        "  username: dev\n"
        "relations:\n"
        "  orders: {table: orders}\n"
        "metrics:\n"
        "  revenue:\n"
        "    relation: orders\n"
        "    expr: SUM(amount)\n"
        "    time_dimension: {name: order_date, grain: day}\n"
    )

    # What the linter says, i.e. what CI would do.
    findings = lint_source(parse_metrics_file(broken).source, "metrics.yaml", "source")
    linter_errors = [f.message for f in findings if f.level == "error"]
    assert linter_errors, "expected the linter to reject a snowflake source with no account"

    # What the agent-facing validator says. check_schema=False so this needs no driver.
    result = await call(
        create_mcp_server(demo_dir),
        "validate_metrics",
        {"yaml_text": broken, "check_schema": False},
    )
    assert result["valid"] is False, result
    # The same message, not merely something mentioning "account".
    assert set(linter_errors) <= set(result["errors"]), result


@pytest.fixture
def ambiguous_project(tmp_path):
    for name, mult in (("a", 2), ("b", 100)):
        (tmp_path / f"{name}.yaml").write_text(
            f"title: Dash {name}\n"
            "source: {type: duckdb, database: ':memory:'}\n"
            "metrics:\n"
            f"  inline_rev: {{sql: 'SELECT 1 AS amount', expr: 'SUM(amount) * {mult}'}}\n"
            "queries: {q: 'SELECT 1'}\n"
            "tiles: [{id: w, metric: inline_rev}]\n"
        )
    return tmp_path


@pytest.mark.anyio
async def test_get_metric_refuses_an_ambiguous_name_without_crashing(ambiguous_project):
    """A refusal must arrive as a readable result, not a protocol error — the
    'helper raising outside the try' shape."""
    result = await call(create_mcp_server(ambiguous_project), "get_metric", {"name": "inline_rev"})
    assert "more than one dashboard" in result["error"]


@pytest.mark.anyio
async def test_mcp_dashboard_scope_resolves_each_definition(ambiguous_project):
    """The refusal tells the caller to scope it, so MCP has to offer a way."""
    server = create_mcp_server(ambiguous_project)
    for dash, expected in (("a", "* 2"), ("b", "* 100")):
        got = await call(server, "get_metric", {"name": "inline_rev", "dashboard": dash})
        assert expected in got["expr"], got
        ran = await call(server, "query_metric", {"name": "inline_rev", "dashboard": dash})
        assert "error" not in ran, ran


@pytest.mark.anyio
async def test_query_metric_unknown_filter_is_an_error_when_dashboard_scoped(demo_dir):
    result = await call(
        create_mcp_server(demo_dir),
        "query_metric",
        {"name": "revenue", "dashboard": "demo", "filters": {"regoin": "us"}},
    )
    assert "error" in result
    assert "unknown filter dimension" in result["error"]
    assert "regoin" in result["error"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "value",
    [{"$gt": "a"}, {"op": "in", "value": {"a": 1}}, ["eu", {"a": 1}]],
)
async def test_query_metric_unsupported_filter_shape_is_an_error(demo_dir, value):
    """#658: an agent that passes a mongo-ish filter got either "has no value" or a
    raw cast error out of the warehouse. Same refusal as HTTP, so the three surfaces
    tell an agent the same thing about the same input."""
    result = await call(
        create_mcp_server(demo_dir),
        "query_metric",
        {"name": "revenue", "dimensions": ["region"], "filters": {"region": value}},
    )
    assert "error" in result
    assert "region" in result["error"]
    assert "accepted shapes" in result["error"]
    assert "has no value" not in result["error"]


@pytest.mark.anyio
async def test_query_metric_explicit_op_with_a_list_is_an_error(demo_dir):
    """#668: an agent sending `{op: ">", value: [...]}` got IN rows back as a
    success. It hears the same refusal as HTTP now, and a bare list or a spelled
    `=` still means IN."""
    server = create_mcp_server(demo_dir)
    refused = await call(
        server,
        "query_metric",
        {
            "name": "revenue",
            "dimensions": ["region"],
            "filters": {"region": {"op": ">", "value": ["eu", "us"]}},
        },
    )
    assert "op '>' with a list value" in refused.get("error", ""), refused
    listed = await call(
        server,
        "query_metric",
        {"name": "revenue", "dimensions": ["region"], "filters": {"region": ["eu", "us"]}},
    )
    assert "error" not in listed, listed
    assert sorted(row[0] for row in listed["rows"]) == ["eu", "us"]
    spelled = await call(
        server,
        "query_metric",
        {
            "name": "revenue",
            "dimensions": ["region"],
            "filters": {"region": {"op": "=", "value": ["eu", "us"]}},
        },
    )
    assert spelled["rows"] == listed["rows"], spelled


@pytest.mark.anyio
async def test_query_metric_dashboard_applies_daterange_default(demo_dir):
    """#269: dashboard= used to only disambiguate inline names, so a scoped
    query_metric returned all-time while the tile used last_60_days."""
    server = create_mcp_server(demo_dir)
    all_time = await call(server, "query_metric", {"name": "revenue"})
    scoped = await call(server, "query_metric", {"name": "revenue", "dashboard": "demo"})
    assert "error" not in all_time, all_time
    assert "error" not in scoped, scoped
    assert scoped["rows"][0][0] < all_time["rows"][0][0]


@pytest.mark.anyio
async def test_query_metric_compare_returns_prior_window(demo_dir):
    result = await call(
        create_mcp_server(demo_dir),
        "query_metric",
        {
            "name": "revenue",
            "dashboard": "demo",
            "start": "2026-06-27",
            "end": "2026-08-26",
            "compare": "previous_period",
        },
    )
    assert "error" not in result, result
    assert result["compare"]["window"] == {"start": "2026-04-27", "end": "2026-06-26"}
    assert result["compare"]["delta"]["current"] == result["rows"][0][0]
    assert result["compare"]["delta"]["previous"] == result["compare"]["rows"][0][0]


@pytest.mark.anyio
async def test_query_metric_compare_delta_is_null_when_grained(demo_dir):
    result = await call(
        create_mcp_server(demo_dir),
        "query_metric",
        {
            "name": "revenue",
            "dashboard": "demo",
            "start": "2026-06-27",
            "end": "2026-08-26",
            "grain": "month",
            "compare": "previous_period",
        },
    )
    assert "error" not in result, result
    assert result["compare"]["delta"] is None
    assert result["compare"]["rows"]


@pytest.mark.anyio
async def test_query_metric_compare_without_a_range_is_an_error(demo_dir):
    result = await call(
        create_mcp_server(demo_dir),
        "query_metric",
        {"name": "revenue", "compare": "previous_period"},
    )
    assert "error" in result
    assert "time range" in result["error"]


@pytest.mark.anyio
async def test_query_metric_compare_with_only_a_start_asks_for_the_end(demo_dir):
    result = await call(
        create_mcp_server(demo_dir),
        "query_metric",
        {"name": "revenue", "start": "-30d", "compare": "previous_period"},
    )
    assert result["error"] == (
        "compare 'previous_period' needs both a start and an end, got only a start: "
        "pass an end, e.g. 'today'. A range open on one side has no length to shift"
    )


@pytest.mark.anyio
async def test_query_metric_compare_scoped_to_a_dashboard_names_what_it_lacks(tmp_path):
    """#516: a dashboard with no daterange filter was told to "run inside a
    dashboard with a daterange filter"; a metric with no time_dimension was
    told to pass start/end, which cannot help either."""
    create_demo(tmp_path)
    folder = tmp_path / ".sqldash"
    with (folder / "metrics.yaml").open("a") as f:
        f.write("  flat_revenue: {relation: orders, expr: SUM(amount)}\n")
    (folder / "bad4.yaml").write_text(
        "title: Bad four\nsource: {type: duckdb, attach_files: true}\n"
        "tiles:\n  - title: T\n    metric: {name: revenue, compare: previous_period}\n"
    )
    server = create_mcp_server(tmp_path)
    no_filter = await call(
        server,
        "query_metric",
        {"name": "revenue", "dashboard": "bad4", "compare": "previous_period"},
    )
    assert no_filter["error"] == (
        "compare 'previous_period' needs a time range — the dashboard has no daterange "
        "filter, so pass start/end or add one"
    )
    no_time = await call(
        server,
        "query_metric",
        {"name": "flat_revenue", "dashboard": "demo", "compare": "yoy"},
    )
    assert no_time["error"] == (
        "compare 'yoy' needs a time range — metric 'flat_revenue' has no "
        "time_dimension, so there is no window to shift"
    )


@pytest.mark.anyio
@pytest.mark.parametrize("token", ["-30d", "mtd", "ytd", "today"])
async def test_mcp_query_metric_accepts_relative_dates(demo_dir, token):
    """#89's MCP half. The helper and CLI tests both pass while this path is
    broken — the wiring is per-surface, so it needs its own cover."""
    result = await call(
        create_mcp_server(demo_dir), "query_metric", {"name": "revenue", "start": token}
    )
    assert "error" not in result, result
    assert "WHERE" in result["sql"].upper()


@pytest.mark.anyio
async def test_mcp_empty_end_is_no_bound_rather_than_an_empty_one(demo_dir):
    """An explicitly empty end used to survive into the compiled WHERE clause,
    where it matches nothing — a silent empty result rather than an error."""
    result = await call(
        create_mcp_server(demo_dir),
        "query_metric",
        {"name": "revenue", "start": "-30d", "end": ""},
    )
    assert "error" not in result, result
    assert result["rows"], "an empty end bound silently filtered everything out"


@pytest.mark.anyio
@pytest.mark.parametrize("tool", ["get_metric", "query_metric"])
async def test_mcp_metric_tools_refuse_a_bad_dashboard_as_a_verdict(demo_dir, tool):
    """`dashboard` names a file, so a typo raises a store error, not a semantic
    one — and an uncaught raise in a tool body is a protocol error rather than
    an answer the agent can read and correct. Same standard as the ambiguity
    refusal; the CLI already met it.
    """
    server = create_mcp_server(demo_dir / ".sqldash")
    async with connected_session(server) as client:
        result = await client.call_tool(tool, {"name": "revenue", "dashboard": "no_such_dash"})
    assert not result.is_error, result.content
    assert "no_such_dash" in json.loads(result.content[0].text)["error"]


def _colliding_project(tmp_path):
    for n in ("a", "b"):
        (tmp_path / f"{n}.yaml").write_text(
            f"title: Dash {n}\n"
            "source: {type: duckdb, database: ':memory:'}\n"
            "metrics:\n  inline_rev: {sql: 'SELECT 1 AS amount', expr: 'SUM(amount)'}\n"
            "queries: {q: 'SELECT 1'}\n"
            "tiles: [{id: w, metric: inline_rev}]\n"
        )
    return tmp_path


@pytest.mark.anyio
async def test_validate_dashboard_agrees_with_lint_on_inline_collisions(tmp_path):
    """The server instructions tell agents to ALWAYS validate before writing, and
    lint_project makes this state fail CI — so a `valid: true` here would send an
    agent to write the exact file CI rejects. The validation surfaces must agree.
    """
    root = _colliding_project(tmp_path)
    server = create_mcp_server(root)
    async with connected_session(server) as client:
        result = await client.call_tool(
            "validate_dashboard", {"yaml_text": (root / "b.yaml").read_text(), "check_sql": False}
        )
    payload = json.loads(result.content[0].text)
    assert payload["valid"] is False
    assert any("inline_rev" in e and "already defined" in e for e in payload["errors"]), payload


@pytest.mark.anyio
async def test_validate_dashboard_does_not_collide_a_dashboard_with_itself(tmp_path):
    """Editing an existing dashboard is the common case; its own stored copy must
    not count as the other definer."""
    root = _colliding_project(tmp_path)
    (root / "a.yaml").unlink()
    server = create_mcp_server(root)
    async with connected_session(server) as client:
        result = await client.call_tool(
            "validate_dashboard",
            {"yaml_text": (root / "b.yaml").read_text(), "check_sql": False, "name": "b"},
        )
    payload = json.loads(result.content[0].text)
    assert payload["valid"] is True, payload


@pytest.mark.anyio
async def test_validate_dashboard_does_not_collide_an_unnamed_candidate_with_itself(tmp_path):
    """An agent validating an edit before it writes has nothing to put in `name`
    yet (#645), and the one stored owner of the name may well be this same file.
    `sqldash lint` passes this very project, so a verdict here contradicts it.
    """
    from sqldash.lint import lint_project
    from sqldash.project.store import DashboardStore
    from sqldash.semantics import SemanticLayer

    root = _colliding_project(tmp_path)
    (root / "a.yaml").unlink()
    store = DashboardStore(root)
    assert [f for f in lint_project(store, SemanticLayer(store)) if f.level == "error"] == []

    server = create_mcp_server(root)
    async with connected_session(server) as client:
        result = await client.call_tool(
            "validate_dashboard", {"yaml_text": (root / "b.yaml").read_text(), "check_sql": False}
        )
    payload = json.loads(result.content[0].text)
    assert payload["valid"] is True, payload
    assert any("inline_rev" in n and "pass name" in n for n in payload["lint"]), payload


@pytest.mark.anyio
async def test_validate_dashboard_still_reports_a_named_new_file_taking_the_name(tmp_path):
    """The unnamed single-owner case became a note, so pin the collision it used
    to carry: a new dashboard really does shadow the stored one's inline metric,
    and with `name` there is nothing ambiguous left to excuse it."""
    root = _colliding_project(tmp_path)
    (root / "a.yaml").unlink()
    server = create_mcp_server(root)
    async with connected_session(server) as client:
        result = await client.call_tool(
            "validate_dashboard",
            {"yaml_text": (root / "b.yaml").read_text(), "check_sql": False, "name": "c"},
        )
    payload = json.loads(result.content[0].text)
    assert payload["valid"] is False, payload
    taken = [e for e in payload["errors"] if "inline_rev" in e and "already defined by b" in e]
    assert taken, payload


def test_lint_reports_one_file_per_collision(tmp_path):
    """The finding's file field is counted as a file checked, so a comma-joined
    list of files invented one: ' 3 file(s) checked' for a two-dashboard project."""
    from sqldash.lint import lint_project
    from sqldash.project.store import DashboardStore
    from sqldash.semantics import SemanticLayer

    store = DashboardStore(_colliding_project(tmp_path))
    findings = lint_project(store, SemanticLayer(store))
    collision = [f for f in findings if "more than one" in f.message]
    assert len(collision) == 1, findings
    assert "," not in collision[0].file
    assert "a.yaml" in collision[0].message
    assert "b.yaml" in collision[0].message


@pytest.mark.anyio
async def test_validate_dashboard_catches_collisions_in_workspace_mode_too(tmp_path):
    """The collision is intra-repo, and `name` carries the repo — so skipping the
    check in workspace mode blessed exactly what per-repo lint fails on, in the
    mode where cross-repo work makes collisions likelier.
    """
    repo = tmp_path / "repo1"
    repo.mkdir()
    for n in ("a", "b"):
        (repo / f"dash_{n}.yaml").write_text(
            f"title: Dash {n}\n"
            "source: {type: duckdb, database: ':memory:'}\n"
            "metrics:\n  inline_rev: {sql: 'SELECT 1 AS amount', expr: 'SUM(amount)'}\n"
            "queries: {q: 'SELECT 1'}\n"
            "tiles: [{id: w, metric: inline_rev}]\n"
        )
    server = create_mcp_server(workspace=[("repo1", repo)])
    async with connected_session(server) as client:
        for candidate in ("repo1/dash_b", "dash_b", "repo1/dash_b.yaml"):
            result = await client.call_tool(
                "validate_dashboard",
                {
                    "yaml_text": (repo / "dash_b.yaml").read_text(),
                    "check_sql": False,
                    "name": candidate,
                },
            )
            payload = json.loads(result.content[0].text)
            assert payload["valid"] is False, (candidate, payload)
            assert any("repo1/dash_a" in e for e in payload["errors"]), (candidate, payload)
            assert not any("dash_b" in e for e in payload["errors"]), (candidate, payload)


@pytest.mark.anyio
async def test_validate_dashboard_says_when_it_skipped_the_collision_check(tmp_path):
    """With several repos and no repo in `name`, nothing says which repo the file
    belongs to, and scanning all of them would invent cross-repo collisions. So
    the check is skipped — but silently skipping reads exactly like passing.
    """
    for r in ("repo1", "repo2"):
        (tmp_path / r).mkdir()
        (tmp_path / r / "d.yaml").write_text(
            "title: D\n"
            "source: {type: duckdb, database: ':memory:'}\n"
            "metrics:\n  m: {sql: 'SELECT 1 AS a', expr: 'SUM(a)'}\n"
            "tiles: [{id: w, metric: m}]\n"
        )
    server = create_mcp_server(
        workspace=[("repo1", tmp_path / "repo1"), ("repo2", tmp_path / "repo2")]
    )
    async with connected_session(server) as client:
        result = await client.call_tool(
            "validate_dashboard",
            {"yaml_text": (tmp_path / "repo1" / "d.yaml").read_text(), "check_sql": False},
        )
    payload = json.loads(result.content[0].text)
    assert any("skipped the cross-dashboard inline metric check" in n for n in payload["lint"])


BROKEN_LAYER = (
    "source: {type: duckdb, attach_files: true}\n"
    "relations:\n  t: {sql: 'SELECT 1 AS a'}\n"
    "metrics:\n"
    "  revenue: {relation: t, expr: SUM(a)}\n"
    "  broken: {derived: '{revenue} / {nope}'}\n"
)


@pytest.mark.anyio
async def test_list_metrics_reports_a_broken_definition_as_a_verdict(tmp_path):
    """One unresolvable definition anywhere in the layer took down the tool the
    server instructions tell agents to call first — as a protocol error, which
    an agent cannot read or act on, while get_metric and query_metric beside it
    already answered with {"error": ...}.
    """
    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "metrics.yaml").write_text(BROKEN_LAYER)
    server = create_mcp_server(tmp_path / ".sqldash")
    async with connected_session(server) as client:
        result = await client.call_tool("list_metrics", {})
    assert not result.is_error, result.content
    assert "nope" in json.loads(result.content[0].text)["error"]


@pytest.mark.anyio
@pytest.mark.parametrize("sql", ["SELECT * FROM no_such_table", "NOT VALID SQL AT ALL"])
async def test_run_sql_reports_a_bad_query_as_a_verdict(demo_dir, sql):
    """A bad query is the expected outcome of handing an agent raw SQL, and the
    tool most likely to produce one was the one that crashed the call."""
    server = create_mcp_server(demo_dir / ".sqldash", allow_sql=True)
    async with connected_session(server) as client:
        result = await client.call_tool("run_sql", {"sql": sql})
    assert not result.is_error, result.content
    assert json.loads(result.content[0].text)["error"]


@pytest.mark.anyio
async def test_a_healthy_layer_still_lists_its_metrics(demo_dir):
    """The guard must not swallow the normal answer."""
    payload = await call(create_mcp_server(demo_dir / ".sqldash"), "list_metrics")
    assert "error" not in payload, payload
    assert {m["name"] for m in payload["metrics"]} >= {"revenue", "order_count"}


@pytest.mark.anyio
async def test_a_programming_error_is_not_dressed_up_as_a_verdict(demo_dir, monkeypatch):
    """The catch is a domain-error tuple on purpose. A healthy layer never
    raises, so a happy-path test cannot tell that from `except Exception` — and
    a broad catch would report a bug in our own code as if the user's project
    were at fault, which is the harder failure to diagnose of the two.
    """
    server = create_mcp_server(demo_dir / ".sqldash")

    def boom():
        raise TypeError("bug in all_metrics")

    monkeypatch.setattr(server_module.SemanticLayer, "all_metrics", lambda self: boom())
    async with connected_session(server) as client:
        result = await client.call_tool("list_metrics", {})
    assert result.is_error, result.content


@pytest.mark.anyio
async def test_run_sql_reports_an_unloadable_first_dashboard(tmp_path):
    """Without a metrics.yaml, run_sql looks to the dashboards for a source,
    and an unloadable one must come back as an error payload, not a protocol
    error."""
    (tmp_path / "demo.yaml").write_text("title: [unbalanced\n")
    server = create_mcp_server(tmp_path, allow_sql=True)
    async with connected_session(server) as client:
        result = await client.call_tool("run_sql", {"sql": "SELECT 1"})
    assert not result.is_error, result.content
    assert json.loads(result.content[0].text)["error"]


@pytest.mark.anyio
async def test_run_sql_in_a_workspace_reports_an_unloadable_dashboard(tmp_path):
    """The same branch, reached the way a workspace always reaches it."""
    for repo, body in (("repo1", "title: [unbalanced\n"), ("repo2", "title: OK\n")):
        (tmp_path / repo).mkdir()
        (tmp_path / repo / "d.yaml").write_text(body)
    server = create_mcp_server(
        workspace=[("repo1", tmp_path / "repo1"), ("repo2", tmp_path / "repo2")],
        allow_sql=True,
    )
    async with connected_session(server) as client:
        result = await client.call_tool("run_sql", {"sql": "SELECT 1"})
    assert not result.is_error, result.content


CUMULATIVE_OVER_AVG = (
    "source: {type: duckdb, database: ':memory:'}\n"
    "relations:\n  t: {sql: \"SELECT 1 AS amount, DATE '2026-01-01' AS d\"}\n"
    "metrics:\n"
    "  avg_ticket:\n"
    "    relation: t\n"
    "    expr: AVG(amount)\n"
    "    cumulative: true\n"
    "    time_dimension: {name: d, grain: day}\n"
)


@pytest.mark.anyio
async def test_validate_metrics_reports_the_cumulative_warning_lint_gives(tmp_path):
    """`sqldash lint` warns that a running total over AVG accumulates into
    nonsense. validate_metrics reported the same file as valid with an empty
    lint list — and the server instructions tell agents to always run it before
    writing a file, so the warning reached whoever read CI output and never the
    agent actually writing the metric.
    """
    server = create_mcp_server(tmp_path)
    async with connected_session(server) as client:
        result = await client.call_tool("validate_metrics", {"yaml_text": CUMULATIVE_OVER_AVG})
    payload = json.loads(result.content[0].text)
    assert any("cumulative metric 'avg_ticket'" in line for line in payload["lint"]), payload
    # A warning, not an error — `sqldash lint` exits 0 for it, so this stays valid.
    assert payload["valid"] is True, payload


@pytest.mark.anyio
async def test_an_additive_cumulative_metric_is_not_warned_about(tmp_path):
    """The warning is about non-additive aggregates; a running SUM is the whole
    point of the feature."""
    server = create_mcp_server(tmp_path)
    async with connected_session(server) as client:
        result = await client.call_tool(
            "validate_metrics",
            {"yaml_text": CUMULATIVE_OVER_AVG.replace("AVG(amount)", "SUM(amount)")},
        )
    payload = json.loads(result.content[0].text)
    assert payload["lint"] == [], payload


@pytest.mark.anyio
async def test_query_metric_inverted_window_is_an_error_payload(demo_dir):
    """#361: MCP answered `[[null]]` with the SQL that produced it."""
    server = create_mcp_server(demo_dir)
    async with connected_session(server) as client:
        for args in (
            {"name": "revenue", "start": "2026-09-01", "end": "2026-01-01"},
            {
                "name": "revenue",
                "dashboard": "demo",
                "filters": {"dates_start": "2026-09-01", "dates_end": "2026-01-01"},
            },
        ):
            result = await client.call_tool("query_metric", args)
            assert not result.is_error
            payload = json.loads(result.content[0].text)
            assert "rows" not in payload, payload
            assert "date range is inverted" in payload["error"], payload
            assert "'2026-09-01' is after" in payload["error"], payload
    same_day = await call(
        server, "query_metric", {"name": "revenue", "start": "2026-01-01", "end": "2026-01-01"}
    )
    assert "rows" in same_day


@pytest.mark.anyio
async def test_query_metric_text_and_structured_content_agree_on_non_finite(tmp_path):
    """The text blob was json.dumps'd with allow_nan=True (`[[NaN]]`) while
    structuredContent carried null for the same rows. #359"""
    create_demo(tmp_path)
    metrics = tmp_path / ".sqldash" / "metrics.yaml"
    metrics.write_text(
        metrics.read_text() + "\n  nan_ratio:\n    title: NaN ratio\n    relation: orders\n"
        "    expr: \"MAX('NaN'::DOUBLE)\"\n"
    )
    server = create_mcp_server(tmp_path)
    async with connected_session(server) as client:
        result = await client.call_tool("query_metric", {"name": "nan_ratio"})
    assert not result.is_error, result.content
    text = result.content[0].text

    def refuse(constant):
        raise ValueError(f"non-finite literal {constant} is not JSON")

    parsed = json.loads(text, parse_constant=refuse)
    assert parsed["rows"] == [["NaN"]]
    assert parsed == result.structured_content


@pytest.mark.anyio
async def test_unknown_argument_is_refused_not_dropped(demo_dir):
    """mcp>=2 builds arg models with pydantic's default extra="ignore", so an
    agent that invents an argument name used to get a confident answer from a
    different scope: get_schema(dashboard=) returned the metrics.yaml tables and
    run_sql(source=) ran against the primary source. #395"""
    server = create_mcp_server(demo_dir, allow_sql=True)
    scoped = await call(server, "get_schema", {"dashboard": "demo"})
    assert scoped == {
        "error": "unknown argument 'dashboard' for get_schema — valid arguments: source"
    }
    refused = await call(server, "run_sql", {"sql": "SELECT 1 AS one", "dashboard": "zzz"})
    assert refused == {
        "error": "unknown argument 'dashboard' for run_sql — valid arguments: sql, limit, source"
    }
    unknown = await call(server, "run_sql", {"sql": "SELECT 1 AS one", "source": "zzz"})
    assert unknown["error"].startswith("unknown source 'zzz' — options: ")
    listed = await call(server, "list_metrics", {"dashboard": "zzz"})
    assert listed == {
        "error": "unknown argument 'dashboard' for list_metrics — valid arguments: (none)"
    }


@pytest.mark.anyio
async def test_a_tool_registered_after_the_first_call_is_still_guarded(demo_dir):
    """The schema map used to fill once, on the first tools/call. A tool added
    after that was absent from it, which reads the same as 'not ours', so the
    guard waved through exactly the arguments it exists to refuse. #395 review."""
    server = create_mcp_server(demo_dir, allow_sql=True)
    first = await call(server, "get_schema", {"dashboard": "demo"})
    assert "unknown argument" in first["error"]

    @server.tool()
    @mcp_result
    def late_tool(source: str) -> dict[str, Any]:
        """Registered after the guard already ran once."""
        return {"source": source}

    refused = await call(server, "late_tool", {"dashboard": "demo", "source": "x"})
    assert refused == {
        "error": "unknown argument 'dashboard' for late_tool — valid arguments: source"
    }
    assert await call(server, "late_tool", {"source": "x"}) == {"source": "x"}


@pytest.mark.anyio
async def test_declared_arguments_still_run(demo_dir):
    server = create_mcp_server(demo_dir, allow_sql=True)
    assert (await call(server, "get_schema", {"source": "zzz"}))["error"].startswith(
        "unknown source 'zzz'"
    )
    assert (await call(server, "get_schema"))["source"] == "metrics.yaml"
    assert (await call(server, "run_sql", {"sql": "SELECT 1 AS one", "limit": 5}))["rows"] == [[1]]
    assert (await call(server, "list_metrics"))["metrics"]
    queried = await call(
        server,
        "query_metric",
        {"name": "revenue", "dashboard": "demo", "dimensions": ["region"], "limit": 10},
    )
    assert queried["row_count"] >= 1


@pytest.mark.anyio
async def test_list_and_get_metric_carry_cumulative(demo_dir):
    """An agent comparing `cumulative_revenue` to `revenue` saw two identical
    rows: same expr, `window` absent on both, no `cumulative` anywhere (#605)."""
    server = create_mcp_server(demo_dir)
    listed = {m["name"]: m for m in (await call(server, "list_metrics"))["metrics"]}
    assert listed["cumulative_revenue"]["cumulative"] is True
    assert "cumulative" not in listed["revenue"]
    detail = await call(server, "get_metric", {"name": "cumulative_revenue"})
    assert detail["cumulative"] is True


@pytest.mark.anyio
async def test_initialize_reports_the_package_version(demo_dir):
    """A client that shows the server version, or checks it for compatibility, got
    an empty string: MCPServer defaults `version` to '' and nothing passed one."""
    from sqldash import __version__

    server = create_mcp_server(demo_dir)
    options = server._lowlevel_server.create_initialization_options()
    assert options.server_name == "sqldash"
    assert options.server_version == __version__
    assert options.server_version


@asynccontextmanager
async def stdio_session(path):
    """A real `sqldash mcp` subprocess over stdio pipes.

    The in-memory session exercises the protocol; this exercises what a host
    actually launches, which is where the clipped series of #674 was seen.
    """
    params = StdioServerParameters(
        command=sys.executable,
        args=["-c", "from sqldash.cli import app; app()", "mcp", str(path)],
        env=dict(os.environ),
    )
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as client,
    ):
        await client.initialize()
        yield client


def _cli_row_count(path) -> int:
    window = ["--start", "-365d", "--end", "today", "-f", "csv"]
    result = CliRunner().invoke(cli_app, ["query", str(path), "order_count", "-g", "day", *window])
    assert result.exit_code == 0, result.output
    return len(result.output.strip().splitlines()) - 1


@pytest.mark.anyio
async def test_query_metric_over_stdio_returns_the_series_the_cli_returns(demo_dir):
    """#674: the tool's `limit: int = 100` default became a `LIMIT 100` in the
    compiled SQL, so the connector never saw more than 100 rows and stamped the
    clipped series `truncated: false`. An agent asking for a year of daily
    orders got the first 100 days and was told nothing was cut, while the CLI
    and `POST /api/run` returned all of them."""
    expected = _cli_row_count(demo_dir)
    assert expected > 100
    async with stdio_session(demo_dir) as client:
        result = await client.call_tool(
            "query_metric",
            {"name": "order_count", "grain": "day", "start": "-365d", "end": "today"},
        )
    payload = json.loads(result.content[0].text)
    assert payload["row_count"] == expected
    assert payload["truncated"] is False
    assert "note" not in payload
    assert "LIMIT" not in payload["sql"]


@pytest.mark.anyio
async def test_the_last_representable_end_day_keeps_an_inclusive_bound(demo_dir):
    """A date-only end compiles as `< next day`, and 9999-12-31 has no next day:
    the CLI exited with an OverflowError traceback and MCP returned a raw tool
    error, where main answered with the inclusive bound."""
    window = ["--start", "2026-01-01", "--end", "9999-12-31", "-f", "csv"]
    cli = CliRunner().invoke(cli_app, ["query", str(demo_dir), "revenue", *window])
    assert cli.exit_code == 0, cli.output
    total = float(cli.output.strip().splitlines()[-1])
    assert total > 0
    result = await call(
        create_mcp_server(demo_dir),
        "query_metric",
        {"name": "revenue", "start": "2026-01-01", "end": "9999-12-31"},
    )
    assert "error" not in result, result
    assert "<= ?" in result["sql"]
    assert result["rows"][0][0] == pytest.approx(total)


@pytest.mark.anyio
async def test_query_metric_limit_reports_the_clip_it_applied(demo_dir):
    """A cap the caller asked for is honest about what it did: the rows stop at
    the cap, `truncated` says there are more, and the note names the way out."""
    server = create_mcp_server(demo_dir)
    result = await call(
        server,
        "query_metric",
        {"name": "order_count", "grain": "day", "start": "-365d", "end": "today", "limit": 5},
    )
    assert result["row_count"] == 5
    assert result["truncated"] is True
    assert result["row_limit"] == 5
    assert "first 5 rows only" in result["note"]


@pytest.mark.anyio
async def test_query_metric_limit_is_compiled_one_past_the_cap(demo_dir):
    """A caller's limit used to stop only the fetch, so a metered warehouse
    computed and shipped every group for a two-row answer. It now reaches the
    SQL as one row past the cap: the warehouse stops early and the extra row is
    still there for `truncated` to see."""
    server = create_mcp_server(demo_dir)
    clipped = await call(
        server, "query_metric", {"name": "order_count", "dimensions": ["region"], "limit": 2}
    )
    assert clipped["sql"].endswith("\nLIMIT 3")
    assert (clipped["row_count"], clipped["truncated"], clipped["row_limit"]) == (2, True, 2)

    whole = await call(
        server, "query_metric", {"name": "order_count", "dimensions": ["region"], "limit": 3}
    )
    assert whole["sql"].endswith("\nLIMIT 4")
    assert (whole["row_count"], whole["truncated"]) == (3, False)


@pytest.mark.anyio
async def test_query_metric_limit_clamped_to_the_server_cap_in_the_sql(demo_dir):
    server = create_mcp_server(demo_dir, row_limit=5)
    result = await call(
        server,
        "query_metric",
        {"name": "order_count", "grain": "day", "start": "-365d", "end": "today", "limit": 50},
    )
    assert result["sql"].endswith("\nLIMIT 6")
    assert (result["row_count"], result["truncated"], result["row_limit"]) == (5, True, 5)


@pytest.mark.anyio
async def test_query_metric_limit_reaches_the_compare_window_sql(demo_dir):
    server = create_mcp_server(demo_dir)
    result = await call(
        server,
        "query_metric",
        {
            "name": "order_count",
            "grain": "day",
            "start": "-30d",
            "end": "today",
            "compare": "previous_period",
            "limit": 4,
        },
    )
    assert result["sql"].endswith("\nLIMIT 5")
    assert result["compare"]["sql"].endswith("\nLIMIT 5")
    assert len(result["compare"]["rows"]) == 4
    assert result["compare"]["truncated"] is True


@pytest.mark.anyio
async def test_query_metric_reports_the_server_row_limit_that_clipped_it(demo_dir):
    """The server's own cap is the default one, so it has to report itself too."""
    server = create_mcp_server(demo_dir, row_limit=5)
    result = await call(
        server,
        "query_metric",
        {"name": "order_count", "grain": "day", "start": "-365d", "end": "today"},
    )
    assert result["row_count"] == 5
    assert result["truncated"] is True
    assert result["row_limit"] == 5


@pytest.mark.anyio
async def test_query_metric_rejects_a_limit_that_cannot_cap(demo_dir):
    """`limit must be positive` used to come from the compiler; the cap no longer
    reaches it, so the tool has to say it."""
    server = create_mcp_server(demo_dir)
    refused = await call(server, "query_metric", {"name": "revenue", "limit": 0})
    assert refused == {"error": "limit must be positive"}


@pytest.mark.anyio
async def test_an_agent_bundle_tool_reports_its_own_limit(tmp_path):
    """agents.yaml `limit:` defaults to 100 and was compiled into the SQL the same
    way, so a bundle clipped a 120-bucket series and reported `truncated: false`."""
    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "agents.yaml").write_text(
        "tools:\n"
        "  daily_orders:\n"
        "    description: daily order counts for the last year\n"
        "    queries:\n"
        "      - {metric: order_count, grain: day, start: -365d, end: today}\n"
    )
    server = create_mcp_server(tmp_path)
    payload = await call(server, "daily_orders")
    entry = payload["results"][0]
    assert entry["row_count"] == 100
    assert entry["truncated"] is True
    assert entry["row_limit"] == 100
    assert "first 100 rows only" in entry["note"]
    assert "LIMIT" not in entry["sql"]


@pytest.mark.anyio
async def test_query_metric_reports_what_the_dashboard_scoped(tmp_path):
    """#676/#680: dashboard= applies a window and the select defaults, and the
    payload said neither — an agent got a narrowed number it could not see."""
    create_demo(tmp_path)
    (tmp_path / ".sqldash" / "dflt.yaml").write_text(
        "title: Defaults\n"
        "source: {type: duckdb, attach_files: true}\n"
        "filters:\n"
        "  - {name: dates, type: daterange, default: last_60_days}\n"
        "  - {name: region, type: select, options: [us, eu, apac], default: us}\n"
        "tiles:\n"
        "  - {title: Rev, metric: revenue}\n"
    )
    server = create_mcp_server(tmp_path)
    scoped = await call(server, "query_metric", {"name": "revenue", "dashboard": "dflt"})
    assert "windowed " in scoped["scope_note"], scoped["scope_note"]
    assert "filtered region=us by dashboard 'dflt'" in scoped["scope_note"]

    bare = await call(server, "query_metric", {"name": "revenue"})
    assert "scope_note" not in bare
    assert bare["rows"][0][0] > scoped["rows"][0][0]

    asked = await call(
        server,
        "query_metric",
        {
            "name": "revenue",
            "dashboard": "dflt",
            "filters": {"region": "all"},
            "start": "2020-01-01",
        },
    )
    assert "scope_note" not in asked


def split_source_repo(root: Path, scale: float) -> Path:
    """metrics.yaml on a duckdb file, a dashboard on attached CSVs: `orders`
    exists in both with different totals, `m_only` only behind metrics.yaml."""
    project = root / ".sqldash"
    (project / "dashdata").mkdir(parents=True)
    con = duckdb.connect(str(project / "proj.duckdb"))
    con.execute("CREATE TABLE orders(amount DOUBLE)")
    con.execute(f"INSERT INTO orders VALUES ({2 * scale}), ({3 * scale})")
    con.execute("CREATE TABLE m_only(x INTEGER)")
    con.execute("INSERT INTO m_only VALUES (7)")
    con.close()
    (project / "dashdata" / "orders.csv").write_text(f"amount\n{1000 * scale}\n")
    (project / "metrics.yaml").write_text(
        "source: {type: duckdb, database: proj.duckdb}\n"
        "relations:\n  orders: {table: orders}\n"
        "metrics:\n  revenue: {relation: orders, expr: SUM(amount)}\n"
    )
    (project / "dash.yaml").write_text(
        "title: Dash\nsource: {type: duckdb, attach_files: true, base_dir: dashdata}\n"
        'tiles:\n  - {title: Orders, sql: "SELECT SUM(amount) AS total FROM orders"}\n'
    )
    return project


TOTAL = {"sql": "SELECT SUM(amount) AS total FROM orders"}


@pytest.mark.anyio
async def test_run_sql_in_a_two_repo_workspace_asks_instead_of_borrowing_a_dashboard(tmp_path):
    """run_sql answered from the first dashboard store.discover() returned,
    alpha's CSVs, with no word of which source that was, while get_schema in
    the same session refused to guess. It now picks the way get_schema does."""
    server = create_mcp_server(
        workspace=[
            ("alpha", split_source_repo(tmp_path / "alpha", 1.0)),
            ("beta", split_source_repo(tmp_path / "beta", 10.0)),
        ],
        allow_sql=True,
    )
    options = "alpha/dash.source, alpha/metrics.yaml, beta/dash.source, beta/metrics.yaml"
    assert await call(server, "run_sql", TOTAL) == {
        "error": f"several sources — pass one of: {options}"
    }
    assert await call(server, "get_schema") == await call(server, "run_sql", TOTAL)
    beta = await call(server, "run_sql", {**TOTAL, "source": "beta/metrics.yaml"})
    assert (beta["source"], beta["rows"]) == ("beta/metrics.yaml", [[50.0]])
    metric = await call(server, "query_metric", {"name": "beta/revenue"})
    assert metric["rows"] == beta["rows"]
    alpha = await call(server, "run_sql", {**TOTAL, "source": "dashboard:alpha/dash"})
    assert (alpha["source"], alpha["rows"]) == ("alpha/dash.source", [[1000.0]])


@pytest.mark.anyio
async def test_run_sql_in_a_one_repo_workspace_uses_the_source_get_schema_advertises(tmp_path):
    server = create_mcp_server(
        workspace=[("solo", split_source_repo(tmp_path / "solo", 1.0))], allow_sql=True
    )
    schema = await call(server, "get_schema")
    assert schema["source"] == "solo/metrics.yaml"
    assert "m_only" in {t["name"] for t in schema["tables"]}
    total = await call(server, "run_sql", TOTAL)
    assert (total["source"], total["rows"]) == ("solo/metrics.yaml", [[5.0]])
    assert (await call(server, "run_sql", {"sql": "SELECT x FROM m_only"}))["rows"] == [[7]]


@pytest.mark.anyio
async def test_run_sql_in_a_single_project_picks_metrics_yaml_and_names_it(tmp_path):
    project = split_source_repo(tmp_path, 1.0)
    server = create_mcp_server(project, allow_sql=True)
    total = await call(server, "run_sql", TOTAL)
    assert (total["source"], total["rows"]) == ("metrics.yaml", [[5.0]])
    dash = await call(server, "run_sql", {**TOTAL, "source": "dash.source"})
    assert (dash["source"], dash["rows"]) == ("dash.source", [[1000.0]])
    (project / "metrics.yaml").unlink()
    lone = await call(create_mcp_server(project, allow_sql=True), "run_sql", TOTAL)
    assert (lone["source"], lone["rows"]) == ("dash.source", [[1000.0]])


@pytest.mark.anyio
async def test_run_sql_in_a_single_project_with_several_dashboards_asks_like_get_schema(
    tmp_path,
):
    """Without a metrics.yaml, run_sql borrowed the alphabetically first
    dashboard's source while get_schema refused to guess between them."""
    project = split_source_repo(tmp_path, 1.0)
    (project / "metrics.yaml").unlink()
    (project / "other.yaml").write_text("title: Other\nsource: {type: duckdb}\ntiles: []\n")
    server = create_mcp_server(project, allow_sql=True)
    refused = await call(server, "run_sql", TOTAL)
    assert refused == {"error": "several sources — pass one of: dash.source, other.source"}
    assert refused == await call(server, "get_schema")
    dash = await call(server, "run_sql", {**TOTAL, "source": "dash.source"})
    assert (dash["source"], dash["rows"]) == ("dash.source", [[1000.0]])


@pytest.mark.anyio
@pytest.mark.parametrize(
    "token", ["-99999999d", "-2739000y", "last_99999999999_days", "-9999999999999999999999w"]
)
async def test_mcp_query_metric_huge_relative_date_is_a_named_error(demo_dir, token):
    """Was a raw isError carrying "date value out of range"."""
    result = await call(
        create_mcp_server(demo_dir), "query_metric", {"name": "revenue", "start": token}
    )
    assert result["error"].startswith(f"unrecognized date {token!r}"), result
