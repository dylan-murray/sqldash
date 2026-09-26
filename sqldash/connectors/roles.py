"""Live role capabilities and per-connection enforcement, never global session mutation."""

import json

from sqlalchemy import event

from sqldash.connectors.base import ConnectorError, source_url
from sqldash.secrets import interpolate_env

SWITCHABLE = {"snowflake", "postgresql", "mysql", "mariadb", "trino", "clickhouse"}
POSTGRES_SESSION_RESET = (
    "SET SESSION AUTHORIZATION DEFAULT",
    "RESET ALL",
    "SELECT pg_advisory_unlock_all()",
)
SNOWFLAKE_SECONDARY_OFF = "USE SECONDARY ROLES NONE"


def provider(source):
    if source.url:
        name = source_url(source).get_backend_name()
    else:
        name = (source.driver or source.type or "").split("+", 1)[0]
    return {"postgres": "postgresql"}.get(name, name)


def mysql_role(value):
    if value.startswith("["):
        pair = json.loads(value)
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or not all(isinstance(part, str) and part for part in pair)
        ):
            raise ConnectorError("MySQL role must be a name or a JSON [name, host] pair")
        return pair
    return value, "%"


def role_label(source, role):
    if provider(source) == "mysql":
        return "@".join(mysql_role(role))
    return role


def clickhouse_http(source):
    driver = source_url(source).drivername if source.url else source.driver or "clickhouse"
    return driver in {"clickhouse", "clickhouse+http"}


def role_connect_args(source, args):
    if provider(source) != "clickhouse" or not source.role:
        return args
    if not clickhouse_http(source):
        raise ConnectorError("Role selection currently requires the ClickHouse HTTP driver")
    return {
        **args,
        "ch_settings": {**args.get("ch_settings", {}), "role": interpolate_env(source.role)},
    }


def snowflake_secondary_off(source):
    """Whether checkout turns secondary roles off: by default only when a role is set.

    A role narrows nothing while the user's DEFAULT_SECONDARY_ROLES (ALL unless an
    admin changed it) stays active, because every granted role's privileges apply.
    """
    if source.secondary_roles is not None:
        return not source.secondary_roles
    return bool(source.role or source.connect_args.get("role"))


def snowflake_name(value):
    """A configured Snowflake identifier as Snowflake reports it: unquoted names fold up."""
    if value.startswith('"') and value.endswith('"'):
        return value[1:-1].replace('""', '"')
    return value.upper()


def configured_warehouse(source):
    """The warehouse a Snowflake source names, as Snowflake reports it, or None."""
    value = source.warehouse or source.connect_args.get("warehouse")
    return snowflake_name(interpolate_env(value)) if value else None


def configured_database(source):
    """The database a Snowflake source names, as Snowflake reports it, or None."""
    value = source.database or source.connect_args.get("database")
    return snowflake_name(interpolate_env(value)) if value else None


def database_warning(role, configured, visible):
    """What to say when a Snowflake session has no current database, like the warehouse."""
    lacking = (
        f"{role} cannot use database {configured}"
        if configured
        else f"No database is active for {role}"
    )
    if visible:
        return f"{lacking}. Pick a database it can see."
    return f"{lacking}, and it cannot see any database. Pick another role."


def snowflake_warehouses(conn, source, role, current):
    """The session's warehouse, the ones its role can see, and a warning when it has none.

    Snowflake keeps the session's warehouse across `USE ROLE`, but reports it (and
    runs queries on it) only while the role has a privilege on it, so a primary role
    that lacks one silently leaves the session with no warehouse.
    """
    try:
        shown = conn.exec_driver_sql("SHOW WAREHOUSES").mappings().all()
    except Exception:
        shown = []
    warehouses = [
        {"value": row["name"], "label": row["name"], "detail": row.get("size") or ""}
        for row in shown
    ]
    configured = configured_warehouse(source)
    warning = ""
    if not current:
        names = ", ".join(w["value"] for w in warehouses)
        lacking = (
            f"{role} cannot use warehouse {configured}"
            if configured
            else f"No warehouse is active for {role}"
        )
        if names:
            warning = f"{lacking}. Warehouses it can see: {names}. Pick one to run queries."
        else:
            warning = f"{lacking}, and it cannot see any warehouse. Pick another role."
    return {
        "warehouse": current,
        "configured_warehouse": configured,
        "warehouses": sorted(warehouses, key=lambda w: w["label"]),
        "warning": warning,
    }


def install_role(engine, source):
    kind = provider(source)
    role = interpolate_env(source.role) if source.role else None
    if role and kind not in SWITCHABLE:
        raise ConnectorError(f"{kind} does not support session-role selection")
    if kind not in SWITCHABLE or kind == "clickhouse":
        return
    secondary_off = kind == "snowflake" and snowflake_secondary_off(source)
    preparer = engine.dialect.identifier_preparer

    def quote(value):
        # quote_identifier doubles % for pyformat parameters, but these statements
        # run on the raw DBAPI cursor with no parameters, so nothing un-doubles it.
        return preparer.quote_identifier(value).replace("%%", "%")

    @event.listens_for(engine, "checkout")
    def apply_role(connection, record, proxy):
        cursor = connection.cursor()
        try:
            target = role
            if kind == "snowflake":
                if target:
                    target = snowflake_name(target)
                else:
                    if "sqldash_role" not in record.info:
                        cursor.execute("SELECT CURRENT_ROLE()")
                        record.info["sqldash_role"] = cursor.fetchone()[0]
                    target = record.info["sqldash_role"]
                if target:
                    # semgrep: roles cannot be bound; quote() escapes them as identifiers
                    # nosemgrep: formatted-sql-query, sqlalchemy-execute-raw-query
                    cursor.execute(f"USE ROLE {quote(target)}")
                if secondary_off:
                    cursor.execute(SNOWFLAKE_SECONDARY_OFF)
            elif kind == "postgresql":
                for statement in POSTGRES_SESSION_RESET:
                    cursor.execute(statement)
                if target is None:
                    if "sqldash_role" not in record.info:
                        cursor.execute("SELECT current_user")
                        record.info["sqldash_role"] = cursor.fetchone()[0]
                    target = record.info["sqldash_role"]
                # semgrep: roles cannot be bound; quote() escapes them as identifiers
                # nosemgrep: formatted-sql-query, sqlalchemy-execute-raw-query
                cursor.execute(f"SET ROLE {quote(target)}")
                # SET ROLE is transactional; pool rollback must not undo it.
                connection.commit()
            elif target and kind == "mysql":
                name, host = mysql_role(target)
                # semgrep: roles cannot be bound; quote() escapes them as identifiers
                # nosemgrep: formatted-sql-query, sqlalchemy-execute-raw-query
                cursor.execute(f"SET ROLE {quote(name)}@{quote(host)}")
            elif target and kind == "mariadb":
                # semgrep: roles cannot be bound; quote() escapes them as identifiers
                # nosemgrep: formatted-sql-query, sqlalchemy-execute-raw-query
                cursor.execute(f"SET ROLE {quote(target)}")
            elif target and kind == "trino":
                # semgrep: roles cannot be bound; quote() escapes them as identifiers
                # nosemgrep: formatted-sql-query, sqlalchemy-execute-raw-query
                cursor.execute(f"SET ROLE {quote(target)}")
                cursor.fetchall()
        finally:
            cursor.close()


def role_context(engine, source):
    kind = provider(source)
    result = {"switchable": False, "current": [], "roles": [], "note": "", "label": "Role"}
    notes = {
        "duckdb": "DuckDB does not use database roles.",
        "sqlite": "SQLite does not use database roles.",
        "bigquery": "Access is governed by Google Cloud IAM for this connection.",
        "athena": "Access is governed by AWS IAM and Lake Formation for this connection.",
        "awsathena": "Access is governed by AWS IAM and Lake Formation for this connection.",
        "databricks": "Access follows this connection's identity and Unity Catalog grants.",
    }
    if kind in notes:
        return {**result, "note": notes[kind]}
    if kind not in SWITCHABLE | {"mssql", "redshift"}:
        return {**result, "note": "This driver does not expose session-role selection."}
    with engine.connect() as conn:

        def rows(sql):
            return conn.exec_driver_sql(sql).fetchall()

        def scalar(sql):
            return conn.exec_driver_sql(sql).scalar()

        roles = []
        current = []
        if kind == "snowflake":
            active, available, secondary, warehouse = rows(
                "SELECT CURRENT_ROLE(), CURRENT_AVAILABLE_ROLES(), CURRENT_SECONDARY_ROLES(), "
                "CURRENT_WAREHOUSE()"
            )[0]
            current = [active] if active else []
            roles = json.loads(available) if isinstance(available, str) else available
            secondary = json.loads(secondary) if isinstance(secondary, str) else secondary
            result["label"] = "Primary role"
            enabled = secondary.get("value") if isinstance(secondary, dict) else secondary
            granted = secondary.get("roles") if isinstance(secondary, dict) else ""
            if not enabled:
                result["note"] = "Secondary roles: none"
            else:
                result["note"] = f"Secondary roles: {enabled}"
                if granted:
                    result["note"] += f", so grants of {granted} also apply"
                if source.secondary_roles is None:
                    result["note"] += ". Picking a role turns them off"
                result["note"] += "."
            result["secondary"] = str(enabled) if enabled else ""
            try:
                shown = conn.exec_driver_sql("SHOW ROLES").mappings().all()
                comments = {row["name"]: row.get("comment") for row in shown if row.get("comment")}
            except Exception:
                comments = {}
            roles = [{"value": r, "label": r, "detail": comments.get(r, "")} for r in roles]
            result.update(snowflake_warehouses(conn, source, active, warehouse))
        elif kind == "postgresql":
            current = [scalar("SELECT current_user")]
            version = int(scalar("SHOW server_version_num"))
            privilege = "SET" if version >= 160000 else "MEMBER"
            roles = [
                r[0]
                for r in rows(
                    "SELECT rolname FROM pg_roles WHERE "
                    f"pg_has_role(session_user, oid, '{privilege}') ORDER BY rolname"
                )
            ]
        elif kind == "mysql":
            version = str(scalar("SELECT VERSION()"))
            if "MariaDB" in version:
                raise ConnectorError("Use type: mariadb to discover and switch MariaDB roles")
            if tuple(int(p) for p in version.split("-")[0].split(".")[:3]) < (8, 0, 19):
                return {**result, "note": "Role discovery requires MySQL 8.0.19 or newer."}
            current = [
                f"{r[0]}@{r[1]}"
                for r in rows("SELECT ROLE_NAME, ROLE_HOST FROM information_schema.ENABLED_ROLES")
            ]
            roles = [
                {
                    "value": json.dumps([r[0], r[1]], separators=(",", ":")),
                    "label": f"{r[0]}@{r[1]}",
                }
                for r in rows(
                    "SELECT ROLE_NAME, ROLE_HOST FROM information_schema.APPLICABLE_ROLES"
                )
            ]
        elif kind == "mariadb":
            active = scalar("SELECT CURRENT_ROLE()")
            current = [active] if active else []
            roles = [
                r[0] for r in rows("SELECT ROLE_NAME FROM information_schema.APPLICABLE_ROLES")
            ]
        elif kind == "trino":
            current = [r[0] for r in rows("SHOW CURRENT ROLES")]
            roles = [r[0] for r in rows("SHOW ROLE GRANTS")]
            result["note"] = "System roles; catalog-specific grants remain unchanged."
        elif kind == "clickhouse":
            current = list(scalar("SELECT currentRoles()"))
            if not clickhouse_http(source):
                return {
                    **result,
                    "current": current,
                    "note": "Role selection currently requires the ClickHouse HTTP driver.",
                }
            roles = [
                r[0]
                for r in rows(
                    "SELECT granted_role_name FROM system.role_grants "
                    "WHERE user_name = currentUser()"
                )
            ]
            version = tuple(int(p) for p in str(scalar("SELECT version()")).split(".")[:2])
            if version < (24, 4):
                return {
                    **result,
                    "current": current,
                    "note": "HTTP role selection requires ClickHouse 24.4 or newer.",
                }
        elif kind == "mssql":
            current = [
                r[0]
                for r in rows(
                    "SELECT name FROM sys.database_principals "
                    "WHERE type = 'R' AND IS_ROLEMEMBER(name)=1"
                )
            ]
            return {
                **result,
                "current": current,
                "note": "Database role memberships apply together; SQL Server has no SET ROLE.",
            }
        elif kind == "redshift":
            current = [
                r[0]
                for r in rows(
                    "SELECT role_name FROM svv_user_grants WHERE user_name = current_user"
                )
            ]
            return {
                **result,
                "current": current,
                "note": "Granted roles apply together; Redshift has no SET ROLE.",
            }
    options = [r if isinstance(r, dict) else {"value": r, "label": r} for r in roles or []]
    return {
        **result,
        "switchable": True,
        "current": current,
        "roles": sorted({r["value"]: r for r in options}.values(), key=lambda r: r["label"]),
    }
