from fastapi import APIRouter, HTTPException, Request
from pydantic import Field

from sqldash.api.helpers import StrictBody
from sqldash.connectors.roles import (
    configured_database,
    configured_warehouse,
    database_warning,
    provider,
    role_label,
    snowflake_name,
)
from sqldash.project.source_context import context_source, split_source_context
from sqldash.project.sources import distinct_picker_sources, picker_sources, resolve_picker_source

router = APIRouter(prefix="/api")
FALLBACK_TRIES = 3


def picker_http(store, layer, name: str, key: str | None, dashboard=None):
    try:
        return resolve_picker_source(store, layer, name, key, dashboard=dashboard)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except KeyError as exc:
        known = exc.args[1] if len(exc.args) > 1 else []
        named = ", ".join(known) or "(none defined)"
        raise HTTPException(404, f"no source named '{key}' — known sources: {named}") from exc


@router.get("/dashboards/{name:dname}/available-sources")
def available_sources(request: Request, name: str):
    store = request.app.state.store
    dashboard, _, _ = store.load(name)
    return {
        "sources": [
            {"key": e.key, "label": e.label, "kind": e.kind}
            for e in distinct_picker_sources(
                picker_sources(store, request.app.state.layer, name, dashboard=dashboard)
            )
        ]
    }


@router.get("/dashboards/{name:dname}/schema")
def source_schema(
    request: Request, name: str, source: str | None = None, database: str | None = None
):
    store = request.app.state.store
    dashboard, _, _ = store.load(name)
    selected, base_dir = picker_http(
        store, request.app.state.layer, name, source, dashboard=dashboard
    )
    with request.app.state.registry.connection(selected, base_dir) as connector:
        tables = connector.introspect_database(database) if database else connector.introspect()
        quote = connector.sql_identifier
    return {
        "tables": [
            {
                "schema": t.schema,
                "name": t.name,
                "sql": ".".join(quote(part) for part in (database, t.schema, t.name) if part),
                "name_sql": quote(t.name),
                "columns": [{"name": c[0], "type": c[1], "sql": quote(c[0])} for c in t.columns],
            }
            for t in tables
        ]
    }


def _database_context(request, name, source):
    selected, base_dir = picker_http(request.app.state.store, request.app.state.layer, name, source)
    with request.app.state.registry.connection(selected, base_dir) as connector:
        return connector.database_context()


@router.get("/dashboards/{name:dname}/databases")
def source_databases(request: Request, name: str, source: str | None = None):
    store, layer = request.app.state.store, request.app.state.layer
    context = _database_context(request, name, source)
    selected, _ = picker_http(store, layer, name, source)
    if provider(selected) != "snowflake" or context["current"]:
        return context
    base, role, database, _ = split_source_context(source)
    configured = configured_database(picker_http(store, layer, name, base)[0])
    context["warning"] = database_warning(
        snowflake_name(role) if role else "The connection's role",
        database or configured,
        context["databases"],
    )
    return context


class RoleSelection(StrictBody):
    source: str = Field(default="", max_length=4096)
    role: str | None = Field(default=None, min_length=1, max_length=1000)


class DatabaseSelection(StrictBody):
    source: str = Field(default="", max_length=4096)
    database: str | None = Field(default=None, min_length=1, max_length=1000)


class WarehouseSelection(StrictBody):
    source: str = Field(default="", max_length=4096)
    warehouse: str | None = Field(default=None, min_length=1, max_length=1000)


def _canonical(store, layer, name, base):
    """(canonical project reference, picker key, label) for a base source key."""
    dashboard, _, _ = store.load(name)
    canonical = base or f"{name}.source"
    if base == dashboard.default_source_name:
        canonical = f"{name}.source"
    elif base in dashboard.sources:
        canonical = f"{name}.sources.{base}"
    picker_key = base or ""
    if canonical == f"{name}.source":
        picker_key = ""
    elif canonical.startswith(f"{name}.sources."):
        picker_key = canonical[len(f"{name}.sources.") :]
    label = next(
        (e.label for e in picker_sources(store, layer, name) if e.key == picker_key), canonical
    )
    return canonical, picker_key, label


def _keyed(canonical, picker_key, role, database, warehouse):
    if role is None and database is None and warehouse is None:
        return picker_key
    return context_source(canonical, role, database, warehouse)


def _role_context(request, name, source):
    store, layer = request.app.state.store, request.app.state.layer
    selected, base_dir = picker_http(store, layer, name, source)
    base, role, database, warehouse = split_source_context(source)
    canonical, picker_key, label = _canonical(store, layer, name, base)
    with request.app.state.registry.connection(selected, base_dir) as connector:
        context = connector.role_context()
    if role and role_label(selected, role) not in context["current"]:
        raise HTTPException(422, "The connection did not activate the requested role")
    if warehouse is not None:
        context["configured_warehouse"] = configured_warehouse(
            picker_http(store, layer, name, base)[0]
        )
    return {
        **context,
        "source": source or "",
        "base_source": picker_key,
        "canonical_source": canonical,
        "selected": role,
        "database": database,
        "selected_warehouse": warehouse,
        "source_label": label,
    }


@router.get("/dashboards/{name:dname}/roles")
def source_roles(request: Request, name: str, source: str | None = None):
    return _role_context(request, name, source)


@router.post("/dashboards/{name:dname}/roles")
def select_source_role(request: Request, name: str, body: RoleSelection):
    picker_http(request.app.state.store, request.app.state.layer, name, body.source)
    base, _, database, warehouse = split_source_context(body.source)
    context = _role_context(request, name, base)
    if body.role is not None and not context["switchable"]:
        raise HTTPException(422, context["note"] or "This connection cannot switch roles")
    if body.role is not None and body.role not in {r["value"] for r in context["roles"]}:
        raise HTTPException(422, "That role is not available to this connection; refresh roles")
    canonical, picker_key = context["canonical_source"], context["base_source"]
    switched = _warehouse_fallback(
        request, name, canonical, picker_key, body.role, database, warehouse
    )
    if switched.get("warehouses") is None:
        return switched
    return _database_fallback(request, name, switched, database)


def _warehouse_fallback(request, name, canonical, picker_key, role, database, warehouse):
    switched = _role_context(
        request, name, _keyed(canonical, picker_key, role, database, warehouse)
    )
    if switched.get("warehouses") is None or switched["warehouse"]:
        return switched
    lost = warehouse or switched["configured_warehouse"]
    usable = [w["value"] for w in switched["warehouses"]]
    candidates = [w for w in usable if w not in {lost, switched["configured_warehouse"]}]
    if warehouse is not None and switched["configured_warehouse"] in usable:
        candidates.insert(0, None)
    for candidate in candidates[:FALLBACK_TRIES]:
        attempt = _role_context(
            request, name, _keyed(canonical, picker_key, role, database, candidate)
        )
        if attempt["warehouse"]:
            active = ", ".join(attempt["current"])
            attempt["warehouse_note"] = (
                f"{active} cannot use warehouse {lost}, so queries run on {attempt['warehouse']}."
            )
            return attempt
    if usable and lost:
        active = ", ".join(switched["current"])
        switched["warning"] = (
            f"{active} cannot use warehouse {lost} or any warehouse it can see. Pick another role."
        )
    return switched


def _database_fallback(request, name, switched, database):
    """Keep the database when the new role can use it, else land on one it can.

    Like the warehouse, Snowflake keeps the session's database across a role
    change but reports it (and resolves names in it) only while the role may use
    it, so a role that cannot see the configured database had no database at all.
    The account's own databases come before shared and application ones, which
    is what a role's grants usually point at.
    """
    key = switched["source"]
    base, role, _, warehouse = split_source_context(key)
    found = _database_context(request, name, key)
    if found["current"]:
        return switched
    canonical, picker_key = switched["canonical_source"], switched["base_source"]
    configured = configured_database(
        picker_http(request.app.state.store, request.app.state.layer, name, base)[0]
    )
    lost = database or configured
    visible = found["databases"]
    kinds = found.get("kinds", {})
    candidates = sorted(
        (d for d in visible if d not in {lost, configured}),
        key=lambda d: kinds.get(d, "STANDARD") != "STANDARD",
    )
    if database is not None and configured in visible:
        candidates.insert(0, None)
    active = ", ".join(switched["current"])
    for candidate in candidates[:FALLBACK_TRIES]:
        attempt = _keyed(canonical, picker_key, role, candidate, warehouse)
        landed = _database_context(request, name, attempt)["current"]
        if landed:
            lacking = (
                f"{active} cannot use database {lost}"
                if lost
                else f"No database is active for {active}"
            )
            return {
                **switched,
                "source": attempt,
                "database": candidate,
                "database_note": f"{lacking}, so queries run in {landed}.",
            }
    if visible:
        switched["database_warning"] = (
            f"{active} cannot use database {lost} or any database it can see. Pick another role."
            if lost
            else f"{active} cannot use any database it can see. Pick another role."
        )
    else:
        switched["database_warning"] = database_warning(active, lost, visible)
    return switched


@router.post("/dashboards/{name:dname}/databases")
def select_source_database(request: Request, name: str, body: DatabaseSelection):
    """Pick the database unqualified SQL runs in, keeping the chosen role.

    Choosing the connection's own default database clears the override, so a
    query only carries a database when it differs from what the source says.
    """
    store, layer = request.app.state.store, request.app.state.layer
    picker_http(store, layer, name, body.source)
    base, role, _, warehouse = split_source_context(body.source)
    canonical, picker_key, _ = _canonical(store, layer, name, base)
    without = _keyed(canonical, picker_key, role, None, warehouse)
    selected, base_dir = picker_http(store, layer, name, without)
    with request.app.state.registry.connection(selected, base_dir) as connector:
        context = connector.database_context()
    database = body.database
    if database is not None and database not in context["databases"]:
        raise HTTPException(422, "That database is not available to this connection; refresh")
    if database == context["current"]:
        database = None
    key = _keyed(canonical, picker_key, role, database, warehouse)
    return {"source": key, "database": database or context["current"]}


@router.post("/dashboards/{name:dname}/warehouses")
def select_source_warehouse(request: Request, name: str, body: WarehouseSelection):
    """Pick the warehouse queries run on, keeping the chosen role and database.

    Choosing the warehouse the session already has clears the override, so a
    query only carries a warehouse when the source's own one is not usable.
    """
    store, layer = request.app.state.store, request.app.state.layer
    picker_http(store, layer, name, body.source)
    base, role, database, _ = split_source_context(body.source)
    canonical, picker_key, _ = _canonical(store, layer, name, base)
    without = _keyed(canonical, picker_key, role, database, None)
    context = _role_context(request, name, without)
    if "warehouses" not in context:
        raise HTTPException(422, "Choosing a query warehouse is only available for Snowflake")
    warehouse = body.warehouse
    if warehouse is not None and warehouse not in {w["value"] for w in context["warehouses"]}:
        raise HTTPException(422, "That warehouse is not available to this role; refresh")
    if warehouse == context["warehouse"]:
        warehouse = None
    return _role_context(request, name, _keyed(canonical, picker_key, role, database, warehouse))
