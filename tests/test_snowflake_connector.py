import itertools
import threading
import time
import uuid
from decimal import Decimal
from typing import ClassVar

import pytest
from sqlalchemy.exc import DBAPIError

from sqldash.connectors.base import CancelToken, ConnectionLost, ConnectorError
from sqldash.connectors.engine import (
    SNOWFLAKE_TIMEOUT,
    EngineConnector,
    _clean,
    _tables_from_rows,
    paramstyle_for,
    snowflake_session_gone,
)
from sqldash.connectors.engine_urls import snowflake_connect_kwargs
from sqldash.models.source import Source
from sqldash.secrets import resolve_credentials


def make_source(**fields) -> Source:
    return Source.model_validate(
        {
            "type": "snowflake",
            "account": "acme-xy123",
            "warehouse": "WH",
            "database": "DB",
            "schema": "PUBLIC",
            "role": "ANALYST",
            **fields,
        }
    )


def kwargs_for(source, profiles=None):
    return snowflake_connect_kwargs(source, resolve_credentials(source, profiles or {}))


def test_schema_keeps_number_precision_and_scale():
    """Snowflake's information_schema says `NUMBER` and keeps (38,10) in
    numeric_precision/numeric_scale, so get_schema and the schema browser showed
    every NUMBER column the same whatever its scale."""
    rows = [
        ("PUBLIC", "T", "I", "NUMBER", 38, 0),
        ("PUBLIC", "T", "D", "NUMBER", 38, 10),
        ("PUBLIC", "T", "F", "FLOAT", None, None),
        ("PUBLIC", "T", "S", "TEXT", None, None),
        ("PUBLIC", "U", "P", "numeric", 10, 2),
        ("PUBLIC", "U", "N", "numeric", None, None),
        ("PUBLIC", "U", "K", "integer", 32, 0),
    ]
    tables = _tables_from_rows(rows)
    assert [(t.name, t.columns) for t in tables] == [
        ("T", [("I", "NUMBER(38,0)"), ("D", "NUMBER(38,10)"), ("F", "FLOAT"), ("S", "TEXT")]),
        ("U", [("P", "numeric(10,2)"), ("N", "numeric"), ("K", "integer")]),
    ]
    legacy = _tables_from_rows([("main", "t", "d", "DECIMAL(18,3)")])
    assert legacy[0].columns == [("d", "DECIMAL(18,3)")]


def test_paramstyle():
    assert paramstyle_for(make_source()) == "pyformat"


def test_externalbrowser_default():
    kwargs = kwargs_for(make_source(username="ada@acme.com"))
    assert kwargs["authenticator"] == "externalbrowser"
    assert kwargs["client_store_temporary_credential"] is True
    assert kwargs["account"] == "acme-xy123"
    assert kwargs["schema"] == "PUBLIC"
    assert kwargs["user"] == "ada@acme.com"
    assert "password" not in kwargs
    assert kwargs["login_timeout"] == 30
    assert "network_timeout" not in kwargs


def test_connect_args_reach_the_snowflake_creator(monkeypatch):
    """SQLAlchemy connect_args never apply when we pass creator=. A dashboard
    that set connect_args.network_timeout used to parse and do nothing, so
    every query still died at the old 60s timebomb."""
    monkeypatch.setenv("SF_TIMEOUT", "600")
    kwargs = kwargs_for(
        make_source(
            username="ada@acme.com",
            connect_args={"network_timeout": "${env:SF_TIMEOUT}", "session_parameters": {"x": 1}},
        )
    )
    assert kwargs["network_timeout"] == "600"
    assert kwargs["session_parameters"] == {"x": 1}


def test_token_implies_pat(monkeypatch):
    monkeypatch.setenv("SF_PAT", "pat-token-xyz")
    kwargs = kwargs_for(make_source(username="svc", token="${env:SF_PAT}"))
    assert kwargs["authenticator"] == "PROGRAMMATIC_ACCESS_TOKEN"
    assert kwargs["token"] == "pat-token-xyz"


def test_explicit_pat_missing_token_message():
    with pytest.raises(ConnectorError, match="token"):
        kwargs_for(make_source(username="svc", authentication="pat"))


def test_password_implies_password_auth(monkeypatch):
    monkeypatch.setenv("SF_PW", "hunter2")
    kwargs = kwargs_for(make_source(username="svc", password="${env:SF_PW}"))
    assert kwargs["password"] == "hunter2"
    assert "authenticator" not in kwargs


def test_keypair_kwargs():
    kwargs = kwargs_for(
        make_source(username="svc", authentication="keypair", private_key_path="~/.ssh/sf.p8")
    )
    assert kwargs["private_key_file"].endswith("/.ssh/sf.p8")


def test_profile_merge(monkeypatch, tmp_path):
    profile = tmp_path / "profiles.yaml"
    profile.write_text("acme:\n  username: svc\n  token: from-profile\n")
    monkeypatch.setattr("sqldash.secrets.profiles_path", lambda: profile)
    kwargs = snowflake_connect_kwargs(
        make_source(profile="acme"), resolve_credentials(make_source(profile="acme"))
    )
    assert kwargs["user"] == "svc"
    assert kwargs["token"] == "from-profile"


def test_bad_authentication_rejected():
    with pytest.raises(Exception, match="authentication"):
        make_source(authentication="magic")


class FakeCursor:
    description: ClassVar[list[tuple]] = [("COL", 2, None, None, None, None, None)]
    rowcount = 1

    def __init__(self):
        self._row = ("FAKE_DB", "PUBLIC")

    def execute(self, sql, *args, **kwargs):
        if "version" in str(sql).lower():
            self._row = ("8.0.0",)
        return self

    def fetchall(self):
        return [self._row]

    def fetchone(self):
        return self._row

    def close(self):
        pass


class FakeConn:
    def cursor(self):
        return FakeCursor()

    def close(self):
        pass

    def rollback(self):
        pass

    def autocommit(self, value):
        pass


def test_connects_are_serialized(monkeypatch):
    import threading
    import time as time_module

    snowflake_connector = pytest.importorskip("snowflake.connector")

    from sqldash.connectors.engine import build_engine

    spans = []

    def slow_connect(**kwargs):
        start = time_module.monotonic()
        time_module.sleep(0.15)
        spans.append((start, time_module.monotonic()))
        return FakeConn()

    monkeypatch.setattr(snowflake_connector, "connect", slow_connect)
    engine = build_engine(make_source(username="ada@acme.com"), None)
    try:
        threads = [threading.Thread(target=engine.raw_connection) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        engine.dispose()
    assert len(spans) == 4
    ordered = sorted(spans)
    for (_, end_a), (start_b, _) in itertools.pairwise(ordered):
        assert start_b >= end_a - 0.01


def test_idp_mismatch_gets_hint(monkeypatch):
    snowflake_connector = pytest.importorskip("snowflake.connector")

    from sqldash.connectors.engine import build_engine

    def failing_connect(**kwargs):
        raise RuntimeError(
            "250001 (08001): The user you were trying to authenticate as differs "
            "from the user currently logged in at the IDP."
        )

    monkeypatch.setattr(snowflake_connector, "connect", failing_connect)
    engine = build_engine(make_source(username="ada@acme.com"), None)
    try:
        with pytest.raises(ConnectorError, match="must exactly match the account you sign in"):
            engine.raw_connection()
    finally:
        engine.dispose()


def test_auth_failure_cooldown_blocks_repeat_attempts(monkeypatch):
    snowflake_connector = pytest.importorskip("snowflake.connector")

    from sqldash.connectors.engine import build_engine

    calls = {"n": 0}

    def failing_connect(**kwargs):
        calls["n"] += 1
        raise RuntimeError("differs from the user currently logged in at the IDP.")

    monkeypatch.setattr(snowflake_connector, "connect", failing_connect)
    engine = build_engine(make_source(username="ada@acme.com"), None)
    try:
        with pytest.raises(ConnectorError, match=r"attempted user: 'ada@acme\.com'"):
            engine.raw_connection()
        with pytest.raises(ConnectorError, match="attempts paused"):
            engine.raw_connection()
        assert calls["n"] == 1
    finally:
        engine.dispose()


def test_env_refs_expand_in_every_connection_field(monkeypatch):
    """`interpolate_env` was applied per use site, and `warehouse`/`role`/`schema`
    never got a call — so the driver received the literal string
    "${env:SNOWFLAKE_WAREHOUSE}". The connection succeeds over SSO and every
    query then fails with "No active warehouse selected in the current session",
    which reads like a grants problem rather than an unexpanded reference."""
    for var, value in [
        ("SNOWFLAKE_ACCOUNT", "acct123"),
        ("SNOWFLAKE_WAREHOUSE", "WH_BIG"),
        ("SNOWFLAKE_ROLE", "ANALYST"),
        ("SNOWFLAKE_SCHEMA", "PUBLIC"),
        ("SNOWFLAKE_DB", "PROD"),
        ("SNOWFLAKE_USER", "me@example.com"),
    ]:
        monkeypatch.setenv(var, value)
    source = Source(
        type="snowflake",
        account="${env:SNOWFLAKE_ACCOUNT}",
        warehouse="${env:SNOWFLAKE_WAREHOUSE}",
        role="${env:SNOWFLAKE_ROLE}",
        database="${env:SNOWFLAKE_DB}",
        schema="${env:SNOWFLAKE_SCHEMA}",
        authentication="externalbrowser",
        username="${env:SNOWFLAKE_USER}",
    )
    kwargs = snowflake_connect_kwargs(source, resolve_credentials(source, {}))
    assert kwargs["warehouse"] == "WH_BIG"
    assert kwargs["role"] == "ANALYST"
    assert kwargs["database"] == "PROD"
    assert kwargs["schema"] == "PUBLIC"
    assert kwargs["account"] == "acct123"
    assert kwargs["user"] == "me@example.com"


def test_an_unset_variable_in_a_connection_field_names_itself(monkeypatch):
    """Passing the reference through meant the failure surfaced from the
    warehouse instead of from sqldash. The credential path already fails by
    name; this one has to as well."""
    from sqldash.secrets import SecretError

    monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "acct123")
    monkeypatch.delenv("SNOWFLAKE_WAREHOUSE", raising=False)
    source = Source(
        type="snowflake",
        account="${env:SNOWFLAKE_ACCOUNT}",
        warehouse="${env:SNOWFLAKE_WAREHOUSE}",
        authentication="externalbrowser",
        username="me@example.com",
    )
    with pytest.raises(SecretError, match="SNOWFLAKE_WAREHOUSE"):
        snowflake_connect_kwargs(source, resolve_credentials(source, {}))


@pytest.mark.parametrize(
    ("source_kwargs", "probe"),
    [
        (
            {"type": "postgres", "host": "${env:PGHOST}", "database": "${env:PGDB}"},
            ("db.example.com", "analytics"),
        ),
        (
            {"type": "bigquery", "project": "${env:GCP_PROJECT}", "database": "${env:PGDB}"},
            ("proj-1", "analytics"),
        ),
        (
            {
                "type": "databricks",
                "host": "${env:DBX_HOST}",
                "http_path": "${env:DBX_PATH}",
                "token": "t",
                "catalog": "${env:DBX_CATALOG}",
            },
            ("db.example.com", "/sql/1.0/x", "catalog=main"),
        ),
        (
            {"type": "athena", "host": "${env:AWS_REGION}", "database": "${env:PGDB}"},
            ("us-east-1", "analytics"),
        ),
    ],
)
def test_env_refs_expand_for_every_engine(monkeypatch, tmp_path, source_kwargs, probe):
    """The same per-use-site gap left different fields unexpanded per engine."""
    from sqldash.connectors.engine_urls import build_url

    for var, value in [
        ("PGHOST", "db.example.com"),
        ("PGDB", "analytics"),
        ("GCP_PROJECT", "proj-1"),
        ("DBX_HOST", "db.example.com"),
        ("DBX_PATH", "/sql/1.0/x"),
        ("DBX_CATALOG", "main"),
        ("AWS_REGION", "us-east-1"),
    ]:
        monkeypatch.setenv(var, value)
    from urllib.parse import unquote

    url = unquote(str(build_url(Source(**source_kwargs), tmp_path)))
    assert "${env:" not in url, url
    for expected in probe:
        assert expected in url, (expected, url)


def test_an_explicit_url_does_not_resolve_the_fields_it_overrides(monkeypatch, tmp_path):
    """`url` wins verbatim and build_url never reads the flat fields, so a
    leftover `host:` beside it is dead — lint already says so. Expanding the
    whole model up front turned that dead field into a hard failure naming a
    variable with nothing to do with the connection."""
    from sqldash.connectors.engine_urls import build_url

    monkeypatch.delenv("UNSET_HOST_VAR", raising=False)
    source = Source(url="duckdb:///:memory:", host="${env:UNSET_HOST_VAR}")
    assert str(build_url(source, tmp_path)) == "duckdb:///:memory:"


def test_the_snowflake_path_does_not_resolve_options_it_never_sends(monkeypatch):
    """Same shape on the other path: `snowflake_connect_kwargs` ignores
    `options`, so an unset ref there must not fail a connection that does not
    use it."""
    monkeypatch.setenv("SNOWFLAKE_WAREHOUSE", "WH")
    monkeypatch.delenv("NEVER_SET_VAR", raising=False)
    source = Source(
        type="snowflake",
        account="acct",
        warehouse="${env:SNOWFLAKE_WAREHOUSE}",
        authentication="externalbrowser",
        username="me@example.com",
        options={"x": "${env:NEVER_SET_VAR}"},
    )
    assert snowflake_connect_kwargs(source, resolve_credentials(source, {}))["warehouse"] == "WH"


def test_attached_sibling_files_follow_the_resolved_database(monkeypatch, tmp_path):
    """`data_dir` is derived from `database`, so an unexpanded "${env:DB}"
    pointed the sibling-file scan at the wrong directory and the views silently
    never appeared. Driven through `build_engine` because the defect was at the
    call site — calling `_duckdb_attach_sql` with an already-resolved argument
    tests nothing. A base_dir/<literal> happens to have base_dir as its parent,
    which hides this unless the database sits in a subdirectory.
    """
    import duckdb
    from sqlalchemy import text

    from sqldash.connectors.engine import build_engine

    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "insub.csv").write_text("a,b\n9,9\n")
    duckdb.connect(str(tmp_path / "sub" / "w.duckdb")).close()
    monkeypatch.setenv("AT_DB", "sub/w.duckdb")

    source = Source(type="duckdb", database="${env:AT_DB}", attach_files=True)
    engine = build_engine(source, tmp_path)
    with engine.connect() as conn:
        assert conn.execute(text("SELECT a FROM insub")).scalar() == 9


def test_an_unanswered_browser_signin_fails_instead_of_hanging(monkeypatch):
    """The driver waits for the SSO callback with no timeout, and login_timeout
    does not cover it. One unanswered sign-in used to hold the connect lock
    forever, so every request hung with nothing on screen. The wait is bounded,
    later requests fail fast without opening a second tab, and a sign-in that
    finishes late is reused rather than thrown away."""
    import threading
    import time as time_module

    snowflake_connector = pytest.importorskip("snowflake.connector")

    from sqldash.connectors import engine as engine_module
    from sqldash.connectors.engine import build_engine

    signed_in = threading.Event()
    calls = {"n": 0}

    def browser_connect(**kwargs):
        calls["n"] += 1
        signed_in.wait(3)
        return FakeConn()

    monkeypatch.setattr(snowflake_connector, "connect", browser_connect)
    monkeypatch.setattr(engine_module, "SIGNIN_WAIT", 0.3, raising=False)
    monkeypatch.setattr(engine_module, "SIGNIN_RECHECK", 0.05, raising=False)
    engine = build_engine(make_source(username="ada@acme.com"), None)
    try:
        start = time_module.monotonic()
        with pytest.raises(ConnectorError, match="waiting for you to sign in"):
            engine.raw_connection()
        assert time_module.monotonic() - start < 2
        start = time_module.monotonic()
        with pytest.raises(ConnectorError, match="waiting for you to sign in"):
            engine.raw_connection()
        assert time_module.monotonic() - start < 0.25
        assert calls["n"] == 1
        signed_in.set()
        time_module.sleep(0.1)
        engine.raw_connection().close()
        assert calls["n"] == 1
    finally:
        signed_in.set()
        engine.dispose()


def test_an_abandoned_browser_signin_can_be_retried(monkeypatch):
    """A closed sign-in tab never calls back, so the pending attempt would block
    every retry until restart. Past SIGNIN_ABANDON the next request starts over."""
    import threading
    import time as time_module

    snowflake_connector = pytest.importorskip("snowflake.connector")

    from sqldash.connectors import engine as engine_module
    from sqldash.connectors.engine import build_engine

    never = threading.Event()
    calls = {"n": 0}

    closed = []

    class TrackedConn(FakeConn):
        def __init__(self, number):
            self.number = number

        def close(self):
            closed.append(self.number)

    def connect(**kwargs):
        calls["n"] += 1
        number = calls["n"]
        if number == 1:
            never.wait(5)
        return TrackedConn(number)

    monkeypatch.setattr(snowflake_connector, "connect", connect)
    monkeypatch.setattr(engine_module, "SIGNIN_WAIT", 0.2, raising=False)
    monkeypatch.setattr(engine_module, "SIGNIN_RECHECK", 0.05, raising=False)
    monkeypatch.setattr(engine_module, "SIGNIN_ABANDON", 0.5, raising=False)
    engine = build_engine(make_source(username="ada@acme.com"), None)
    try:
        with pytest.raises(ConnectorError, match="waiting for you to sign in"):
            engine.raw_connection()
        with pytest.raises(ConnectorError, match="waiting for you to sign in"):
            engine.raw_connection()
        assert calls["n"] == 1
        time_module.sleep(0.5)
        engine.raw_connection().close()
        assert calls["n"] == 2
        never.set()
        time_module.sleep(0.2)
        assert closed == [1], "the abandoned sign-in's late connection is closed, not leaked"
    finally:
        never.set()
        engine.dispose()


def test_other_auth_methods_connect_on_the_calling_thread(monkeypatch):
    import threading

    snowflake_connector = pytest.importorskip("snowflake.connector")

    from sqldash.connectors.engine import build_engine

    seen = []

    def connect(**kwargs):
        seen.append(threading.current_thread())
        return FakeConn()

    monkeypatch.setattr(snowflake_connector, "connect", connect)
    monkeypatch.setenv("SNOWFLAKE_TEST_PASSWORD", "hunter2")
    source = make_source(username="ada@acme.com", password="${env:SNOWFLAKE_TEST_PASSWORD}")
    engine = build_engine(source, None)
    try:
        engine.raw_connection().close()
    finally:
        engine.dispose()
    assert seen == [threading.current_thread()]


class AsyncWarehouse:
    """Stands in for Snowflake's async query API: a query runs until finished or cancelled."""

    def __init__(self, rows=None, error=None, network_timeout=None, description=None):
        self.rows = rows if rows is not None else [(1,), (2,), (3,)]
        self.description = description or [("N", 0, None, None, None, None, None)]
        self.error = error
        self.network_timeout = network_timeout
        self.finished = threading.Event()
        self.submitted = []
        self.cancelled = []
        self.polls = 0

    def status(self, qid):
        self.polls += 1
        if qid in self.cancelled:
            return self.query_status.ABORTED
        if self.finished.is_set():
            return self.query_status.FAILED_WITH_ERROR if self.error else self.query_status.SUCCESS
        return self.query_status.RUNNING


class AsyncCursor(FakeCursor):
    def __init__(self, warehouse):
        super().__init__()
        self.warehouse = warehouse
        self.sfqid = None
        self._pending = []

    def execute(self, sql, *args, **kwargs):
        if "SYSTEM$CANCEL_QUERY" in sql:
            self.warehouse.cancelled.append(args[0][0])
            return self
        return super().execute(sql, *args, **kwargs)

    def execute_async(self, sql, *args):
        self.warehouse.submitted.append((sql, *args))
        self.sfqid = str(uuid.uuid4())
        return {"queryId": self.sfqid}

    def query_result(self, qid):
        if self.warehouse.error:
            raise self.warehouse.error
        self.description = self.warehouse.description
        self._pending = list(self.warehouse.rows)
        return self

    def fetchmany(self, size):
        batch, self._pending = self._pending[:size], self._pending[size:]
        return batch


class AsyncConn(FakeConn):
    def __init__(self, warehouse):
        self.warehouse = warehouse
        self.network_timeout = warehouse.network_timeout

    def cursor(self):
        return AsyncCursor(self.warehouse)

    def commit(self):
        pass

    def get_query_status(self, qid):
        return self.warehouse.status(qid)

    def is_still_running(self, status):
        return self.warehouse.connection_class.is_still_running(status)


def async_connector(monkeypatch, warehouse):
    snowflake_connector = pytest.importorskip("snowflake.connector")
    warehouse.query_status = pytest.importorskip("snowflake.connector.constants").QueryStatus
    warehouse.connection_class = snowflake_connector.SnowflakeConnection
    monkeypatch.setattr(snowflake_connector, "connect", lambda **kwargs: AsyncConn(warehouse))
    monkeypatch.setenv("SNOWFLAKE_TEST_PASSWORD", "hunter2")
    source = make_source(username="ada@acme.com", password="${env:SNOWFLAKE_TEST_PASSWORD}")
    return EngineConnector(source, None)


def test_a_snowflake_query_is_submitted_async_and_read_after_it_finishes(monkeypatch):
    warehouse = AsyncWarehouse(rows=[(n,) for n in range(5)])
    connector = async_connector(monkeypatch, warehouse)
    threading.Timer(0.3, warehouse.finished.set).start()
    try:
        result = connector.execute(
            "SELECT n FROM t WHERE x = %(x)s", {"x": 1}, row_limit=3, cancel_token=CancelToken()
        )
    finally:
        connector.close()
    assert warehouse.submitted == [("SELECT n FROM t WHERE x = %(x)s", {"x": 1})]
    assert [row[0] for row in result.rows] == [0, 1, 2]
    assert result.truncated
    assert 2 <= warehouse.polls <= 8, "polling backs off instead of spinning"
    assert warehouse.cancelled == []


def test_snowflake_semi_structured_columns_are_typed_json_and_parsed(monkeypatch):
    """Snowflake describes columns by numeric field id, which no name mapping
    matched, so VARIANT/ARRAY/OBJECT/GEOGRAPHY were typed from their value, the
    driver's indented JSON text, and reached every surface as a multi-line
    string. A SQL NULL inside an ARRAY came through as `undefined`."""
    description = [
        (name, code, None, None, None, scale, True)
        for name, code, scale in [
            ("V", 5, None),
            ("ARR", 10, None),
            ("OBJ", 9, None),
            ("G", 14, None),
            ("WKT", 14, None),
            ("I", 0, 0),
            ("D", 0, 10),
            ("NULLS", 0, 2),
            ("F", 1, None),
            ("B", 11, None),
        ]
    ]
    row = (
        '{\n  "a": 1,\n  "b": [\n    1,\n    2\n  ]\n}',
        '[\n  1,\n  undefined,\n  "undefined"\n]',
        '{\n  "k": "v"\n}',
        '{\n  "coordinates": [\n    1,\n    2\n  ],\n  "type": "Point"\n}',
        "POINT(1 2)",
        12345678901234567890,
        Decimal("12.5000000000"),
        None,
        float("nan"),
        b"ab",
    )
    warehouse = AsyncWarehouse(rows=[row], description=description)
    warehouse.finished.set()
    connector = async_connector(monkeypatch, warehouse)
    try:
        result = connector.execute("SELECT v", [], row_limit=10, cancel_token=CancelToken())
    finally:
        connector.close()
    assert [c.type for c in result.columns] == [
        "json",
        "json",
        "json",
        "json",
        "json",
        "integer",
        "decimal",
        "decimal",
        "float",
        "binary",
    ]
    assert result.rows == [
        [
            {"a": 1, "b": [1, 2]},
            [1, None, "undefined"],
            {"k": "v"},
            {"coordinates": [1, 2], "type": "Point"},
            "POINT(1 2)",
            12345678901234567890,
            "12.5000000000",
            None,
            "NaN",
            "6162",
        ]
    ]


def test_cancelling_a_running_snowflake_query_reaches_the_warehouse(monkeypatch):
    """The blocking execute() only learns the query id once the query is done, so
    a cancel had no id to send and the query ran on, billed, to completion."""
    warehouse = AsyncWarehouse()
    connector = async_connector(monkeypatch, warehouse)
    token = CancelToken()
    outcome = {}

    def run():
        try:
            connector.execute("SELECT slow()", [], row_limit=10, cancel_token=token)
        except ConnectorError as exc:
            outcome["error"] = str(exc)
            outcome["at"] = time.monotonic()

    worker = threading.Thread(target=run)
    worker.start()
    try:
        time.sleep(1.5)
        cancelled_at = time.monotonic()
        token.cancel()
        worker.join(2)
    finally:
        warehouse.finished.set()
        worker.join(2)
        connector.close()
    assert outcome["error"] == "query cancelled"
    assert outcome["at"] - cancelled_at < 0.5
    assert len(warehouse.cancelled) == 1
    assert str(uuid.UUID(warehouse.cancelled[0])) == warehouse.cancelled[0]


def test_a_cancel_before_submission_still_cancels_the_submitted_query(monkeypatch):
    warehouse = AsyncWarehouse()
    connector = async_connector(monkeypatch, warehouse)
    token = CancelToken()
    token.cancel()
    try:
        with pytest.raises(ConnectorError, match="query cancelled"):
            connector.execute("SELECT slow()", [], row_limit=10, cancel_token=token)
    finally:
        connector.close()
    assert len(warehouse.cancelled) == 1


def test_a_failed_snowflake_query_keeps_the_drivers_message(monkeypatch):
    errors = pytest.importorskip("snowflake.connector.errors")

    message = (
        "002003 (42S02): SQL compilation error:\n"
        "Object 'DOES_NOT_EXIST' does not exist or not authorized."
    )
    warehouse = AsyncWarehouse(error=errors.ProgrammingError(msg=message, done_format_msg=True))
    warehouse.finished.set()
    connector = async_connector(monkeypatch, warehouse)
    try:
        with pytest.raises(ConnectorError) as caught:
            connector.execute("SELECT * FROM does_not_exist", [], 10, CancelToken())
    finally:
        connector.close()
    assert str(caught.value) == message


def test_snowflake_network_timeout_still_caps_the_query(monkeypatch):
    warehouse = AsyncWarehouse(network_timeout=0.2)
    connector = async_connector(monkeypatch, warehouse)
    try:
        with pytest.raises(ConnectorError) as caught:
            connector.execute("SELECT slow()", [], 10, CancelToken())
    finally:
        warehouse.finished.set()
        connector.close()
    assert str(caught.value) == SNOWFLAKE_TIMEOUT
    assert len(warehouse.cancelled) == 1


def session_gone_error():
    errors = pytest.importorskip("snowflake.connector.errors")
    return errors.ProgrammingError(
        msg="Session no longer exists.  New login required to access the service.",
        errno=390111,
        send_telemetry=False,
    )


class KilledCursor(AsyncCursor):
    def execute(self, sql, *args, **kwargs):
        raise session_gone_error()

    def execute_async(self, sql, *args):
        raise session_gone_error()


class KilledMidQueryCursor(AsyncCursor):
    def execute_async(self, sql, *args):
        raise session_gone_error()


class KillableConn(AsyncConn):
    killed_cursor = KilledCursor

    def __init__(self, warehouse):
        super().__init__(warehouse)
        self.killed = False

    def cursor(self):
        return self.killed_cursor(self.warehouse) if self.killed else AsyncCursor(self.warehouse)


def killable_connector(monkeypatch, warehouse):
    connector = async_connector(monkeypatch, warehouse)
    opened = []

    def connect(**kwargs):
        opened.append(KillableConn(warehouse))
        return opened[-1]

    monkeypatch.setattr(pytest.importorskip("snowflake.connector"), "connect", connect)
    return connector, opened


def test_a_killed_pooled_session_is_replaced_at_checkout(monkeypatch):
    """snowflake-sqlalchemy never classifies 390111 as a disconnect, so pre-ping
    re-raised it and every killed pooled session failed one run before recovering."""
    warehouse = AsyncWarehouse(rows=[(1,)])
    warehouse.finished.set()
    connector, opened = killable_connector(monkeypatch, warehouse)
    try:
        connector.execute("SELECT 1", [], 10, CancelToken())
        opened[0].killed = True
        result = connector.execute("SELECT 1", [], 10, CancelToken())
    finally:
        connector.close()
    assert [tuple(row) for row in result.rows] == [(1,)]
    assert len(opened) == 2


def test_a_session_killed_mid_query_is_a_lost_connection(monkeypatch):
    """ConnectionLost is what earns the registry's one retry on a fresh session."""
    warehouse = AsyncWarehouse(rows=[(1,)])
    warehouse.finished.set()
    connector, opened = killable_connector(monkeypatch, warehouse)
    monkeypatch.setattr(KillableConn, "killed_cursor", KilledMidQueryCursor)
    try:
        connector.execute("SELECT 1", [], 10, CancelToken())
        opened[0].killed = True
        with pytest.raises(ConnectionLost) as caught:
            connector.execute("SELECT 1", [], 10, CancelToken())
    finally:
        connector.close()
    assert "New login required" not in str(caught.value)
    assert "Snowflake ended this session" in str(caught.value)


@pytest.mark.parametrize(
    ("errno", "sqlstate", "gone"),
    [
        (390111, None, True),
        (390112, None, True),
        (390114, None, True),
        (250002, "08003", True),
        (250001, "08001", True),
        (2003, "42S02", False),
        (100051, "22012", False),
    ],
)
def test_session_gone_is_told_apart_from_a_failing_statement(errno, sqlstate, gone):
    errors = pytest.importorskip("snowflake.connector.errors")
    exc = errors.ProgrammingError(msg="x", errno=errno, sqlstate=sqlstate, send_telemetry=False)
    assert snowflake_session_gone(exc) is gone


def test_snowflake_messages_drop_the_doubled_errno_and_the_relogin_hint():
    """At INFO logging (the MCP server's) the driver formats errors without a
    SQLSTATE as '390111: 390111: ...', and 'New login required' misleads anyone
    on key-pair or PAT auth, who has nothing to log in to."""
    driver = "390111: 390111: Session no longer exists.  New login required to access the service."
    message = _clean(Exception(driver))
    assert message == (
        "390111: Session no longer exists. Snowflake ended this session on the server "
        "(it was aborted or expired); sqldash opens a new one on the next run."
    )
    assert _clean(Exception("002003 (42S02): SQL compilation error")) == (
        "002003 (42S02): SQL compilation error"
    )


def test_a_killed_pooled_session_does_not_log_the_others_in_again(monkeypatch):
    """Pre-ping reporting a dead session as a failed ping made SQLAlchemy raise
    InvalidatePoolError, which retires every pooled session older than now, so one
    expired session meant a fresh login (an SSO prompt on externalbrowser) per session."""
    warehouse = AsyncWarehouse(rows=[(1,)])
    warehouse.finished.set()
    connector, opened = killable_connector(monkeypatch, warehouse)
    try:
        first, second = connector.engine.connect(), connector.engine.connect()
        first.close()
        second.close()
        assert len(opened) == 2
        opened[0].killed = True
        again = [connector.engine.connect(), connector.engine.connect()]
        dbapi = [conn.connection.dbapi_connection for conn in again]
        for conn in again:
            conn.close()
    finally:
        connector.close()
    assert len(opened) == 3
    assert opened[1] in dbapi
    assert opened[2] in dbapi


def test_a_session_killed_under_a_statement_drops_only_that_connection(monkeypatch):
    warehouse = AsyncWarehouse(rows=[(1,)])
    warehouse.finished.set()
    connector, opened = killable_connector(monkeypatch, warehouse)
    try:
        first, second = connector.engine.connect(), connector.engine.connect()
        second.close()
        opened[0].killed = True
        with pytest.raises(DBAPIError) as caught:
            first.exec_driver_sql("SELECT 1")
        first.close()
        again = [connector.engine.connect(), connector.engine.connect()]
        dbapi = [conn.connection.dbapi_connection for conn in again]
        for conn in again:
            conn.close()
    finally:
        connector.close()
    assert caught.value.connection_invalidated
    assert len(opened) == 3
    assert opened[1] in dbapi
