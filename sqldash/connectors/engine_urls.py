"""Flat source config → SQLAlchemy URL (or connect kwargs, for snowflake).

Authors write human fields — type, host, database, username — and sqldash
assembles the dialect string, so nobody hand-writes ``postgresql+psycopg://``.
Credentials come from profiles/env at build time and exist only in the
in-memory URL, never in project files.
"""

from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import URL

from sqldash.connectors.base import ConnectorError
from sqldash.secrets import interpolate_env, resolve_credentials

if TYPE_CHECKING:
    from sqldash.models.source import Source

DRIVERS = {
    "postgres": "postgresql+psycopg",
    "postgresql": "postgresql+psycopg",
    "mysql": "mysql+pymysql",
    "mariadb": "mariadb+pymysql",
    "mssql": "mssql+pyodbc",
    "sqlite": "sqlite",
    "duckdb": "duckdb",
    "redshift": "redshift+redshift_connector",
    "bigquery": "bigquery",
    "databricks": "databricks",
    "athena": "awsathena+rest",
    "trino": "trino",
    "clickhouse": "clickhouse",
}

INSTALL_EXTRAS = {
    "bigquery": "bigquery",
    "redshift": "redshift",
    "databricks": "databricks",
    "athena": "athena",
    "mysql": "mysql",
    "mariadb": "mysql",
    "trino": "trino",
    "clickhouse": "clickhouse",
    "snowflake": "snowflake",
    "postgres": "postgres",
    "postgresql": "postgres",
}

FILE_DATABASES = ("duckdb", "sqlite")


# The connection fields each path actually reads, other than the credentials
# `resolve_credentials` already resolves. Interpolating at each use site meant
# `warehouse` and `role` were passed to Snowflake as the literal string
# "${env:SNOWFLAKE_WAREHOUSE}" — the driver connects, has no warehouse, and
# every query fails with "No active warehouse selected in the current session",
# which reads like a permissions problem rather than an unexpanded reference.
#
# Scoped per path rather than one list for the whole model: expanding a field
# the path ignores turns a working source into a hard failure naming a variable
# that has nothing to do with the connection. `url` wins verbatim, so a leftover
# `host:` beside it is dead — lint says so — and must not be resolved.
URL_ENV_FIELDS = (
    "host",
    "database",
    "db_schema",
    "driver",
    "project",
    "catalog",
    "http_path",
    "options",
)
SNOWFLAKE_ENV_FIELDS = ("account", "warehouse", "database", "db_schema", "role")


def resolve_source_env(source: "Source", fields: "tuple[str, ...]") -> "Source":
    """Expand ``${env:VAR}`` in the named connection fields, failing loudly on unset."""
    updates: dict[str, Any] = {}
    for field in fields:
        value = getattr(source, field, None)
        if isinstance(value, str):
            updates[field] = interpolate_env(value)
        elif field == "options" and value:
            updates["options"] = {
                k: interpolate_env(v) if isinstance(v, str) else v for k, v in value.items()
            }
    return source.model_copy(update=updates)


def snowflake_connect_kwargs(source: "Source", credentials: dict[str, Any]) -> dict[str, Any]:
    """Infer the auth method from whichever secret is present (default externalbrowser SSO).

    ``network_timeout`` is deliberately omitted: the Snowflake cursor uses it
    as a query timebomb (cancel after N seconds) when ``cursor.execute`` is
    called without ``timeout=``. The driver's default is infinite. Authors
    who want a cap set ``connect_args.network_timeout``; those merge in below.
    ``login_timeout`` stays 30 — that one is login-only. It does not bound the
    externalbrowser wait for the SSO callback, which has no timeout in the
    driver; ``build_engine`` bounds that wait itself.
    """
    source = resolve_source_env(source, SNOWFLAKE_ENV_FIELDS)
    method = credentials.get("authentication") or (
        "password"
        if credentials.get("password")
        else "pat"
        if credentials.get("token")
        else "keypair"
        if credentials.get("private_key_path")
        else "externalbrowser"
    )
    kwargs: dict[str, Any] = {
        "account": source.account,
        "user": credentials.get("username"),
        "warehouse": source.warehouse,
        "database": source.database,
        "schema": source.db_schema,
        "role": source.role,
        "client_session_keep_alive": True,
        "login_timeout": 30,
    }
    if method == "externalbrowser":
        kwargs["authenticator"] = "externalbrowser"
        kwargs["client_store_temporary_credential"] = True
    elif method == "pat":
        token = credentials.get("token")
        if not token:
            raise ConnectorError(
                "authentication: pat requires 'token' (e.g. token: ${env:SNOWFLAKE_PAT})"
            )
        kwargs["authenticator"] = "PROGRAMMATIC_ACCESS_TOKEN"
        kwargs["token"] = token
    elif method == "password":
        password = credentials.get("password")
        if not password:
            raise ConnectorError(
                "authentication: password requires 'password' "
                "(e.g. password: ${env:SNOWFLAKE_PASSWORD})"
            )
        kwargs["password"] = password
    elif method == "keypair":
        key_path = credentials.get("private_key_path")
        if not key_path:
            raise ConnectorError("authentication: keypair requires 'private_key_path'")
        kwargs["private_key_file"] = str(Path(key_path).expanduser())
        if credentials.get("private_key_passphrase"):
            kwargs["private_key_file_pwd"] = credentials["private_key_passphrase"]
    extras = interpolate_env(source.connect_args) if source.connect_args else {}
    kwargs.update(extras)
    return {k: v for k, v in kwargs.items() if v is not None}


def build_url(source, base_dir: Path):
    """An explicit ``url`` wins verbatim; otherwise assemble it from the flat fields."""
    if source.url:
        return interpolate_env(source.url)
    source = resolve_source_env(source, URL_ENV_FIELDS)
    if source.type == "bigquery":
        project = source.project or source.host or ""
        if not project:
            raise ConnectorError("bigquery sources require 'project'")
        dataset = source.database or source.db_schema
        return URL.create("bigquery", host=project, database=dataset, query=source.options or {})
    if source.type == "databricks":
        credentials = resolve_credentials(source)
        token = credentials.get("token") or credentials.get("password")
        if not source.host or not source.http_path or not token:
            raise ConnectorError(
                "databricks sources require 'host', 'http_path', and a 'token' "
                "(e.g. token: ${env:DATABRICKS_TOKEN})"
            )
        query = {"http_path": source.http_path}
        if source.catalog:
            query["catalog"] = source.catalog
        if source.db_schema:
            query["schema"] = source.db_schema
        query.update(source.options or {})
        return URL.create(
            "databricks",
            username="token",
            password=token,
            host=source.host,
            port=source.port or 443,
            query=query,
        )
    if source.type == "athena":
        credentials = resolve_credentials(source)
        host = source.host or ""
        if host and "." not in host:
            host = f"athena.{host}.amazonaws.com"
        if not host:
            raise ConnectorError(
                "athena sources require 'host' (an AWS region like us-east-1, "
                "or a full athena endpoint)"
            )
        return URL.create(
            "awsathena+rest",
            username=credentials.get("username"),
            password=credentials.get("password"),
            host=host,
            port=source.port or 443,
            database=source.db_schema or source.database,
            query=source.options or {},
        )
    dialect = source.driver or DRIVERS.get(source.type, source.type)
    if source.type in FILE_DATABASES:
        database = source.database or ":memory:"
        if database == ":memory:":
            return f"{dialect}:///:memory:"
        return f"{dialect}:///{(base_dir / database).resolve()}"
    credentials = resolve_credentials(source)
    return URL.create(
        dialect,
        username=credentials.get("username"),
        password=credentials.get("password"),
        host=source.host,
        port=source.port,
        database=source.database,
        query=source.options or {},
    )
