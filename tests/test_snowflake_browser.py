from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects.sqlite import dialect

from sqldash.connectors.base import ConnectorError
from sqldash.connectors.engine import EngineConnector
from sqldash.models.source import Source
from sqldash.server import create_app


def connector_with_rows(rows):
    connector = EngineConnector(Source(type="snowflake", account="example"), None)
    connection = MagicMock()
    connection.exec_driver_sql.return_value.fetchall.return_value = rows
    engine = MagicMock()
    engine.dialect = SimpleNamespace(
        name="snowflake",
        identifier_preparer=dialect().identifier_preparer,
        requires_name_normalize=False,
    )
    engine.connect.return_value.__enter__.return_value = connection
    connector._engine = engine
    return connector, connection


def test_database_browse_quotes_identifiers_without_mutating_session():
    connector, connection = connector_with_rows([("PUBLIC", "orders", "total", "NUMBER")])
    tables = connector.introspect_database('sales"; USE DATABASE other; --')
    statement = connection.exec_driver_sql.call_args.args[0]
    assert 'FROM "sales""; USE DATABASE other; --".information_schema.columns' in statement
    assert connection.exec_driver_sql.call_count == 1
    assert tables[0].name == "orders"
    assert tables[0].columns == [("total", "NUMBER")]


def test_database_browse_empty_and_permission_failure_never_fall_back():
    connector, connection = connector_with_rows([])
    assert connector.introspect_database("EMPTY") == []
    connection.exec_driver_sql.side_effect = RuntimeError("Database is not authorized")
    with pytest.raises(ConnectorError, match="not authorized"):
        connector.introspect_database("DENIED")
    assert connection.exec_driver_sql.call_count == 2


def test_database_context_uses_live_session_and_accessible_catalog():
    connector, connection = connector_with_rows([])
    connection.exec_driver_sql.return_value.scalar.return_value = "SALES"
    connection.exec_driver_sql.return_value.mappings.return_value.all.return_value = [
        {"name": "SALES", "comment": "Revenue marts"},
        {"name": "ANALYTICS", "comment": ""},
    ]
    assert connector.database_context() == {
        "current": "SALES",
        "databases": ["ANALYTICS", "SALES"],
        "comments": {"SALES": "Revenue marts"},
        "kinds": {},
    }


def test_schema_database_api_quotes_all_parts(tmp_path, monkeypatch):
    (tmp_path / "d.yaml").write_text(
        "title: D\nsource: {type: duckdb, database: ':memory:'}\ntiles: []\n"
    )
    connector, _ = connector_with_rows([('odd"schema', "orders", 'odd"column', "NUMBER")])
    app = create_app(tmp_path, allowed_hosts=["testserver"])

    @contextmanager
    def connection(*args):
        yield connector

    monkeypatch.setattr(app.state.registry, "connection", connection)
    with TestClient(app) as client:
        response = client.get("/api/dashboards/d/schema", params={"database": 'odd"db'})
        assert response.status_code == 200
        table = response.json()["tables"][0]
        assert table["sql"] == '"odd""db"."odd""schema".orders'
        assert table["columns"][0]["sql"] == '"odd""column"'


def test_snowflake_identifiers_quote_exactly_the_case_sensitive_names():
    snowflake = pytest.importorskip("snowflake.sqlalchemy")
    connector = EngineConnector(Source(type="snowflake", account="example"), None)
    connector._engine = SimpleNamespace(dialect=snowflake.snowdialect.SnowflakeDialect())
    expected = {
        "ORDERS": "ORDERS",
        "ORDER_DATE": "ORDER_DATE",
        "order_date_lc": '"order_date_lc"',
        "my col": '"my col"',
        "Mixed": '"Mixed"',
        "ORDER": '"ORDER"',
        'odd"name': '"odd""name"',
    }
    assert {name: connector.sql_identifier(name) for name in expected} == expected
