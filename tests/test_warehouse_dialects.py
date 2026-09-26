import importlib.util
import json
import os
import re
import time

import pytest
from sqlalchemy import create_engine

from sqldash.connectors.base import ConnectorError
from sqldash.connectors.engine import build_engine, paramstyle_for
from sqldash.connectors.engine_urls import build_url
from sqldash.models.source import Source

CASES = [
    pytest.param(
        "sqlalchemy_bigquery",
        Source(type="bigquery", project="my-proj", database="analytics"),
        "bigquery",
        id="bigquery",
    ),
    pytest.param(
        "databricks",
        Source(type="databricks", host="dbx.cloud", http_path="/sql/1.0/wh/abc", token="tok"),
        "databricks",
        id="databricks",
    ),
    pytest.param(
        "redshift_connector",
        Source(type="redshift", host="rs.aws", database="dw", username="u", password="p"),
        "redshift+redshift_connector",
        id="redshift",
    ),
    pytest.param(
        "pyathena",
        Source(
            type="athena",
            host="us-east-1",
            database="curated",
            options={"s3_staging_dir": "s3://bkt/stage"},
        ),
        "awsathena+rest",
        id="athena",
    ),
    pytest.param(
        "pymysql",
        Source(type="mysql", host="db", database="analytics", username="u", password="p"),
        "mysql+pymysql",
        id="mysql",
    ),
    pytest.param(
        "trino",
        Source(type="trino", host="trino.internal", port=8080, database="hive", username="u"),
        "trino",
        id="trino",
    ),
]


def _fake_google_credentials(tmp_path, monkeypatch):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    info = {
        "type": "service_account",
        "project_id": "my-proj",
        "private_key_id": "fake",
        "private_key": pem,
        "client_email": "fake@my-proj.iam.gserviceaccount.com",
        "client_id": "1",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    path = tmp_path / "sa.json"
    path.write_text(json.dumps(info))
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(path))


@pytest.mark.parametrize(("package", "source", "drivername"), CASES)
def test_real_dialect_accepts_our_config(package, source, drivername, tmp_path, monkeypatch):
    if importlib.util.find_spec(package) is None:
        pytest.skip(f"{package} not installed")
    if source.type == "bigquery":
        _fake_google_credentials(tmp_path, monkeypatch)
    url = build_url(source, tmp_path)
    assert str(url).split("://")[0] == drivername
    engine = create_engine(url)
    try:
        assert engine.dialect.paramstyle in (
            "qmark",
            "numeric",
            "named",
            "format",
            "pyformat",
        )
    finally:
        engine.dispose()
    assert paramstyle_for(source) in ("qmark", "numeric", "named", "format", "pyformat")


@pytest.mark.skipif(not os.environ.get("SQLDASH_TEST_MYSQL"), reason="no MySQL service")
def test_mysql_live_end_to_end(tmp_path):
    from sqldash.connectors.base import CancelToken
    from sqldash.connectors.engine import EngineConnector
    from sqldash.params import bind_sql

    source = Source(
        type="mysql",
        host=os.environ.get("MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        database="sqldash_test",
        username="root",
        password=os.environ.get("MYSQL_PASSWORD", "root"),
    )
    conn = EngineConnector(source, tmp_path)
    conn.connect()
    try:
        token = CancelToken()
        conn.execute("DROP TABLE IF EXISTS orders", [], 10, token)
        conn.execute(
            "CREATE TABLE orders (region VARCHAR(8), amount DECIMAL(10,2), created DATE)",
            [],
            10,
            token,
        )
        conn.execute(
            "INSERT INTO orders VALUES ('us', 10.50, '2026-01-01'), "
            "('eu', 20.25, '2026-01-02'), ('us', 5.25, '2026-01-03')",
            [],
            10,
            token,
        )
        sql, bind = bind_sql(
            "SELECT region, SUM(amount) AS revenue FROM orders "
            "WHERE region = {{ r }} GROUP BY region",
            {"r": "us"},
            paramstyle_for(source),
        )
        result = conn.execute(sql, bind, 100, token)
        assert result.rows == [["us", "15.75"]]
        assert [c.type for c in result.columns] == ["string", "decimal"]

        deadline = time.monotonic() + 5
        tables = []
        while time.monotonic() < deadline and not tables:
            tables = [t for t in conn.introspect() if t.name == "orders"]
        assert tables
        assert ("amount", "decimal") in [(c[0], c[1].lower()) for c in tables[0].columns]
    finally:
        conn.close()


def test_tcp_warehouses_get_a_connect_timeout():
    """`engine.connect()` is a blocking socket with nothing for cancel() to
    attach to. An unreachable host wedged a pool worker until the OS TCP
    timeout, so `source test` printed FAIL and then never exited, and eight
    such runs starved every later /api/run."""
    from sqldash.connectors.engine import CONNECT_TIMEOUT_S, connect_args_for

    pg = connect_args_for(Source(type="postgres", host="h", database="d", username="u"))
    assert pg["connect_timeout"] == CONNECT_TIMEOUT_S
    authored = connect_args_for(
        Source(
            type="postgres",
            host="h",
            database="d",
            username="u",
            connect_args={"connect_timeout": 45},
        )
    )
    assert authored["connect_timeout"] == 45
    duck = connect_args_for(Source(type="duckdb", database=":memory:"))
    assert "connect_timeout" not in duck
    trino = connect_args_for(Source(type="trino", host="h", username="u"))
    assert "connect_timeout" not in trino
    redshift = connect_args_for(
        Source(type="redshift", host="h", database="d", username="u", password="p")
    )
    assert "connect_timeout" not in redshift
    assert "timeout" not in redshift
    override = connect_args_for(
        Source(
            type="postgres",
            driver="postgresql+pg8000",
            host="h",
            database="d",
            username="u",
        )
    )
    assert "connect_timeout" not in override
    url_pg8000 = connect_args_for(Source(type="postgres", url="postgresql+pg8000://u:p@h/d"))
    assert "connect_timeout" not in url_pg8000
    url_only = connect_args_for(Source(url="sqlite:////tmp/x.db"))
    assert url_only == {}
    bare = connect_args_for(
        Source(type="postgres", driver="postgresql", host="h", database="d", username="u")
    )
    assert bare["connect_timeout"] == CONNECT_TIMEOUT_S


def test_build_engine_forwards_connect_timeout(monkeypatch, tmp_path):
    """connect_args_for in isolation still passes if build_engine drops
    connect_args — that is the wiring that actually unblocks the pool."""
    from sqldash.connectors import engine as engine_mod
    from sqldash.connectors.base import ConnectorError
    from sqldash.connectors.engine import CONNECT_TIMEOUT_S, build_engine

    captured: dict = {}

    def fake_create_engine(url, **kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop")

    monkeypatch.setattr(engine_mod, "create_engine", fake_create_engine)
    with pytest.raises(ConnectorError, match="stop"):
        build_engine(Source(type="postgres", host="h", database="d", username="u"), tmp_path)
    assert captured["connect_args"]["connect_timeout"] == CONNECT_TIMEOUT_S


def test_cli_query_paths_do_not_cap_long_queries():
    """Removing timeout=None put the 300s registry default on the scripted
    path. connect_timeout is what unblocks an unreachable host; a nightly
    rollup must still be allowed to finish."""
    from pathlib import Path

    package = Path(__file__).resolve().parents[1] / "sqldash"
    src = "".join((package / name).read_text() for name in ("cli.py", "query_command.py"))
    calls = re.findall(r"run_(?:sync|bound)\((.*?)\)", src, flags=re.S)
    uncapped = [c for c in calls if "timeout=30" not in c]
    assert len(uncapped) == 3, calls
    assert all("timeout=None" in c for c in uncapped), calls


@pytest.mark.parametrize(
    ("extra", "source"),
    [
        ("trino", Source(type="trino", host="127.0.0.1", port=1, username="u")),
        (
            "redshift_connector",
            Source(
                type="redshift", host="127.0.0.1", port=1, database="d", username="u", password="p"
            ),
        ),
    ],
)
def test_injected_connect_args_are_ones_the_driver_accepts(extra, source, tmp_path):
    """connect_timeout is not a trino or redshift_connector kwarg — injecting
    it TypeError'd every such source before any I/O."""
    pytest.importorskip(extra)
    from sqldash.connectors.engine import build_engine

    engine = build_engine(source, tmp_path)
    try:
        engine.connect()
    except TypeError as exc:
        pytest.fail(f"{source.type} rejected our connect_args: {exc}")
    except Exception:
        pass


def test_build_engine_wraps_a_duckdb_url_naming_an_unloadable_dialect(tmp_path):
    source = Source(type="duckdb", url="nosuchdialect://h/d")
    with pytest.raises(ConnectorError, match="cannot create engine for source"):
        build_engine(source, tmp_path)
