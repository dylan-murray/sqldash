from uuid import uuid4

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import ConfigDict, Field, field_validator

from sqldash.api.helpers import StrictBody
from sqldash.api.routes_sources import picker_http
from sqldash.params import (
    ParamError,
    extract_params,
    filter_param_names,
    filter_params,
    render_conditionals,
)
from sqldash.project.query_library import LibraryQuery, QueryLibrary

router = APIRouter(prefix="/api/dashboards/{name:dname}/library")


class QueryInput(StrictBody):
    title: str = Field(min_length=1, max_length=160)
    sql: str = Field(min_length=1, max_length=500_000)
    source: str = Field(default="", max_length=4096)

    @field_validator("title", "sql")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()


def library(request: Request, name: str) -> QueryLibrary:
    store = request.app.state.store
    store.load(name)
    if hasattr(store, "repos"):
        store, _, _ = store.split(name)
    return QueryLibrary(store.root)


def definition(request: Request, name: str, query_id: str, body: QueryInput) -> LibraryQuery:
    store = request.app.state.store
    dashboard, _, _ = store.load(name)
    picker_http(store, request.app.state.layer, name, body.source, dashboard=dashboard)
    source = body.source
    if not source or source == dashboard.default_source_name:
        source = f"{name}.source"
    elif source in dashboard.sources:
        source = f"{name}.sources.{source}"
    try:
        render_conditionals(body.sql, {})
    except ParamError as exc:
        raise HTTPException(422, f"SQL template syntax is invalid: {exc}") from exc
    variables = set(extract_params(body.sql))
    parameters = [f for f in dashboard.filters if variables & set(filter_param_names(f))]
    if variables - set(filter_params(dashboard)):
        raise HTTPException(422, "Define the query's parameters in this dashboard before saving")
    return LibraryQuery(
        id=query_id, title=body.title.strip(), sql=body.sql, source=source, parameters=parameters
    )


@router.get("")
def list_queries(request: Request, name: str):
    entries, errors = library(request, name).list()
    return {"queries": entries, "errors": errors, "semantics": "copy"}


@router.post("", status_code=201)
def create_query(request: Request, name: str, body: QueryInput):
    query = definition(request, name, uuid4().hex, body)
    etag = library(request, name).save(query, "*")
    return {**query.model_dump(mode="json"), "etag": etag}


@router.put("/{query_id}")
def update_query(
    request: Request, name: str, query_id: str, body: QueryInput, if_match: str = Header(...)
):
    target = library(request, name)
    target.existing(query_id)
    query = definition(request, name, query_id, body)
    etag = target.save(query, if_match)
    return {**query.model_dump(mode="json"), "etag": etag}


class QueryTitle(StrictBody):
    model_config = ConfigDict(str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=160)


@router.patch("/{query_id}")
def rename_query(
    request: Request, name: str, query_id: str, body: QueryTitle, if_match: str = Header(...)
):
    target = library(request, name)
    query, _ = target.load(query_id)
    query = query.model_copy(update={"title": body.title})
    etag = target.save(query, if_match)
    return {**query.model_dump(mode="json"), "etag": etag}


@router.delete("/{query_id}")
def delete_query(request: Request, name: str, query_id: str, if_match: str = Header(...)):
    library(request, name).delete(query_id, if_match)
    return {"deleted": query_id}


@router.get("/{query_id}/open")
def open_query(request: Request, name: str, query_id: str):
    query, etag = library(request, name).load(query_id)
    store = request.app.state.store
    dashboard, _, _ = store.load(name)
    source = query.source
    if source == f"{name}.source":
        source = ""
    elif source.startswith(f"{name}.sources."):
        source = source[len(f"{name}.sources.") :]
    picker_http(store, request.app.state.layer, name, source, dashboard=dashboard)
    current = {f.name: f.model_dump(mode="json") for f in dashboard.filters}
    incompatible = [
        f.name for f in query.parameters if current.get(f.name) != f.model_dump(mode="json")
    ]
    if incompatible:
        raise HTTPException(
            422,
            {"message": "Dashboard parameters differ from this query", "parameters": incompatible},
        )
    return {
        "id": query.id,
        "etag": etag,
        "state": {"sql": query.sql, "title": query.title, "source": source, "mode": "sql"},
        "semantics": "copy",
    }
