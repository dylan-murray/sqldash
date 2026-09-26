import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

psycopg = pytest.importorskip("psycopg")

from helpers import run_to_completion  # noqa: E402

from sqldash.connectors.base import CancelToken, ConnectorError  # noqa: E402
from sqldash.connectors.engine import EngineConnector  # noqa: E402
from sqldash.lint import lint_project  # noqa: E402
from sqldash.models.source import Source  # noqa: E402
from sqldash.project.store import DashboardStore  # noqa: E402
from sqldash.semantics import SemanticLayer  # noqa: E402
from sqldash.server import create_app  # noqa: E402

PG_PORT = 54329


@pytest.fixture(scope="module")
def pg_server(tmp_path_factory):
    pg_ctl = shutil.which("pg_ctl")
    initdb = shutil.which("initdb")
    if not pg_ctl or not initdb:
        pytest.skip("postgres binaries not available")
    data_dir = tmp_path_factory.mktemp("pgdata")
    subprocess.run(
        [initdb, "-D", str(data_dir), "-U", "sqldash", "--auth=trust"],
        check=True,
        capture_output=True,
    )
    log = data_dir / "pg.log"
    subprocess.run(
        [
            pg_ctl,
            "-D",
            str(data_dir),
            "-l",
            str(log),
            "-o",
            f"-p {PG_PORT} -k '' -c listen_addresses=127.0.0.1",
            "start",
        ],
        check=True,
        capture_output=True,
    )
    try:
        deadline = time.time() + 45  # cold CI runners start postgres slowly
        while time.time() < deadline:
            try:
                with psycopg.connect(
                    host="127.0.0.1", port=PG_PORT, user="sqldash", dbname="postgres"
                ) as conn:
                    conn.execute(
                        "CREATE TABLE orders "
                        "(id serial, region text, amount numeric(10,2), created date)"
                    )
                    conn.execute(
                        "INSERT INTO orders (region, amount, created) "
                        "SELECT 'us', (n % 50) + 0.99, DATE '2026-01-01' + (n % 30) "
                        "FROM generate_series(1, 500) n"
                    )
                    conn.commit()
                break
            except psycopg.OperationalError:
                time.sleep(0.3)
        else:
            pytest.fail("postgres did not start")
        yield
    finally:
        subprocess.run(
            [pg_ctl, "-D", str(data_dir), "stop", "-m", "immediate"], capture_output=True
        )


@pytest.fixture
def connector(pg_server):
    source = Source(
        type="postgres",
        host="127.0.0.1",
        port=PG_PORT,
        database="postgres",
        username="sqldash",
    )
    conn = EngineConnector(source, Path("."))
    conn.connect()
    yield conn
    conn.close()


def test_execute_and_types(connector):
    result = connector.execute(
        "SELECT region, SUM(amount) AS total, COUNT(*) AS n, MIN(created) AS first "
        "FROM orders GROUP BY 1",
        [],
        100,
        CancelToken(),
    )
    assert [c.type for c in result.columns] == ["string", "decimal", "integer", "date"]
    assert result.rows[0][0] == "us"
    assert result.rows[0][2] == 500


def test_bind_params(connector):
    result = connector.execute(
        "SELECT COUNT(*) AS n FROM orders WHERE amount > %s AND region = %s",
        [25, "us"],
        10,
        CancelToken(),
    )
    assert result.rows[0][0] > 0


def test_row_limit(connector):
    result = connector.execute("SELECT * FROM orders", [], 100, CancelToken())
    assert result.row_count == 100
    assert result.truncated is True


def test_error_surfaces(connector):
    with pytest.raises(ConnectorError, match="missing_table"):
        connector.execute("SELECT * FROM missing_table", [], 10, CancelToken())


def test_cancel_long_query(pg_server):
    import threading

    source = Source(
        type="postgres",
        host="127.0.0.1",
        port=PG_PORT,
        database="postgres",
        username="sqldash",
    )
    conn = EngineConnector(source, Path("."))
    conn.connect()
    token = CancelToken()
    errors = []

    def run():
        try:
            conn.execute("SELECT pg_sleep(30)", [], 10, token)
        except ConnectorError as exc:
            errors.append(str(exc))

    thread = threading.Thread(target=run)
    started = time.monotonic()
    thread.start()
    time.sleep(0.8)
    token.cancel()
    thread.join(timeout=10)
    elapsed = time.monotonic() - started
    conn.close()
    assert not thread.is_alive(), "query did not cancel"
    assert elapsed < 10, f"cancel took {elapsed:.1f}s"
    assert errors
    assert "cancel" in errors[0].lower()


def test_introspect(connector):
    """information_schema's data_type is a bare `numeric`; the precision and
    scale live in their own columns and were dropped."""
    tables = connector.introspect()
    orders = next(t for t in tables if t.name == "orders")
    assert ("amount", "numeric(10,2)") in orders.columns
    assert ("id", "integer") in orders.columns


def _table_exists(name):
    with psycopg.connect(host="127.0.0.1", port=PG_PORT, user="sqldash", dbname="postgres") as c:
        return c.execute("SELECT to_regclass(%s)", (name,)).fetchone()[0] is not None


def test_adhoc_select_into_behind_a_template_quote_creates_nothing(pg_server, tmp_path):
    """The guard scanned the template, where `{% if region %}'{% endif %}` put
    INTO inside a string literal; rendering dropped the quote and Postgres
    created the table. The rendered text is what gets scanned now."""
    root = tmp_path / ".sqldash"
    root.mkdir()
    (root / "pg.yaml").write_text(
        "title: PG\n"
        f"source: {{type: postgres, host: 127.0.0.1, port: {PG_PORT}, "
        "database: postgres, username: sqldash}\n"
        "filters:\n"
        "  - {name: region, type: select, options: [us, eu]}\n"
        "tiles:\n"
        "  - {title: N, sql: SELECT COUNT(*) AS n FROM orders}\n"
    )
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as client:
        client.headers["X-Sqldash-Token"] = app.state.api_token
        res = client.post(
            "/api/run",
            json={
                "dashboard": "pg",
                "sql": "SELECT 1 AS x {% if region %}'{% endif %} INTO pwned_tbl --'",
                "params": {"region": "all"},
            },
        )
        assert res.status_code == 422, res.text
        assert res.json()["detail"] == (
            "ad-hoc sql is read-only; a statement containing INTO is refused"
        )
    assert not _table_exists("pwned_tbl")


@pytest.fixture
def staging(pg_server):
    with psycopg.connect(host="127.0.0.1", port=PG_PORT, user="sqldash", dbname="postgres") as db:
        db.execute("CREATE SCHEMA IF NOT EXISTS staging")
        db.execute("DROP TABLE IF EXISTS staging.orders")
        db.execute("CREATE TABLE staging.orders (LIKE public.orders)")
        db.execute("INSERT INTO staging.orders (id, region, amount) VALUES (1, 'us', 6)")
        db.execute("DROP ROLE IF EXISTS reader")
        db.execute("CREATE ROLE reader")
        db.execute("GRANT reader TO sqldash")
        db.commit()
    yield
    with psycopg.connect(host="127.0.0.1", port=PG_PORT, user="sqldash", dbname="postgres") as db:
        db.execute("DROP SCHEMA staging CASCADE")
        db.execute("DROP ROLE reader")
        db.commit()


def _scalars(connector, sql):
    return tuple(connector.execute(sql, [], 10, CancelToken()).rows[0])


def test_session_settings_do_not_outlive_the_statement_that_set_them(connector, staging):
    """A read can change session state through a function (set_config with
    is_local=false, pg_advisory_lock), and the pooled connection carried it to
    every later caller: a governed SUM silently read another schema's table."""
    probe = "SELECT pg_backend_pid(), SUM(amount), current_setting('search_path') FROM orders"
    pid, total, path = _scalars(connector, probe)
    assert path == '"$user", public'
    _scalars(connector, "SELECT set_config('search_path', 'staging', false)")
    _scalars(connector, "SELECT set_config('statement_timeout', '1', false)")
    _scalars(connector, "SELECT set_config('session_authorization', 'reader', false)")
    _scalars(connector, "SELECT pg_advisory_lock(38)")
    after = _scalars(connector, probe)
    assert after == (pid, total, path)
    held, timeout, who = _scalars(
        connector,
        "SELECT (SELECT COUNT(*) FROM pg_locks WHERE locktype = 'advisory' "
        "AND pid = pg_backend_pid()), current_setting('statement_timeout'), session_user",
    )
    assert (held, timeout, who) == (0, "0", "sqldash")


def test_session_reset_keeps_the_configured_role_and_connect_options(pg_server, staging):
    source = Source(
        type="postgres",
        host="127.0.0.1",
        port=PG_PORT,
        database="postgres",
        username="sqldash",
        role="reader",
        connect_args={"options": "-c search_path=staging"},
    )
    conn = EngineConnector(source, Path("."))
    try:
        probe = "SELECT pg_backend_pid(), current_user, current_setting('search_path')"
        pid, *configured = _scalars(conn, probe)
        assert configured == ["reader", "staging"]
        _scalars(conn, "SELECT set_config('search_path', 'public', false)")
        _scalars(conn, "SELECT set_config('role', 'none', false)")
        assert _scalars(conn, probe) == (pid, *configured)
    finally:
        conn.close()


PERCENT_PROJECT = (
    "title: PG\n"
    f"source: {{type: postgres, host: 127.0.0.1, port: {PG_PORT}, "
    "database: postgres, username: sqldash}\n"
    "filters:\n"
    "  - {name: region, type: select, options: [us, eu]}\n"
    "queries:\n"
    "  q: |\n"
    "    SELECT COUNT(*) AS n, '100%' AS label -- 5% of rows\n"
    "    FROM orders WHERE region LIKE 'u%' AND region = {{ region }}\n"
    "tiles:\n"
    "  - {query: q}\n"
)

PERCENT_METRICS = (
    f"source: {{type: postgres, host: 127.0.0.1, port: {PG_PORT}, "
    "database: postgres, username: sqldash}\n"
    "relations:\n"
    "  orders: {table: orders}\n"
    "metrics:\n"
    "  u_orders:\n"
    "    relation: orders\n"
    "    expr: SUM(CASE WHEN region LIKE 'u%' THEN 1 ELSE 0 END)\n"
    "    filters: [\"region NOT LIKE '%x%'\"]\n"
    "    time_dimension: {name: created, grain: day}\n"
    "    dimensions: [{name: region}]\n"
)


@pytest.mark.parametrize(
    "payload",
    [
        {"dashboard": "pg", "query": "q", "params": {"region": "us"}},
        {"dashboard": "pg", "metric": "u_orders", "filters": {"region": "us"}},
        {"dashboard": "pg", "metric": "u_orders", "start": "2026-01-01"},
    ],
    ids=["tile", "metric-filter", "metric-window"],
)
def test_literal_percent_survives_a_bound_value(pg_server, tmp_path, payload):
    """psycopg reads every % in the text once any value is bound, so a LIKE 'u%'
    that ran unfiltered failed with "only '%s', '%b', '%t' are allowed as
    placeholders" as soon as a filter was set."""
    root = tmp_path / ".sqldash"
    root.mkdir()
    (root / "pg.yaml").write_text(PERCENT_PROJECT)
    (root / "metrics.yaml").write_text(PERCENT_METRICS)
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as client:
        client.headers["X-Sqldash-Token"] = app.state.api_token
        _, ex = run_to_completion(client, payload)
    assert ex["status"] == "done", ex["error"]
    assert ex["result"]["rows"][0][0] == 500
    if "query" in payload:
        assert ex["result"]["rows"][0][1] == "100%"


def test_strict_lint_names_a_time_dimension_the_warehouse_case_folds(pg_server, tmp_path):
    """A column created quoted in the non-folding case is lint-clean by name but
    compiles as a bare identifier the warehouse folds: Snowflake uppercases
    `order_date_lc` into an invalid identifier, Postgres lowercases `ORDER_DATE_UC`.
    Only the --strict probe sees it."""
    with psycopg.connect(host="127.0.0.1", port=PG_PORT, user="sqldash", dbname="postgres") as c:
        c.execute('CREATE TABLE IF NOT EXISTS cased_orders ("ORDER_DATE_UC" date, amount numeric)')
        c.commit()
    root = tmp_path / ".sqldash"
    root.mkdir()
    (root / "metrics.yaml").write_text(
        f"source: {{type: postgres, host: 127.0.0.1, port: {PG_PORT}, "
        "database: postgres, username: sqldash}\n"
        "relations:\n"
        "  cased: {table: cased_orders}\n"
        "metrics:\n"
        "  bare_trend:\n"
        "    relation: cased\n"
        "    expr: SUM(amount)\n"
        "    time_dimension: {name: order_date_uc, grain: day}\n"
        "  quoted_trend:\n"
        "    relation: cased\n"
        "    expr: SUM(amount)\n"
        "    time_dimension: {name: order_date_uc, grain: day, expr: '\"ORDER_DATE_UC\"'}\n"
    )
    store = DashboardStore(tmp_path)
    assert lint_project(store, SemanticLayer(store)) == []
    findings = lint_project(store, SemanticLayer(store), check_sql=True)
    assert [f.message for f in findings] == [
        "metric 'bare_trend': SQL fails against the source: "
        'column "order_date_uc" does not exist; '
        "time_dimension 'order_date_uc' has no expr, so it compiles as a bare column the "
        "warehouse case-folds. If the column was created quoted, set expr to its "
        "name in double quotes, exactly as created"
    ]
