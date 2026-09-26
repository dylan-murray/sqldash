import threading
import time

import pytest

from sqldash.connectors.base import CancelToken, ConnectorError
from sqldash.connectors.engine import (
    EngineConnector,
    explain_confinement,
    paramstyle_for,
)
from sqldash.connectors.wire import flat_text, parse_json_text, to_jsonable
from sqldash.models.source import Source


@pytest.fixture
def connector(tmp_path):
    conn = EngineConnector(Source(type="duckdb"), tmp_path)
    conn.connect()
    yield conn
    conn.close()


def test_execute_types(connector):
    result = connector.execute(
        "SELECT 1 AS i, 1.5::DOUBLE AS f, 1.5 AS dec, 'x' AS s, DATE '2026-01-01' AS d, TRUE AS b",
        [],
        100,
        CancelToken(),
    )
    assert [c.type for c in result.columns] == [
        "integer",
        "float",
        "decimal",
        "string",
        "date",
        "boolean",
    ]
    assert result.rows == [[1, 1.5, "1.5", "x", "2026-01-01", True]]


def test_row_limit_truncates(connector):
    result = connector.execute("SELECT * FROM range(100)", [], 10, CancelToken())
    assert result.row_count == 10
    assert result.truncated is True


def test_bind_params(connector):
    assert paramstyle_for(Source(type="duckdb")) == "qmark"
    result = connector.execute("SELECT 40 + ? AS answer", [2], 10, CancelToken())
    assert result.rows == [[42]]


def test_error_surfaces(connector):
    with pytest.raises(ConnectorError, match="nonexistent_table"):
        connector.execute("SELECT * FROM nonexistent_table", [], 10, CancelToken())


def _raise_from_cursor(connector, message: str, dialect_name: str | None = None):
    """Drive EngineConnector.execute's conversion branch with a raw driver error."""
    engine = connector.engine
    if dialect_name is not None:
        engine.dialect.name = dialect_name
    real_connect = engine.connect

    def patched_connect():
        pooled = real_connect()
        dbapi = pooled.connection.dbapi_connection
        orig_cursor = dbapi.cursor

        def cursor():
            cur = orig_cursor()

            def boom(*args, **kwargs):
                raise RuntimeError(message)

            cur.execute = boom
            cur.execute_async = boom
            return cur

        dbapi.cursor = cursor
        return pooled

    engine.connect = patched_connect


def test_snowflake_timeout_wording_is_named_only_on_snowflake(connector):
    _raise_from_cursor(
        connector,
        "SQL execution was cancelled by the client due to a timeout.",
        dialect_name="snowflake",
    )
    with pytest.raises(ConnectorError, match="network_timeout"):
        connector.execute("SELECT 1", [], 10, CancelToken())


def test_a_postgres_statement_timeout_is_not_a_snowflake_hint(connector):
    """Postgres says 'canceling statement due to statement timeout'. The
    timeout branch used to rewrite every dialect to a Snowflake connect_args
    hint that psycopg then rejects."""
    _raise_from_cursor(connector, "canceling statement due to statement timeout")
    with pytest.raises(ConnectorError, match="statement timeout") as exc:
        connector.execute("SELECT 1", [], 10, CancelToken())
    assert "network_timeout" not in str(exc.value)
    assert "Snowflake" not in str(exc.value)


def _write_duckdb(path, value: int) -> None:
    import duckdb

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute(f"INSERT INTO t VALUES ({value})")
    conn.close()


@pytest.mark.parametrize("attach_files", [False, True])
def test_relative_database_opens_under_source_base_dir(tmp_path, attach_files):
    """build_url used to join `database:` onto the dashboard dir while attach
    scanned `base_dir:`, so the nested file lost to a decoy at the root."""
    nested = tmp_path / "data" / "csv"
    _write_duckdb(nested / "app.duckdb", 42)
    _write_duckdb(tmp_path / "app.duckdb", 999)
    source = Source(
        type="duckdb",
        attach_files=attach_files,
        base_dir="data/csv",
        database="app.duckdb",
    )
    conn = EngineConnector(source, tmp_path)
    conn.connect()
    try:
        assert conn.execute("SELECT x FROM t", [], 10, CancelToken()).rows == [[42]]
    finally:
        conn.close()


def test_empty_attach_dir_fails_before_catalog_error(tmp_path):
    conn = EngineConnector(Source(type="duckdb", attach_files=True), tmp_path)
    with pytest.raises(ConnectorError, match="0 files") as exc:
        conn.connect()
    msg = str(exc.value)
    assert "attached 0 files" in msg
    assert "csv" in msg
    assert "parquet" in msg
    assert str(tmp_path) in msg or str(tmp_path.resolve()) in msg
    assert "Catalog Error" not in msg


def test_attach_files_csv_and_nested(tmp_path):
    (tmp_path / "pets.csv").write_text("name,age\nrex,3\nmochi,5\n")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "toys.csv").write_text("toy\nball\n")
    conn = EngineConnector(Source(type="duckdb", attach_files=True), tmp_path)
    conn.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM pets", [], 10, CancelToken()).rows == [[2]]
        assert conn.execute("SELECT COUNT(*) FROM toys", [], 10, CancelToken()).rows == [[1]]
    finally:
        conn.close()


def test_attach_files_picks_up_a_file_added_after_the_first_query(tmp_path):
    """The attach scan used to run once at engine build and be captured by the
    connect listener, so a csv dropped in later was never a view until restart
    (#279). Now every checkout re-derives it, and a removed file's view goes
    with it rather than lingering as a read error naming a deleted path."""
    (tmp_path / "first.csv").write_text("a,b\n1,x\n2,y\n")
    conn = EngineConnector(Source(type="duckdb", attach_files=True), tmp_path)
    conn.connect()
    try:
        assert conn.execute("SELECT count(*) FROM first", [], 10, CancelToken()).rows == [[2]]
        with pytest.raises(ConnectorError, match="second"):
            conn.execute("SELECT count(*) FROM second", [], 10, CancelToken())
        (tmp_path / "second.csv").write_text("a,b\n1,x\n2,y\n3,z\n")
        assert conn.execute("SELECT count(*) FROM second", [], 10, CancelToken()).rows == [[3]]
        assert conn.execute("SELECT count(*) FROM first", [], 10, CancelToken()).rows == [[2]]
        (tmp_path / "second.csv").unlink()
        with pytest.raises(ConnectorError, match="second does not exist"):
            conn.execute("SELECT count(*) FROM second", [], 10, CancelToken())
        assert conn.execute("SELECT count(*) FROM first", [], 10, CancelToken()).rows == [[2]]
    finally:
        conn.close()


def test_attach_files_keeps_a_view_whose_name_another_file_still_claims(tmp_path):
    """`a-b.csv` and `a_b.csv` both map to view `a_b`; removing one must not drop
    the view the survivor still owns, and an in-place rewrite is picked up
    through its mtime. #279 review."""
    (tmp_path / "a-b.csv").write_text("n\n1\n")
    (tmp_path / "a_b.csv").write_text("n\n1\n2\n")
    conn = EngineConnector(Source(type="duckdb", attach_files=True), tmp_path)
    conn.connect()
    try:
        assert conn.execute("SELECT count(*) FROM a_b", [], 10, CancelToken()).rows == [[2]]
        (tmp_path / "a-b.csv").unlink()
        assert conn.execute("SELECT count(*) FROM a_b", [], 10, CancelToken()).rows == [[2]]
        (tmp_path / "a_b.csv").write_text("n\n1\n2\n3\n")
        assert conn.execute("SELECT count(*) FROM a_b", [], 10, CancelToken()).rows == [[3]]
    finally:
        conn.close()


def test_attach_files_syncs_every_pooled_connection(tmp_path):
    """Each pooled connection tracks what it attached, so a second connection
    that was already open when the file landed still sees it on its next use."""
    from sqlalchemy import text

    (tmp_path / "first.csv").write_text("a\n1\n")
    conn = EngineConnector(Source(type="duckdb", attach_files=True), tmp_path)
    conn.connect()
    try:
        held = conn.engine.connect()
        assert held.execute(text("SELECT count(*) FROM first")).scalar() == 1
        assert conn.execute("SELECT count(*) FROM first", [], 10, CancelToken()).rows == [[1]]
        held.close()
        (tmp_path / "second.csv").write_text("a\n1\n2\n")
        assert conn.execute("SELECT count(*) FROM second", [], 10, CancelToken()).rows == [[2]]
        with conn.engine.connect() as other:
            assert other.execute(text("SELECT count(*) FROM second")).scalar() == 2
    finally:
        conn.close()


def test_introspect(tmp_path):
    (tmp_path / "pets.csv").write_text("name,age\nrex,3\n")
    conn = EngineConnector(Source(type="duckdb", attach_files=True), tmp_path)
    conn.connect()
    try:
        tables = conn.introspect()
        pets = next(t for t in tables if t.name == "pets")
        assert [c[0] for c in pets.columns] == ["name", "age"]
    finally:
        conn.close()


def test_cancel_long_query(tmp_path):
    conn = EngineConnector(Source(type="duckdb"), tmp_path)
    conn.connect()
    token = CancelToken()
    errors = []

    def run():
        try:
            conn.execute("SELECT COUNT(*) FROM range(100000000) a, range(1000) b", [], 10, token)
        except ConnectorError as exc:
            errors.append(str(exc))

    thread = threading.Thread(target=run)
    started = time.monotonic()
    thread.start()
    time.sleep(0.5)
    token.cancel()
    thread.join(timeout=10)
    elapsed = time.monotonic() - started
    conn.close()
    assert not thread.is_alive(), "query did not cancel"
    assert elapsed < 10, f"cancel took {elapsed:.1f}s"
    assert errors
    assert "cancel" in errors[0].lower() or "interrupt" in errors[0].lower()


def test_generic_sqlalchemy_source_sqlite(tmp_path):
    db = tmp_path / "t.db"
    source = Source(url=f"sqlite:///{db}")
    assert paramstyle_for(source) == "qmark"
    conn = EngineConnector(source, tmp_path)
    conn.connect()
    try:
        conn.execute("CREATE TABLE t (a INTEGER, b TEXT)", [], 10, CancelToken())
        conn.execute("INSERT INTO t VALUES (1, 'x'), (2, 'y')", [], 10, CancelToken())
        result = conn.execute("SELECT * FROM t WHERE a > ?", [0], 10, CancelToken())
        assert result.row_count == 2
        tables = conn.introspect()
        assert any(t.name == "t" for t in tables)
    finally:
        conn.close()


def test_wire_inference_is_generic():
    from datetime import date
    from decimal import Decimal

    from sqldash.connectors.wire import infer_columns, wire_type_from_native

    assert wire_type_from_native("DOUBLE PRECISION") == "float"
    assert wire_type_from_native("TIMESTAMP WITH TIME ZONE") == "timestamp"
    assert wire_type_from_native("text[]") == "json"
    assert wire_type_from_native("whatever_exotic") == "string"

    description = [("a", "MYSTERY"), ("b", "MYSTERY"), ("c", "MYSTERY")]
    rows = [(Decimal("1.5"), date(2026, 1, 1), None), (Decimal("2.5"), None, None)]
    columns = infer_columns(description, rows)
    assert [c.type for c in columns] == ["decimal", "date", "string"]


def test_warehouse_url_building(tmp_path):
    from sqldash.connectors.engine_urls import build_url
    from sqldash.models.source import Source

    bq = build_url(Source(type="bigquery", project="my-proj", database="analytics"), tmp_path)
    assert str(bq) == "bigquery://my-proj/analytics"

    dbx = build_url(
        Source(
            type="databricks",
            host="dbx.cloud",
            http_path="/sql/1.0/wh/abc",
            token="tok",
            catalog="main",
            **{"schema": "gold"},
        ),
        tmp_path,
    )
    assert dbx.drivername == "databricks"
    assert dbx.username == "token"
    assert dbx.password == "tok"
    assert dbx.query["http_path"] == "/sql/1.0/wh/abc"
    assert dbx.query["catalog"] == "main"
    assert dbx.query["schema"] == "gold"

    rs = build_url(
        Source(type="redshift", host="rs.aws", database="dw", username="u", password="p"),
        tmp_path,
    )
    assert rs.drivername == "redshift+redshift_connector"
    assert (rs.username, rs.password, rs.host, rs.database) == ("u", "p", "rs.aws", "dw")

    athena = build_url(
        Source(
            type="athena",
            host="us-east-1",
            **{"schema": "curated"},
            options={"s3_staging_dir": "s3://bkt/stage"},
        ),
        tmp_path,
    )
    assert athena.host == "athena.us-east-1.amazonaws.com"
    assert athena.database == "curated"
    assert athena.query["s3_staging_dir"] == "s3://bkt/stage"


def test_warehouse_url_errors(tmp_path):
    from sqldash.connectors.engine_urls import build_url
    from sqldash.models.source import Source

    with pytest.raises(ConnectorError, match="project"):
        build_url(Source(type="bigquery"), tmp_path)
    with pytest.raises(ConnectorError, match="http_path"):
        build_url(Source(type="databricks", host="h"), tmp_path)


def test_to_jsonable_spells_non_finite_floats_as_text():
    """NaN/Infinity are Python-only literals: RFC 8259 has no spelling for them,
    node refuses them and jq silently rewrites them. They used to become null,
    which made a warehouse NaN indistinguishable from NULL on every surface, so
    they travel as the text JSON.parse and float() both read back."""
    assert to_jsonable(float("nan")) == "NaN"
    assert to_jsonable(float("inf")) == "Infinity"
    assert to_jsonable(float("-inf")) == "-Infinity"
    assert to_jsonable(1.5) == 1.5
    assert to_jsonable(0.0) == 0.0
    assert to_jsonable(True) is True
    assert to_jsonable([float("nan"), 2.0, {"k": float("inf")}]) == ["NaN", 2.0, {"k": "Infinity"}]


def test_binary_is_hex_like_the_warehouse_shows_it():
    assert to_jsonable(b"ab") == "6162"
    assert to_jsonable(bytearray(b"\x00\xff")) == "00FF"


def test_json_text_parses_and_undefined_inside_it_is_null():
    assert parse_json_text("[\n  1,\n  undefined\n]") == [1, None]
    assert parse_json_text('{"undefined": "undefined", "x": undefined}') == {
        "undefined": "undefined",
        "x": None,
    }
    assert parse_json_text('"a \\" undefined"') == 'a " undefined'
    assert parse_json_text("POINT(1 2)") == "POINT(1 2)"
    assert parse_json_text("123") == "123"
    assert parse_json_text("true") == "true"
    assert parse_json_text({"k": 1}) == {"k": 1}


def test_flat_text_puts_json_on_one_line():
    assert flat_text({"a": [1, None]}) == '{"a":[1,null]}'
    assert flat_text(["é"]) == '["é"]'
    assert flat_text("x") == "x"
    assert flat_text(None) is None


def test_duckdb_json_blob_and_non_finite_values(connector):
    result = connector.execute(
        "SELECT '{\"a\":1}'::JSON AS j, [1, NULL] AS l, {'k': 'v'} AS s, 'ab'::BLOB AS b, "
        "'NaN'::DOUBLE AS n, '-inf'::DOUBLE AS ni",
        [],
        100,
        CancelToken(),
    )
    assert [c.type for c in result.columns] == ["json", "json", "json", "binary", "float", "float"]
    assert result.rows == [[{"a": 1}, [1, None], {"k": "v"}, "6162", "NaN", "-Infinity"]]


@pytest.mark.parametrize("source_type", ["duckdb", "sqlite"])
def test_missing_file_database_is_refused_before_connecting(tmp_path, monkeypatch, source_type):
    """DuckDB and SQLite create a missing database on connect, so every surface
    that reached the engine (describe, get_schema, queries, schema probes) used
    to plant an empty file at a typo'd path and report on it. An env-expanded
    path is checked too; `source test`'s pre-check has to skip those. #515."""
    monkeypatch.setenv("SQLDASH_TEST_DB", "sub/typo.db")
    for database in ("typo.db", "${env:SQLDASH_TEST_DB}"):
        conn = EngineConnector(Source(type=source_type, database=database), tmp_path)
        with pytest.raises(ConnectorError, match="database file not found: "):
            conn.connect()
    assert list(tmp_path.rglob("*")) == []


@pytest.mark.parametrize("source_type", [None, "duckdb"])
def test_duckdb_url_ignores_attachment_fields(tmp_path, source_type):
    source = Source(
        type=source_type, url="duckdb:///:memory:", attach_files=True, base_dir="missing"
    )
    conn = EngineConnector(source, tmp_path)
    try:
        conn.connect()
        result = conn.execute("SELECT 7 AS n", [], 1, CancelToken())
        assert result.rows == [[7]]
    finally:
        conn.close()


def _outside_secret(tmp_path):
    """A stand-in for ~/.config/sqldash/profiles.yaml, outside the project dir."""
    secrets_dir = tmp_path / "config" / "sqldash"
    secrets_dir.mkdir(parents=True)
    path = secrets_dir / "profiles.yaml"
    path.write_text("acme:\n  password: test-fixture-not-a-real-secret\n")
    return path


def test_duckdb_cannot_read_a_file_outside_the_project(tmp_path):
    """The connection's filesystem reach was the server user's whole filesystem,
    so ad-hoc SQL returned the owner's credential store verbatim. #622."""
    project = tmp_path / "project"
    project.mkdir()
    secret = _outside_secret(tmp_path)
    conn = EngineConnector(Source(type="duckdb"), project)
    try:
        conn.connect()
        with pytest.raises(ConnectorError) as exc:
            conn.execute(f"SELECT content FROM read_text('{secret}')", [], 10, CancelToken())
        assert "test-fixture-not-a-real-secret" not in str(exc.value)
        assert str(secret) in str(exc.value)
        assert "external_access: true" in str(exc.value)
        with pytest.raises(ConnectorError, match="outside"):
            conn.execute(f"SELECT file FROM glob('{tmp_path}/*')", [], 10, CancelToken())
    finally:
        conn.close()


def test_duckdb_still_reads_the_projects_own_files(tmp_path):
    """Confinement has to leave `sqldash init --demo` working: attached views and
    an explicit read_csv of a project file both stay readable."""
    project = tmp_path / "project"
    (project / "data").mkdir(parents=True)
    csv = project / "data" / "orders.csv"
    csv.write_text("k,v\na,1\nb,2\n")
    conn = EngineConnector(Source(type="duckdb", attach_files=True), project)
    try:
        conn.connect()
        assert conn.execute("SELECT COUNT(*) FROM orders", [], 10, CancelToken()).rows == [[2]]
        direct = conn.execute(f"SELECT COUNT(*) FROM read_csv('{csv}')", [], 10, CancelToken())
        assert direct.rows == [[2]]
    finally:
        conn.close()


def test_duckdb_reads_the_database_file_outside_the_dashboard_dir(tmp_path):
    """`base_dir: ../warehouse` puts the .duckdb file outside the dashboard dir;
    the allowlist has to follow the database, not just the dashboard."""
    warehouse = tmp_path / "warehouse"
    warehouse.mkdir()
    _write_duckdb(warehouse / "wh.duckdb", 11)
    dashboards = tmp_path / "dash"
    dashboards.mkdir()
    source = Source(type="duckdb", database="wh.duckdb", base_dir="../warehouse")
    conn = EngineConnector(source, dashboards)
    try:
        conn.connect()
        assert conn.execute("SELECT x FROM t", [], 10, CancelToken()).rows == [[11]]
    finally:
        conn.close()


def test_a_project_reads_its_own_files_beside_a_database_in_a_subdirectory(tmp_path):
    """`database: warehouse/w.duckdb` with csvs at the project root: confining to
    the database's own folder refused the project its own data (#622 follow-up)."""
    project = tmp_path / "project"
    project.mkdir()
    _write_duckdb(project / "warehouse" / "w.duckdb", 7)
    csv = project / "orders.csv"
    csv.write_text("id,amount\n1,10\n2,32\n")
    conn = EngineConnector(Source(type="duckdb", database="warehouse/w.duckdb"), project)
    try:
        conn.connect()
        rows = conn.execute(f"SELECT SUM(amount) FROM read_csv('{csv}')", [], 10, CancelToken())
        assert rows.rows == [[42]]
    finally:
        conn.close()


def test_base_dir_moves_the_reach_with_the_data_it_points_at(tmp_path):
    """`base_dir: ../warehouse` says the source's files live there, so that is
    what it reads. A csv left beside the dashboard is outside it — the price of a
    reach two dashboards over one file can still agree on."""
    _write_duckdb(tmp_path / "warehouse" / "wh.duckdb", 11)
    warehouse_csv = tmp_path / "warehouse" / "extra.csv"
    warehouse_csv.write_text("id,amount\n1,5\n")
    dashboards = tmp_path / "dash"
    dashboards.mkdir()
    beside = dashboards / "beside.csv"
    beside.write_text("id,amount\n1,9\n")
    source = Source(type="duckdb", database="wh.duckdb", base_dir="../warehouse")
    conn = EngineConnector(source, dashboards)
    try:
        conn.connect()
        assert conn.execute("SELECT x FROM t", [], 10, CancelToken()).rows == [[11]]
        rows = conn.execute(
            f"SELECT SUM(amount) FROM read_csv('{warehouse_csv}')", [], 10, CancelToken()
        )
        assert rows.rows == [[5]]
        with pytest.raises(ConnectorError, match="outside"):
            conn.execute(f"SELECT * FROM read_csv('{beside}')", [], 10, CancelToken())
    finally:
        conn.close()


def test_a_confined_source_still_cannot_read_outside_every_project_directory(tmp_path):
    """Widening the allowlist to the project's own directories keeps #622 closed."""
    project = tmp_path / "project"
    project.mkdir()
    _write_duckdb(project / "warehouse" / "w.duckdb", 3)
    secret = _outside_secret(tmp_path)
    conn = EngineConnector(Source(type="duckdb", database="warehouse/w.duckdb"), project)
    try:
        conn.connect()
        with pytest.raises(ConnectorError, match="outside"):
            conn.execute(f"SELECT content FROM read_text('{secret}')", [], 10, CancelToken())
        with pytest.raises(ConnectorError, match="outside"):
            conn.execute(f"SELECT file FROM glob('{secret.parent}/*')", [], 10, CancelToken())
    finally:
        conn.close()


def test_external_access_is_an_explicit_opt_in(tmp_path):
    """A project that legitimately reads outside itself says so on the source."""
    project = tmp_path / "project"
    project.mkdir()
    secret = _outside_secret(tmp_path)
    conn = EngineConnector(Source(type="duckdb", external_access=True), project)
    try:
        conn.connect()
        rows = conn.execute(f"SELECT content FROM read_text('{secret}')", [], 10, CancelToken())
        assert "test-fixture-not-a-real-secret" in rows.rows[0][0]
    finally:
        conn.close()


def test_a_confined_duckdb_cannot_turn_external_access_back_on(tmp_path):
    """`SET enable_external_access = true` is refused by DuckDB itself once the
    database is running, so the latch does not depend on sqlguard alone."""
    project = tmp_path / "project"
    project.mkdir()
    secret = _outside_secret(tmp_path)
    conn = EngineConnector(Source(type="duckdb"), project)
    try:
        conn.connect()
        with pytest.raises(ConnectorError):
            conn.execute("SET enable_external_access = true", [], 10, CancelToken())
        with pytest.raises(ConnectorError, match="outside"):
            conn.execute(f"SELECT content FROM read_text('{secret}')", [], 10, CancelToken())
    finally:
        conn.close()


def test_confinement_only_rewrites_duckdbs_permission_error(tmp_path):
    source = Source(type="duckdb")
    assert explain_confinement("Catalog Error: no such table", source, tmp_path) == (
        "Catalog Error: no such table"
    )
    blocked = (
        'Permission Error: Cannot access file "/etc/passwd" - file system '
        "operations are disabled by configuration"
    )
    assert "external_access: true" in explain_confinement(blocked, source, tmp_path)
    assert explain_confinement(blocked, Source(type="duckdb", external_access=True), tmp_path) == (
        blocked
    )


def test_sqlite_has_no_file_reader_to_confine(tmp_path):
    """Checked alongside #622: the sqlite driver ships no readfile()/glob(), and
    ATTACH is already refused as a write, so it has no comparable reach."""
    conn = EngineConnector(Source(type="sqlite", database=":memory:"), tmp_path)
    try:
        conn.connect()
        with pytest.raises(ConnectorError, match="no such function"):
            conn.execute("SELECT readfile('/etc/passwd')", [], 10, CancelToken())
    finally:
        conn.close()


def _shared_file_project(tmp_path):
    """Two dashboard dirs over one `.duckdb` file, plus a secret outside both."""
    _write_duckdb(tmp_path / "shared" / "wh.duckdb", 1)
    (tmp_path / "dash_a" / "data").mkdir(parents=True)
    (tmp_path / "dash_a" / "data" / "orders.csv").write_text("k,v\na,1\n")
    (tmp_path / "dash_b").mkdir()
    secret = _outside_secret(tmp_path)
    return tmp_path / "dash_a", tmp_path / "dash_b", secret


SHARED_DB = "../shared/wh.duckdb"


@pytest.mark.parametrize("opted_out_first", [False, True])
def test_two_sources_on_one_duckdb_file_cannot_disagree_about_reach(tmp_path, opted_out_first):
    """DuckDB keeps one database instance (and one set of access settings) per
    file per process, so the second source used to silently take the first one's
    reach: `external_access: true` overridden into a raw permission error one
    way round, a confined source handed the other project's allowlist the other.
    Whoever connects first now gets what it asked for and the other is refused.
    #622 review."""
    dash_a, dash_b, secret = _shared_file_project(tmp_path)
    confined = EngineConnector(Source(type="duckdb", database=SHARED_DB), dash_a)
    opted_out = EngineConnector(
        Source(type="duckdb", database=SHARED_DB, external_access=True), dash_b
    )
    first, second = (opted_out, confined) if opted_out_first else (confined, opted_out)
    try:
        if opted_out_first:
            rows = first.execute(
                f"SELECT content FROM read_text('{secret}')", [], 10, CancelToken()
            )
            assert "test-fixture-not-a-real-secret" in rows.rows[0][0]
        else:
            with pytest.raises(ConnectorError, match="external_access: true"):
                first.execute(f"SELECT content FROM read_text('{secret}')", [], 10, CancelToken())
        with pytest.raises(ConnectorError) as exc:
            second.execute(f"SELECT content FROM read_text('{secret}')", [], 10, CancelToken())
        assert "want different file access" in str(exc.value)
        assert "test-fixture-not-a-real-secret" not in str(exc.value)
        assert first.execute("SELECT x FROM t", [], 10, CancelToken()).rows == [[1]]
    finally:
        confined.close()
        opted_out.close()


def test_two_projects_on_one_duckdb_file_do_not_inherit_each_others_allowlist(tmp_path):
    """Two dashboards in different directories over one warehouse file. The
    second used to find external access already off, skip its own SET, and read
    the first project's files through the allowlist it inherited. Each source now
    also reaches its own project dir, so the two want different reach on one
    file: the second is refused, and neither reads the other's files. #622
    review, and the follow-up that widened the allowlist."""
    dash_a, dash_b, _ = _shared_file_project(tmp_path)
    a = EngineConnector(Source(type="duckdb", database=SHARED_DB), dash_a)
    b = EngineConnector(Source(type="duckdb", database=SHARED_DB), dash_b)
    try:
        assert a.execute("SELECT x FROM t", [], 10, CancelToken()).rows == [[1]]
        with pytest.raises(ConnectorError, match="different file access"):
            b.execute("SELECT x FROM t", [], 10, CancelToken())
    finally:
        a.close()
        b.close()


def test_two_projects_share_one_duckdb_file_by_agreeing_on_base_dir(tmp_path):
    """The remedy the conflict message names: point both at the warehouse's own
    directory and their reach is identical, so the file is shared again."""
    dash_a, dash_b, _ = _shared_file_project(tmp_path)
    shared = "../shared"
    a = EngineConnector(Source(type="duckdb", database="wh.duckdb", base_dir=shared), dash_a)
    b = EngineConnector(Source(type="duckdb", database="wh.duckdb", base_dir=shared), dash_b)
    try:
        assert a.execute("SELECT x FROM t", [], 10, CancelToken()).rows == [[1]]
        assert b.execute("SELECT x FROM t", [], 10, CancelToken()).rows == [[1]]
        csv = dash_a / "data" / "orders.csv"
        with pytest.raises(ConnectorError, match="outside"):
            b.execute(f"SELECT * FROM read_csv('{csv}')", [], 10, CancelToken())
    finally:
        a.close()
        b.close()


def test_two_sources_that_agree_still_share_one_duckdb_file(tmp_path):
    """Two dashboards in one project over one warehouse file is the ordinary
    case; only a disagreement is refused."""
    dash_a, _, secret = _shared_file_project(tmp_path)
    first = EngineConnector(Source(type="duckdb", database=SHARED_DB), dash_a)
    second = EngineConnector(Source(type="duckdb", database=SHARED_DB), dash_a)
    try:
        assert first.execute("SELECT x FROM t", [], 10, CancelToken()).rows == [[1]]
        assert second.execute("SELECT x FROM t", [], 10, CancelToken()).rows == [[1]]
        with pytest.raises(ConnectorError, match="external_access: true"):
            second.execute(f"SELECT content FROM read_text('{secret}')", [], 10, CancelToken())
    finally:
        first.close()
        second.close()


def test_memory_duckdb_sources_are_confined_independently(tmp_path):
    """An in-memory database is never shared between connections, so two
    projects' `:memory:` sources must not constrain each other."""
    (tmp_path / "dash_a" / "data").mkdir(parents=True)
    (tmp_path / "dash_a" / "data" / "orders.csv").write_text("k,v\na,1\n")
    (tmp_path / "dash_b").mkdir()
    secret = _outside_secret(tmp_path)
    confined = EngineConnector(Source(type="duckdb", attach_files=True), tmp_path / "dash_a")
    opted_out = EngineConnector(Source(type="duckdb", external_access=True), tmp_path / "dash_b")
    try:
        assert confined.execute("SELECT COUNT(*) FROM orders", [], 10, CancelToken()).rows == [[1]]
        with pytest.raises(ConnectorError, match="external_access: true"):
            confined.execute(f"SELECT content FROM read_text('{secret}')", [], 10, CancelToken())
        rows = opted_out.execute(
            f"SELECT content FROM read_text('{secret}')", [], 10, CancelToken()
        )
        assert "test-fixture-not-a-real-secret" in rows.rows[0][0]
    finally:
        confined.close()
        opted_out.close()


def test_a_nested_project_is_not_handed_the_parent_projects_reach(tmp_path):
    """A dashboard nested inside another project, sharing its warehouse file,
    asked for less than the parent claimed. Serving it on the parent's allowlist
    let it read the parent's files, which the narrow confinement refused, so a
    reach that only covers one way round is not enough. #638 review."""
    parent = tmp_path / "proj_a"
    nested = parent / "proj_b"
    nested.mkdir(parents=True)
    _write_duckdb(parent / "warehouse" / "w.duckdb", 9)
    private = parent / "secret.csv"
    private.write_text("id,amount\n1,9\n")
    outer = EngineConnector(Source(type="duckdb", database="warehouse/w.duckdb"), parent)
    inner = EngineConnector(Source(type="duckdb", database="../warehouse/w.duckdb"), nested)
    try:
        assert outer.execute("SELECT x FROM t", [], 10, CancelToken()).rows == [[9]]
        with pytest.raises(ConnectorError, match="different file access"):
            inner.execute(f"SELECT * FROM read_csv('{private}')", [], 10, CancelToken())
    finally:
        outer.close()
        inner.close()


def test_a_shared_warehouse_file_keeps_working_after_it_is_relatched(tmp_path):
    """Sibling projects beside one warehouse file: the reach is mutual, so both
    are served, and the record follows whichever connection ran the SET, so the
    first project still reads its own data when it comes back to a file the
    other one relatched."""
    _write_duckdb(tmp_path / "wh.duckdb", 2)
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()
    csv = first / "orders.csv"
    csv.write_text("id,amount\n1,7\n")
    a = EngineConnector(Source(type="duckdb", database="../wh.duckdb"), first)
    a.execute("SELECT x FROM t", [], 10, CancelToken())
    a.close()
    b = EngineConnector(Source(type="duckdb", database="../wh.duckdb"), second)
    back = EngineConnector(Source(type="duckdb", database="../wh.duckdb"), first)
    try:
        assert b.execute("SELECT x FROM t", [], 10, CancelToken()).rows == [[2]]
        assert back.execute(f"SELECT * FROM read_csv('{csv}')", [], 10, CancelToken()).rows == [
            [1, 7]
        ]
    finally:
        b.close()
        back.close()


def test_a_source_can_be_reconfigured_once_its_connector_closed(tmp_path):
    """The claim on a database file used to last the whole process, so editing a
    source to opt out was refused until restart, and the message blamed a second
    source that did not exist. #622 follow-up."""
    project = tmp_path / "project"
    project.mkdir()
    _write_duckdb(project / "wh.duckdb", 5)
    secret = _outside_secret(tmp_path)
    confined = EngineConnector(Source(type="duckdb", database="wh.duckdb"), project)
    try:
        assert confined.execute("SELECT x FROM t", [], 10, CancelToken()).rows == [[5]]
    finally:
        confined.close()
    opted_out = EngineConnector(
        Source(type="duckdb", database="wh.duckdb", external_access=True), project
    )
    try:
        rows = opted_out.execute(
            f"SELECT content FROM read_text('{secret}')", [], 10, CancelToken()
        )
        assert "test-fixture-not-a-real-secret" in rows.rows[0][0]
    finally:
        opted_out.close()


def test_a_claim_survives_while_another_engine_still_holds_the_file(tmp_path):
    """Releasing on the first close would hand the file's reach to whoever asked
    next, while a live source is still reading it."""
    project = tmp_path / "project"
    project.mkdir()
    _write_duckdb(project / "wh.duckdb", 6)
    secret = _outside_secret(tmp_path)
    first = EngineConnector(Source(type="duckdb", database="wh.duckdb"), project)
    second = EngineConnector(Source(type="duckdb", database="wh.duckdb"), project)
    try:
        assert first.execute("SELECT x FROM t", [], 10, CancelToken()).rows == [[6]]
        assert second.execute("SELECT x FROM t", [], 10, CancelToken()).rows == [[6]]
        first.close()
        opted_out = EngineConnector(
            Source(type="duckdb", database="wh.duckdb", external_access=True), project
        )
        try:
            with pytest.raises(ConnectorError, match="different file access"):
                opted_out.execute(
                    f"SELECT content FROM read_text('{secret}')", [], 10, CancelToken()
                )
        finally:
            opted_out.close()
    finally:
        second.close()


def test_an_engine_discarded_after_it_was_built_does_not_pin_the_claim(tmp_path):
    """`role:` on a duckdb source is refused *after* the engine exists, so the
    engine is discarded with its claim on the file. Counting that holder forever
    left the claim unreleasable, which is the bug this release exists to avoid.
    #639 review."""
    project = tmp_path / "project"
    project.mkdir()
    _write_duckdb(project / "wh.duckdb", 8)
    secret = _outside_secret(tmp_path)
    refused = EngineConnector(Source(type="duckdb", database="wh.duckdb", role="analyst"), project)
    with pytest.raises(ConnectorError):
        refused.execute("SELECT x FROM t", [], 10, CancelToken())
    confined = EngineConnector(Source(type="duckdb", database="wh.duckdb"), project)
    try:
        assert confined.execute("SELECT x FROM t", [], 10, CancelToken()).rows == [[8]]
    finally:
        confined.close()
    opted_out = EngineConnector(
        Source(type="duckdb", database="wh.duckdb", external_access=True), project
    )
    try:
        rows = opted_out.execute(
            f"SELECT content FROM read_text('{secret}')", [], 10, CancelToken()
        )
        assert "test-fixture-not-a-real-secret" in rows.rows[0][0]
    finally:
        opted_out.close()


def test_editing_a_served_source_to_widen_its_access_asks_for_a_restart(tmp_path):
    """#639 made an edited source replace its own retired engine so the edit took
    effect live. That could not tell an edit from a second dashboard on the same
    file, and a widening one then disposed a confined engine and reopened the
    file unrestricted (#641). Widening now leaves the incumbent alone and says to
    restart; the reach it asks for is never granted under a running source."""
    from sqldash.execution import ExecutionRegistry

    project = tmp_path / "project"
    project.mkdir()
    _write_duckdb(project / "wh.duckdb", 12)
    secret = _outside_secret(tmp_path)
    registry = ExecutionRegistry()
    try:
        confined = Source(type="duckdb", database="wh.duckdb")
        assert registry.run_sync(confined, project, "SELECT x FROM t", [], 10).rows == [[12]]
        opted_out = Source(type="duckdb", database="wh.duckdb", external_access=True)
        with pytest.raises(ConnectorError, match="restart the server"):
            registry.run_sync(
                opted_out, project, f"SELECT content FROM read_text('{secret}')", [], 10
            )
    finally:
        registry.shutdown()


def test_a_source_with_the_same_reach_is_served_off_a_still_live_instance(tmp_path):
    """A dispose can leave a connection holding the DuckDB instance, so the next
    connector finds the file latched with no claim recorded. DuckDB still knows
    the allowlist, so a source whose reach it covers is served rather than told
    to restart. #639 review."""
    project = tmp_path / "project"
    project.mkdir()
    _write_duckdb(project / "wh.duckdb", 13)
    csv = project / "orders.csv"
    csv.write_text("id,amount\n1,4\n")
    first = EngineConnector(Source(type="duckdb", database="wh.duckdb"), project)
    pooled = first.engine.connect()
    try:
        first.execute("SELECT x FROM t", [], 10, CancelToken())
        first.close()
        again = EngineConnector(Source(type="duckdb", database="wh.duckdb"), project)
        try:
            rows = again.execute(f"SELECT * FROM read_csv('{csv}')", [], 10, CancelToken())
            assert rows.rows == [[1, 4]]
        finally:
            again.close()
    finally:
        pooled.close()


def test_a_second_source_cannot_switch_confinement_off_for_a_file(tmp_path):
    """Two dashboards over one warehouse file, one of them
    `external_access: true`. The registry retired the incumbent engine for any
    replacement on the same file, so the opted-out source disposed the confined
    one and reopened the file unrestricted: confinement was off for whoever asked
    last, and the refusal never fired on the server path. #641."""
    from sqldash.execution import ExecutionRegistry

    project = tmp_path / "project"
    project.mkdir()
    _write_duckdb(project / "wh.duckdb", 1)
    secret = _outside_secret(tmp_path)
    registry = ExecutionRegistry()
    confined = Source(type="duckdb", database="wh.duckdb")
    opted_out = Source(type="duckdb", database="wh.duckdb", external_access=True)
    try:
        assert registry.run_sync(confined, project, "SELECT x FROM t", [], 10).rows == [[1]]
        with pytest.raises(ConnectorError, match="different file access") as exc:
            registry.run_sync(
                opted_out, project, f"SELECT content FROM read_text('{secret}')", [], 10
            )
        assert "restart the server" in str(exc.value)
        with pytest.raises(ConnectorError, match="outside"):
            registry.run_sync(
                confined, project, f"SELECT content FROM read_text('{secret}')", [], 10
            )
    finally:
        registry.shutdown()


def test_an_edit_that_does_not_widen_still_retires_the_old_engine(tmp_path):
    """The narrowing half of the same rule has to keep working: an edited source
    asking for no more reach than the incumbent still replaces it live, which is
    what #639 fixed."""
    from sqldash.execution import ExecutionRegistry

    project = tmp_path / "project"
    (project / "data").mkdir(parents=True)
    _write_duckdb(project / "data" / "wh.duckdb", 2)
    registry = ExecutionRegistry()
    wide = Source(type="duckdb", database="data/wh.duckdb")
    narrow = Source(type="duckdb", database="wh.duckdb", base_dir="data")
    try:
        assert registry.run_sync(wide, project, "SELECT x FROM t", [], 10).rows == [[2]]
        assert registry.run_sync(narrow, project, "SELECT x FROM t", [], 10).rows == [[2]]
    finally:
        registry.shutdown()


def test_widening_by_repointing_base_dir_is_refused_too(tmp_path):
    """Widening is not only `external_access: true`. Moving a source's reach up a
    level asks for more of the filesystem just as much, and the incumbent stays
    confined. #641 review."""
    from sqldash.execution import ExecutionRegistry

    project = tmp_path / "project"
    (project / "data").mkdir(parents=True)
    _write_duckdb(project / "data" / "wh.duckdb", 3)
    beside = project / "beside.csv"
    beside.write_text("id,amount\n1,5\n")
    registry = ExecutionRegistry()
    narrow = Source(type="duckdb", database="wh.duckdb", base_dir="data")
    wider = Source(type="duckdb", database="data/wh.duckdb")
    try:
        assert registry.run_sync(narrow, project, "SELECT x FROM t", [], 10).rows == [[3]]
        with pytest.raises(ConnectorError, match="restart the server"):
            registry.run_sync(wider, project, f"SELECT * FROM read_csv('{beside}')", [], 10)
        with pytest.raises(ConnectorError, match="outside"):
            registry.run_sync(narrow, project, f"SELECT * FROM read_csv('{beside}')", [], 10)
    finally:
        registry.shutdown()


def test_a_predicate_that_cannot_answer_keeps_the_file_confined(tmp_path):
    """`still_declared` is the only thing that lets an edit widen a file's reach
    (#642), so a store that fails to answer must read as "still declared"."""
    from sqldash.execution import ExecutionRegistry

    project = tmp_path / "project"
    project.mkdir()
    _write_duckdb(project / "wh.duckdb", 4)
    secret = _outside_secret(tmp_path)

    def broken(source, base_dir):
        raise OSError("store unreadable")

    registry = ExecutionRegistry(still_declared=broken)
    confined = Source(type="duckdb", database="wh.duckdb")
    opted_out = Source(type="duckdb", database="wh.duckdb", external_access=True)
    try:
        assert registry.run_sync(confined, project, "SELECT x FROM t", [], 10).rows == [[4]]
        with pytest.raises(ConnectorError, match="different file access"):
            registry.run_sync(
                opted_out, project, f"SELECT content FROM read_text('{secret}')", [], 10
            )
        with pytest.raises(ConnectorError, match="outside"):
            registry.run_sync(
                confined, project, f"SELECT content FROM read_text('{secret}')", [], 10
            )
    finally:
        registry.shutdown()


def test_an_undeclared_incumbent_gives_way_to_a_widening_edit(tmp_path):
    """With the store saying the confined config is gone, the widening source is
    an edit and is served, cache hit or miss (#642)."""
    from sqldash.execution import ExecutionRegistry

    project = tmp_path / "project"
    project.mkdir()
    _write_duckdb(project / "wh.duckdb", 9)
    secret = _outside_secret(tmp_path)
    confined = Source(type="duckdb", database="wh.duckdb")
    opted_out = Source(type="duckdb", database="wh.duckdb", external_access=True)
    declared = {confined.model_dump_json(), opted_out.model_dump_json()}
    registry = ExecutionRegistry(
        still_declared=lambda source, base_dir: source.model_dump_json() in declared
    )
    read = f"SELECT content FROM read_text('{secret}')"
    try:
        assert registry.run_sync(confined, project, "SELECT x FROM t", [], 10).rows == [[9]]
        with pytest.raises(ConnectorError, match="different file access"):
            registry.run_sync(opted_out, project, read, [], 10)
        declared.discard(confined.model_dump_json())
        rows = registry.run_sync(opted_out, project, read, [], 10).rows
        assert "test-fixture-not-a-real-secret" in rows[0][0]
    finally:
        registry.shutdown()
