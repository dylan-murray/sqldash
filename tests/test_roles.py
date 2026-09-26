import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import mysql, postgresql

from sqldash.connectors.base import ConnectorError
from sqldash.connectors.roles import (
    POSTGRES_SESSION_RESET,
    SNOWFLAKE_SECONDARY_OFF,
    clickhouse_http,
    install_role,
    mysql_role,
    provider,
    role_connect_args,
    role_context,
)
from sqldash.models.source import Source
from sqldash.project.source_context import (
    context_source,
    role_source,
    split_role_source,
    split_source_context,
)
from sqldash.project.sources import alias_for_picker_key, resolve_picker_source
from sqldash.server import create_app


@pytest.mark.parametrize("key", ["@role:[]", '@role:["a",null]', '@role:["@role:x","r"]'])
def test_invalid_role_references_are_rejected(key):
    with pytest.raises(ValueError, match="Invalid role-qualified"):
        split_role_source(key)


def test_role_reference_round_trip():
    key = role_source("workspace/sales.sources.warehouse", 'analyst"; SELECT 1 --')
    assert split_role_source(key) == ("workspace/sales.sources.warehouse", 'analyst"; SELECT 1 --')


def test_url_provider_wins_over_type():
    assert provider(Source(type="postgres", url="mysql+pymysql://host/db")) == "mysql"


@pytest.mark.parametrize("probe", [provider, clickhouse_http])
def test_unparseable_url_is_a_connector_error(probe):
    source = Source(url="bad url//admin:HUNTER2_URL_SECRET@host/db")
    with pytest.raises(ConnectorError, match="cannot resolve dialect") as info:
        probe(source)
    assert "HUNTER2_URL_SECRET" not in str(info.value)


@pytest.mark.parametrize("kind", ["snowflake", "postgresql", "mysql", "mariadb", "trino"])
def test_pool_checkout_reapplies_quoted_role(kind, monkeypatch):
    hooks = []
    monkeypatch.setattr(
        "sqldash.connectors.roles.event.listens_for",
        lambda engine, event: lambda fn: hooks.append(fn),
    )
    dialect = mysql.dialect() if kind in {"mysql", "mariadb"} else postgresql.dialect()
    engine = SimpleNamespace(dialect=dialect)
    role = 'role"`; SELECT 1 --'
    configured = '"' + role.replace('"', '""') + '"' if kind == "snowflake" else role
    source = Source(type=kind, role=configured)
    install_role(engine, source)
    connection = MagicMock()
    record = SimpleNamespace(info={})
    connection.cursor.return_value.fetchone.return_value = (role,)
    for _ in range(2):
        hooks[0](connection, record, None)
    statements = [call.args[0] for call in connection.cursor.return_value.execute.call_args_list]
    quote = dialect.identifier_preparer.quote_identifier
    expected = f"USE ROLE {quote(role)}" if kind == "snowflake" else f"SET ROLE {quote(role)}"
    if kind == "mysql":
        expected += "@`%`"
    reset = list(POSTGRES_SESSION_RESET) if kind == "postgresql" else []
    after = [SNOWFLAKE_SECONDARY_OFF] if kind == "snowflake" else []
    assert statements == [*reset, expected, *after, *reset, expected, *after]
    assert connection.cursor.return_value.close.call_count == 2
    assert connection.commit.call_count == (2 if kind == "postgresql" else 0)


@pytest.mark.parametrize(
    ("kind", "role", "statement"),
    [
        ("mysql", "analyst", "SET ROLE `analyst`@`%`"),
        ("mariadb", "team%ops", "SET ROLE `team%ops`"),
        ("postgresql", "team%ops", 'SET ROLE "team%ops"'),
        ("trino", "team%%ops", 'SET ROLE "team%%ops"'),
    ],
)
def test_role_statements_keep_a_literal_percent(kind, role, statement, monkeypatch):
    """quote_identifier doubles % for pyformat parameters, but the checkout hook
    runs on the raw DBAPI cursor with no parameters, so nothing un-doubles it.
    MySQL gives every bare role the host `%`, which reached the server as `%%`
    and failed as "not granted" on every pooled checkout."""
    hooks = []
    monkeypatch.setattr(
        "sqldash.connectors.roles.event.listens_for",
        lambda engine, event: lambda fn: hooks.append(fn),
    )
    dialect = mysql.dialect() if kind in {"mysql", "mariadb"} else postgresql.dialect()
    install_role(SimpleNamespace(dialect=dialect), Source(type=kind, role=role))
    connection = MagicMock()
    hooks[0](connection, SimpleNamespace(info={}), None)
    sent = [call.args[0] for call in connection.cursor.return_value.execute.call_args_list]
    reset = list(POSTGRES_SESSION_RESET) if kind == "postgresql" else []
    assert sent == [*reset, statement]


def test_snowflake_default_is_captured_once_per_physical_connection(monkeypatch):
    hooks = []
    monkeypatch.setattr(
        "sqldash.connectors.roles.event.listens_for",
        lambda engine, event: lambda fn: hooks.append(fn),
    )
    install_role(SimpleNamespace(dialect=postgresql.dialect()), Source(type="snowflake"))
    connection = MagicMock()
    connection.cursor.return_value.fetchone.return_value = ("ANALYST",)
    record = SimpleNamespace(info={})
    for _ in range(2):
        hooks[0](connection, record, None)
    assert [call.args[0] for call in connection.cursor.return_value.execute.call_args_list] == [
        "SELECT CURRENT_ROLE()",
        'USE ROLE "ANALYST"',
        'USE ROLE "ANALYST"',
    ]


def test_clickhouse_role_is_sent_on_every_http_request():
    source = Source(type="clickhouse", role="analyst")
    original = {"ch_settings": {"max_threads": 2}}
    args = role_connect_args(source, original)
    assert args == {"ch_settings": {"max_threads": 2, "role": "analyst"}}
    assert "role" not in original["ch_settings"]


def test_mysql_role_preserves_user_and_host():
    assert mysql_role(json.dumps(["odd@role", "host@domain"])) == ["odd@role", "host@domain"]
    assert mysql_role("analyst") == ("analyst", "%")


def test_snowflake_context_is_live_not_configured():
    engine = MagicMock()
    conn = engine.connect.return_value.__enter__.return_value
    conn.exec_driver_sql.return_value.fetchall.return_value = [
        ("ACTUAL", '["ACTUAL","READER"]', '{"roles":[],"value":"ALL"}', "WH")
    ]
    context = role_context(engine, Source(type="snowflake", role="CONFIGURED"))
    assert context["current"] == ["ACTUAL"]
    assert [r["value"] for r in context["roles"]] == ["ACTUAL", "READER"]
    assert context["label"] == "Primary role"


@pytest.mark.parametrize("kind", ["duckdb", "sqlite", "bigquery", "athena", "databricks"])
def test_non_session_roles_are_explicit_without_connecting(kind):
    engine = MagicMock()
    context = role_context(engine, Source(type=kind))
    assert not context["switchable"]
    assert context["note"]
    engine.connect.assert_not_called()


def test_roles_survive_library_restart_and_portable_tile_copy(tmp_path, monkeypatch):
    for name in ["a", "b"]:
        (tmp_path / f"{name}.yaml").write_text(
            f"title: {name}\nsource: {{type: snowflake, account: example, role: BASE}}\ntiles: []\n"
        )
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    seen = []

    @contextmanager
    def connection(source, base):
        seen.append(source)

        def context():
            return {
                "switchable": True,
                "current": [source.role],
                "roles": [{"value": name, "label": name} for name in ["BASE", "ANALYST"]],
                "note": "",
                "label": "Primary role",
            }

        yield SimpleNamespace(role_context=context)

    monkeypatch.setattr(app.state.registry, "connection", connection)
    with TestClient(app) as client:
        client.headers["X-Sqldash-Token"] = app.state.api_token
        selected = client.post("/api/dashboards/a/roles", json={"role": "ANALYST"})
        assert selected.status_code == 200, selected.text
        data = selected.json()
        assert data["current"] == ["ANALYST"]
        assert data["base_source"] == ""
        key = data["source"]
        saved = client.post(
            "/api/dashboards/a/library",
            json={
                "title": "Revenue",
                "sql": "SELECT 1 AS revenue",
                "source": key,
            },
        ).json()
        opened = client.get(f"/api/dashboards/b/library/{saved['id']}/open")
        assert opened.status_code == 200, opened.text
        assert opened.json()["state"]["source"] == key
        before = client.get("/api/dashboards/b").json()
        added = client.post(
            "/api/dashboards/b/tiles",
            headers={"If-Match": before["etag"]},
            json={
                "tile": {
                    "id": "revenue",
                    "query": "revenue",
                    "type": "chart",
                    "source": key,
                    "chart": {"type": "table"},
                },
                "sql": "SELECT 1 AS revenue",
            },
        )
        assert added.status_code == 200, added.text
        dash = client.get("/api/dashboards/b").json()["dashboard"]
        alias = dash["tiles"][0]["source"]
        assert dash["sources"][alias]["role"] == "ANALYST"
        assert "@role:" not in (tmp_path / "b.yaml").read_text()
        assert "role: BASE" in (tmp_path / "a.yaml").read_text()
        reset = client.post("/api/dashboards/a/roles", json={"source": key, "role": None})
        assert reset.json()["source"] == ""
        assert reset.json()["current"] == ["BASE"]
        denied = client.post("/api/dashboards/a/roles", json={"role": "UNGRANTED"})
        assert denied.status_code == 422
        assert all(source.role != "UNGRANTED" for source in seen)
        malformed = client.get("/api/dashboards/a/roles", params={"source": "@role:[]"})
        assert malformed.status_code == 422


@pytest.mark.parametrize(("version", "privilege"), [("150006", "MEMBER"), ("160004", "SET")])
def test_postgres_discovery_uses_version_appropriate_set_permission(version, privilege):
    engine = MagicMock()
    conn = engine.connect.return_value.__enter__.return_value
    statements = []

    def execute(sql):
        statements.append(sql)
        result = MagicMock()
        result.scalar.return_value = version if sql == "SHOW server_version_num" else "analyst"
        result.fetchall.return_value = [("analyst",), ("reader",)]
        return result

    conn.exec_driver_sql.side_effect = execute
    context = role_context(engine, Source(type="postgres"))
    assert context["current"] == ["analyst"]
    assert any(f"pg_has_role(session_user, oid, '{privilege}')" in sql for sql in statements)


@pytest.mark.parametrize(
    ("kind", "scalars", "data", "current"),
    [
        ("mysql", ["8.4.0"], [[("analyst", "%")], [("reader", "localhost")]], ["analyst@%"]),
        ("mariadb", ["analyst"], [[("reader",)]], ["analyst"]),
        ("trino", [], [[("analyst",)], [("reader",)]], ["analyst"]),
        ("clickhouse", [["analyst"], "24.4.1"], [[("reader",)]], ["analyst"]),
        ("mssql", [], [[("reader",), ("public",)]], ["reader", "public"]),
        ("redshift", [], [[("reader",)]], ["reader"]),
    ],
)
def test_provider_role_discovery(kind, scalars, data, current):
    engine = MagicMock()
    result = engine.connect.return_value.__enter__.return_value.exec_driver_sql.return_value
    result.scalar.side_effect = scalars
    result.fetchall.side_effect = data
    context = role_context(engine, Source(type=kind))
    assert context["current"] == current
    assert context["switchable"] == (kind not in {"mssql", "redshift"})
    if context["switchable"]:
        assert context["roles"]


def test_clickhouse_native_reports_read_only_roles():
    engine = MagicMock()
    conn = engine.connect.return_value.__enter__.return_value
    conn.exec_driver_sql.return_value.scalar.return_value = ["analyst"]
    context = role_context(engine, Source(type="clickhouse", driver="clickhouse+native"))
    assert not context["switchable"]
    assert context["current"] == ["analyst"]


def test_source_context_preserves_case_sensitive_snowflake_names(tmp_path):
    (tmp_path / "a.yaml").write_text(
        "title: A\nsource: {type: snowflake, account: example, "
        "connect_args: {role: OVERRIDE}}\ntiles: []\n"
    )
    app = create_app(tmp_path)
    source, _ = resolve_picker_source(
        app.state.store, app.state.layer, "a", role_source("a.source", 'MixedCase "role"')
    )
    assert source.role == '"MixedCase ""role"""'
    assert "role" not in source.connect_args


def test_role_context_rejects_dashboard_relative_sources(tmp_path):
    (tmp_path / "a.yaml").write_text(
        "title: A\nsource: {type: postgres}\nsources: {local: {type: postgres}}\ntiles: []\n"
    )
    app = create_app(tmp_path)
    with pytest.raises(ValueError, match="canonical"):
        resolve_picker_source(app.state.store, app.state.layer, "a", role_source("local", "reader"))


def test_role_references_cannot_expand_environment_values():
    with pytest.raises(ValueError, match="Invalid role-qualified"):
        role_source("a.source", "${env:PRIVATE_TOKEN}")


def test_snowflake_role_overrides_url_and_normalizes_unquoted_names(monkeypatch):
    hooks = []
    monkeypatch.setattr(
        "sqldash.connectors.roles.event.listens_for",
        lambda engine, event: lambda fn: hooks.append(fn),
    )
    install_role(
        SimpleNamespace(dialect=postgresql.dialect()),
        Source(url="snowflake://host/db", role="analyst"),
    )
    connection = MagicMock()
    hooks[0](connection, SimpleNamespace(info={}), None)
    sent = [call.args[0] for call in connection.cursor.return_value.execute.call_args_list]
    assert sent == ['USE ROLE "ANALYST"', SNOWFLAKE_SECONDARY_OFF]


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ({"role": "READER"}, ['USE ROLE "READER"', SNOWFLAKE_SECONDARY_OFF]),
        ({"connect_args": {"role": "READER"}}, ['USE ROLE "READER"', SNOWFLAKE_SECONDARY_OFF]),
        ({"role": "READER", "secondary_roles": True}, ['USE ROLE "READER"']),
        ({}, ['USE ROLE "READER"']),
        ({"secondary_roles": False}, ['USE ROLE "READER"', SNOWFLAKE_SECONDARY_OFF]),
    ],
)
def test_snowflake_checkout_turns_secondary_roles_off_when_a_role_is_set(
    fields, expected, monkeypatch
):
    """A user's DEFAULT_SECONDARY_ROLES (ALL by default) kept every granted role's
    privileges active under the configured role, so an owner-only table was readable
    through a source that named a read-only role."""
    hooks = []
    monkeypatch.setattr(
        "sqldash.connectors.roles.event.listens_for",
        lambda engine, event: lambda fn: hooks.append(fn),
    )
    install_role(SimpleNamespace(dialect=postgresql.dialect()), Source(type="snowflake", **fields))
    connection = MagicMock()
    connection.cursor.return_value.fetchone.return_value = ("READER",)
    hooks[0](connection, SimpleNamespace(info={"sqldash_role": "READER"}), None)
    sent = [call.args[0] for call in connection.cursor.return_value.execute.call_args_list]
    assert sent == expected


@pytest.mark.parametrize(
    ("fields", "secondary", "note"),
    [
        (
            {},
            '{"roles":"OWNER","value":"ALL"}',
            "Secondary roles: ALL, so grants of OWNER also apply. Picking a role turns them off.",
        ),
        (
            {"role": "READER", "secondary_roles": True},
            '{"roles":"OWNER","value":"ALL"}',
            "Secondary roles: ALL, so grants of OWNER also apply.",
        ),
        ({"role": "READER"}, '{"roles":"","value":""}', "Secondary roles: none"),
    ],
)
def test_snowflake_role_note_says_which_secondary_grants_apply(fields, secondary, note):
    engine = MagicMock()
    conn = engine.connect.return_value.__enter__.return_value
    conn.exec_driver_sql.return_value.fetchall.return_value = [
        ("READER", '["READER","OWNER"]', secondary, "WH")
    ]
    context = role_context(engine, Source(type="snowflake", **fields))
    assert context["note"] == note


def test_database_context_keys_round_trip_and_keep_the_role_form():
    assert context_source("a.source") == "a.source"
    assert context_source("a.source", "ANALYST") == role_source("a.source", "ANALYST")
    key = context_source("a.source", "ANALYST", 'Mixed "db"')
    assert split_source_context(key) == ("a.source", "ANALYST", 'Mixed "db"', None)
    assert split_role_source(key) == ("a.source", "ANALYST")
    only = context_source("a.source", database="SALES")
    assert split_source_context(only) == ("a.source", None, "SALES", None)


@pytest.mark.parametrize(
    "key",
    [
        "@context:{}",
        '@context:{"source":"a.source"}',
        '@context:{"source":"a.source","database":""}',
        '@context:{"source":"a.source","database":"${env:TOKEN}"}',
        '@context:{"source":"a.source","database":"X","schema":"Y"}',
        '@context:{"source":"@context:x","database":"X"}',
        '@context:["a.source","X"]',
    ],
)
def test_invalid_database_context_keys_are_rejected(key):
    with pytest.raises(ValueError, match="Invalid role-qualified"):
        split_source_context(key)


def test_database_context_resolves_on_snowflake_and_drops_the_old_schema(tmp_path):
    (tmp_path / "a.yaml").write_text(
        "title: A\nsource: {type: snowflake, account: example, database: DEV, schema: PUBLIC, "
        "connect_args: {database: OTHER}}\ntiles: []\n"
    )
    app = create_app(tmp_path)
    plain, _ = resolve_picker_source(
        app.state.store, app.state.layer, "a", context_source("a.source", database="SALES")
    )
    assert (plain.database, plain.db_schema) == ("SALES", None)
    assert "database" not in plain.connect_args
    mixed, _ = resolve_picker_source(
        app.state.store, app.state.layer, "a", context_source("a.source", "R", "Mixed db")
    )
    assert (mixed.database, mixed.role) == ('"Mixed db"', "R")


def test_database_context_is_refused_off_snowflake(tmp_path):
    (tmp_path / "a.yaml").write_text("title: A\nsource: {type: postgres}\ntiles: []\n")
    app = create_app(tmp_path)
    with pytest.raises(ValueError, match="only available for Snowflake"):
        resolve_picker_source(
            app.state.store, app.state.layer, "a", context_source("a.source", database="X")
        )


def test_copied_source_alias_names_the_database_and_role():
    key = context_source("a.source", "ANALYST", "SALES")
    assert alias_for_picker_key(key, {}) == "a_SALES_ANALYST"


def test_database_choice_survives_role_switch_library_and_tile_copy(tmp_path, monkeypatch):
    for name in ["a", "b"]:
        (tmp_path / f"{name}.yaml").write_text(
            f"title: {name}\nsource: {{type: snowflake, account: example, role: BASE, "
            "database: DEV}\ntiles: []\n"
        )
    app = create_app(tmp_path, allowed_hosts=["testserver"])

    @contextmanager
    def connection(source, base):
        yield SimpleNamespace(
            role_context=lambda: {
                "switchable": True,
                "current": [source.role],
                "roles": [{"value": n, "label": n} for n in ["BASE", "ANALYST"]],
                "note": "",
                "label": "Primary role",
            },
            database_context=lambda: {
                "current": source.database,
                "databases": ["DEV", "SALES"],
            },
        )

    monkeypatch.setattr(app.state.registry, "connection", connection)
    with TestClient(app) as client:
        client.headers["X-Sqldash-Token"] = app.state.api_token
        picked = client.post("/api/dashboards/a/databases", json={"database": "SALES"})
        assert picked.status_code == 200, picked.text
        key = picked.json()["source"]
        assert picked.json()["database"] == "SALES"
        assert split_source_context(key) == ("a.source", None, "SALES", None)
        assert (
            client.get("/api/dashboards/a/databases", params={"source": key}).json()["current"]
            == "SALES"
        )
        role = client.post("/api/dashboards/a/roles", json={"source": key, "role": "ANALYST"})
        assert role.status_code == 200, role.text
        both = role.json()["source"]
        assert split_source_context(both) == ("a.source", "ANALYST", "SALES", None)
        assert role.json()["database"] == "SALES"
        kept = client.post(
            "/api/dashboards/a/databases", json={"source": both, "database": "SALES"}
        )
        assert kept.json()["source"] == both
        saved = client.post(
            "/api/dashboards/a/library",
            json={"title": "Sales", "sql": "SELECT 1 AS n", "source": both},
        ).json()
        opened = client.get(f"/api/dashboards/b/library/{saved['id']}/open")
        assert opened.json()["state"]["source"] == both
        before = client.get("/api/dashboards/b").json()
        added = client.post(
            "/api/dashboards/b/tiles",
            headers={"If-Match": before["etag"]},
            json={
                "tile": {
                    "id": "n",
                    "query": "n",
                    "type": "chart",
                    "source": both,
                    "chart": {"type": "table"},
                },
                "sql": "SELECT 1 AS n",
            },
        )
        assert added.status_code == 200, added.text
        dash = client.get("/api/dashboards/b").json()["dashboard"]
        copied = dash["sources"][dash["tiles"][0]["source"]]
        assert (copied["database"], copied["role"]) == ("SALES", "ANALYST")
        assert "@context:" not in (tmp_path / "b.yaml").read_text()
        back = client.post("/api/dashboards/a/databases", json={"source": both, "database": "DEV"})
        assert split_source_context(back.json()["source"]) == ("a.source", "ANALYST", None, None)
        cleared = client.post(
            "/api/dashboards/a/databases", json={"source": key, "database": "DEV"}
        )
        assert cleared.json()["source"] == ""
        unknown = client.post("/api/dashboards/a/databases", json={"database": "SECRET"})
        assert unknown.status_code == 422
        other = client.post(
            "/api/dashboards/a/databases", json={"source": "a.source", "database": "${env:X}"}
        )
        assert other.status_code == 422


@pytest.mark.parametrize("show_roles_fails", [False, True])
def test_snowflake_roles_carry_their_comments_when_visible(show_roles_fails):
    engine = MagicMock()
    conn = engine.connect.return_value.__enter__.return_value

    def execute(sql):
        result = MagicMock()
        if sql.startswith("SELECT CURRENT_ROLE()"):
            result.fetchall.return_value = [
                ("ANALYST", '["ANALYST","LOADER"]', '{"value":"ALL"}', "WH")
            ]
        elif sql == "SHOW ROLES":
            if show_roles_fails:
                raise RuntimeError("insufficient privileges")
            result.mappings.return_value.all.return_value = [
                {"name": "ANALYST", "comment": "Read marts"},
                {"name": "LOADER", "comment": None},
            ]
        return result

    conn.exec_driver_sql.side_effect = execute
    context = role_context(engine, Source(type="snowflake", account="example"))
    assert context["secondary"] == "ALL"
    details = {role["value"]: role["detail"] for role in context["roles"]}
    assert details == {"ANALYST": "" if show_roles_fails else "Read marts", "LOADER": ""}


def _snowflake_engine(warehouse, shown):
    engine = MagicMock()
    conn = engine.connect.return_value.__enter__.return_value

    def execute(sql):
        result = MagicMock()
        if sql.startswith("SELECT CURRENT_ROLE()"):
            result.fetchall.return_value = [("LEARNER", '["LEARNER"]', '{"value":""}', warehouse)]
        elif sql == "SHOW WAREHOUSES":
            result.mappings.return_value.all.return_value = [
                {"name": name, "size": "X-Small"} for name in shown
            ]
        else:
            result.mappings.return_value.all.return_value = []
        return result

    conn.exec_driver_sql.side_effect = execute
    return engine


@pytest.mark.parametrize(
    ("fields", "warehouse", "shown", "warning"),
    [
        ({"warehouse": "analytics_wh"}, "ANALYTICS_WH", ["ANALYTICS_WH"], ""),
        (
            {"warehouse": "analytics_wh"},
            None,
            ["LEARNING_WH", "OTHER_WH"],
            "LEARNER cannot use warehouse ANALYTICS_WH. Warehouses it can see: "
            "LEARNING_WH, OTHER_WH. Pick one to run queries.",
        ),
        (
            {"connect_args": {"warehouse": "ANALYTICS_WH"}},
            None,
            [],
            "LEARNER cannot use warehouse ANALYTICS_WH, and it cannot see any warehouse. "
            "Pick another role.",
        ),
        (
            {},
            None,
            ["LEARNING_WH"],
            "No warehouse is active for LEARNER. Warehouses it can see: LEARNING_WH. "
            "Pick one to run queries.",
        ),
    ],
)
def test_snowflake_role_context_says_when_the_role_cannot_use_the_warehouse(
    fields, warehouse, shown, warning
):
    """Snowflake keeps the session's warehouse across USE ROLE but hides it from a
    primary role without a privilege on it, so picking such a role left the schema
    panel failing with 000606 and nothing said which grant was missing."""
    engine = _snowflake_engine(warehouse, shown)
    context = role_context(engine, Source(type="snowflake", account="example", **fields))
    assert context["warehouse"] == warehouse
    assert context["configured_warehouse"] == ("ANALYTICS_WH" if fields else None)
    assert [w["value"] for w in context["warehouses"]] == shown
    assert context["warning"] == warning


def test_warehouse_context_keys_round_trip_and_resolve_on_snowflake_only(tmp_path):
    key = context_source("a.source", "LEARNER", warehouse="LEARNING_WH")
    assert split_source_context(key) == ("a.source", "LEARNER", None, "LEARNING_WH")
    assert split_role_source(key) == ("a.source", "LEARNER")
    assert alias_for_picker_key(key, {}) == "a_LEARNING_WH_LEARNER"
    (tmp_path / "a.yaml").write_text(
        "title: A\nsource: {type: snowflake, account: example, warehouse: BASE_WH, "
        "connect_args: {warehouse: OTHER_WH}}\ntiles: []\n"
    )
    (tmp_path / "p.yaml").write_text("title: P\nsource: {type: postgres}\ntiles: []\n")
    app = create_app(tmp_path)
    picked, _ = resolve_picker_source(app.state.store, app.state.layer, "a", key)
    assert (picked.role, picked.warehouse) == ("LEARNER", "LEARNING_WH")
    assert "warehouse" not in picked.connect_args
    with pytest.raises(ValueError, match="warehouse is only available for Snowflake"):
        resolve_picker_source(
            app.state.store,
            app.state.layer,
            "p",
            context_source("p.source", warehouse="X"),
        )


def test_role_switch_keeps_falls_back_or_names_the_missing_warehouse(tmp_path, monkeypatch):
    """Picking a primary role without USAGE on the session's warehouse left every query
    failing with 000606. The switch keeps the warehouse when the new role can use it,
    otherwise falls back to the configured one, then to the first it can use, and says
    so; a role that can use none gets a plain warning instead."""
    (tmp_path / "a.yaml").write_text(
        "title: a\nsource: {type: snowflake, account: example, role: BASE, "
        "warehouse: BASE_WH, database: DEV}\ntiles: []\n"
    )
    (tmp_path / "p.yaml").write_text("title: p\nsource: {type: postgres}\ntiles: []\n")
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    visible = {
        "BASE": ["BASE_WH"],
        "LEARNER": ["A_WH", "LEARNING_WH"],
        "BOTH": ["BASE_WH", "LEARNING_WH"],
        "NOTHING": [],
        "MONITOR": ["WATCHED_WH"],
    }
    usable = {**visible, "LEARNER": ["LEARNING_WH"], "MONITOR": []}
    logins = []

    @contextmanager
    def connection(source, base):
        if source.type != "snowflake":
            yield SimpleNamespace(
                role_context=lambda: {
                    "switchable": True,
                    "current": ["reader"],
                    "roles": [],
                    "note": "",
                    "label": "Role",
                }
            )
            return
        logins.append((source.role, source.warehouse))
        current = source.warehouse if source.warehouse in usable[source.role] else None
        yield SimpleNamespace(
            role_context=lambda: {
                "switchable": True,
                "current": [source.role],
                "roles": [{"value": n, "label": n} for n in visible],
                "note": "",
                "label": "Primary role",
                "warehouse": current,
                "configured_warehouse": source.warehouse,
                "warehouses": [{"value": w, "label": w} for w in visible[source.role]],
                "warning": "" if current else "cannot see any warehouse",
            },
            database_context=lambda: {"current": source.database, "databases": ["DEV", "LRN"]},
        )

    monkeypatch.setattr(app.state.registry, "connection", connection)

    def switch(source, role):
        response = client.post("/api/dashboards/a/roles", json={"source": source, "role": role})
        assert response.status_code == 200, response.text
        return response.json()

    with TestClient(app) as client:
        client.headers["X-Sqldash-Token"] = app.state.api_token
        learner = switch("", "LEARNER")
        assert split_source_context(learner["source"]) == (
            "a.source",
            "LEARNER",
            None,
            "LEARNING_WH",
        )
        assert (learner["warehouse"], learner["warning"]) == ("LEARNING_WH", "")
        assert learner["configured_warehouse"] == "BASE_WH"
        assert learner["warehouse_note"] == (
            "LEARNER cannot use warehouse BASE_WH, so queries run on LEARNING_WH."
        )
        assert ("LEARNER", "A_WH") in logins
        both = switch(learner["source"], "BOTH")
        assert split_source_context(both["source"])[3] == "LEARNING_WH"
        assert "warehouse_note" not in both
        back = switch(learner["source"], "BASE")
        assert back["source"] == '@role:["a.source","BASE"]'
        assert back["warehouse"] == "BASE_WH"
        assert back["warehouse_note"] == (
            "BASE cannot use warehouse LEARNING_WH, so queries run on BASE_WH."
        )
        nothing = switch("", "NOTHING")
        assert (nothing["warehouse"], nothing["warning"]) == (None, "cannot see any warehouse")
        watched = switch("", "MONITOR")
        assert watched["warehouse"] is None
        assert watched["warning"] == (
            "MONITOR cannot use warehouse BASE_WH or any warehouse it can see. Pick another role."
        )

        denied = client.post(
            "/api/dashboards/a/warehouses",
            json={"source": nothing["source"], "warehouse": "BASE_WH"},
        )
        assert denied.status_code == 422
        picked = client.post(
            "/api/dashboards/a/warehouses",
            json={"source": both["source"], "warehouse": "BASE_WH"},
        ).json()
        assert split_source_context(picked["source"]) == ("a.source", "BOTH", None, None)
        again = client.post(
            "/api/dashboards/a/warehouses",
            json={"source": picked["source"], "warehouse": "LEARNING_WH"},
        ).json()
        assert split_source_context(again["source"]) == ("a.source", "BOTH", None, "LEARNING_WH")
        assert (again["selected_warehouse"], again["configured_warehouse"]) == (
            "LEARNING_WH",
            "BASE_WH",
        )
        db = client.post(
            "/api/dashboards/a/databases", json={"source": again["source"], "database": "LRN"}
        ).json()
        assert split_source_context(db["source"]) == ("a.source", "BOTH", "LRN", "LEARNING_WH")
        other = client.post("/api/dashboards/p/warehouses", json={"warehouse": "X"})
        assert other.status_code == 422


def test_role_switch_keeps_falls_back_or_names_the_missing_database(tmp_path, monkeypatch):
    """Picking a role that cannot see the source's database left the Database row with
    an empty label and the schema panel failing with a raw 002043. The switch keeps the
    database when the new role can use it, otherwise lands on the configured one, then
    on the first one it can see (the account's own before shared ones), and says so;
    a role that can use none gets a plain warning instead."""
    (tmp_path / "a.yaml").write_text(
        "title: a\nsource: {type: snowflake, account: example, role: BASE, "
        "warehouse: WH, database: DEV}\ntiles: []\n"
    )
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    visible = {
        "BASE": ["DEV", "LRN"],
        "LEARNER": ["ACCOUNT_APP", "LRN", "SHARED"],
        "BOTH": ["DEV", "LRN"],
        "HIDDEN": [],
        "LOOKER": ["NOPE"],
    }
    usable = {**visible, "LOOKER": []}
    kinds = {"ACCOUNT_APP": "APPLICATION", "SHARED": "IMPORTED DATABASE"}
    tried = []

    @contextmanager
    def connection(source, base):
        def database_context():
            tried.append((source.role, source.database))
            shown = visible[source.role]
            return {
                "current": source.database if source.database in usable[source.role] else None,
                "databases": shown,
                "comments": {},
                "kinds": {d: kinds.get(d, "STANDARD") for d in shown},
            }

        yield SimpleNamespace(
            role_context=lambda: {
                "switchable": True,
                "current": [source.role],
                "roles": [{"value": n, "label": n} for n in visible],
                "note": "",
                "label": "Primary role",
                "warehouse": "WH",
                "configured_warehouse": "WH",
                "warehouses": [{"value": "WH", "label": "WH"}],
                "warning": "",
            },
            database_context=database_context,
        )

    monkeypatch.setattr(app.state.registry, "connection", connection)

    def switch(source, role):
        response = client.post("/api/dashboards/a/roles", json={"source": source, "role": role})
        assert response.status_code == 200, response.text
        return response.json()

    with TestClient(app) as client:
        client.headers["X-Sqldash-Token"] = app.state.api_token
        kept = switch("", "BOTH")
        assert kept["source"] == '@role:["a.source","BOTH"]'
        assert {"database_note", "database_warning"}.isdisjoint(kept)

        learner = switch("", "LEARNER")
        assert split_source_context(learner["source"]) == ("a.source", "LEARNER", "LRN", None)
        assert learner["database"] == "LRN"
        assert learner["database_note"] == (
            "LEARNER cannot use database DEV, so queries run in LRN."
        )

        tried.clear()
        back = switch(learner["source"], "BASE")
        assert split_source_context(back["source"]) == ("a.source", "BASE", "LRN", None)
        assert "database_note" not in back
        picked = client.post(
            "/api/dashboards/a/databases", json={"source": back["source"], "database": "LRN"}
        ).json()
        assert split_source_context(picked["source"])[2] == "LRN"

        tried.clear()
        configured = switch(context_source("a.source", "LEARNER", "SHARED"), "BOTH")
        assert configured["source"] == '@role:["a.source","BOTH"]'
        assert configured["database_note"] == (
            "BOTH cannot use database SHARED, so queries run in DEV."
        )
        assert tried == [("BOTH", "SHARED"), ("BOTH", "DEV")]

        hidden = switch("", "HIDDEN")
        assert hidden["source"] == '@role:["a.source","HIDDEN"]'
        assert hidden["database_warning"] == (
            "HIDDEN cannot use database DEV, and it cannot see any database. Pick another role."
        )
        looker = switch("", "LOOKER")
        assert looker["database_warning"] == (
            "LOOKER cannot use database DEV or any database it can see. Pick another role."
        )
        listed = client.get("/api/dashboards/a/databases", params={"source": looker["source"]})
        assert listed.json()["warning"] == (
            "LOOKER cannot use database DEV. Pick a database it can see."
        )
        fine = client.get("/api/dashboards/a/databases", params={"source": kept["source"]})
        assert "warning" not in fine.json()
