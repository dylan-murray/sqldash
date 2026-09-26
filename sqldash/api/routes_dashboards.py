import difflib
import unicodedata
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import ValidationError, field_validator

from sqldash.api.helpers import StrictBody, client_payload
from sqldash.models.dashboard import (
    TILE_LEVEL_DIMENSIONS_HINT,
    Dashboard,
    FilterDef,
    Position,
    Tile,
    dashboard_stem,
    slugify,
)
from sqldash.models.source import source_as_project_yaml
from sqldash.project.catalog import list_dashboards as catalog_dashboards
from sqldash.project.sources import alias_for_picker_key, resolve_picker_source, source_for_copy
from sqldash.project.store import (
    ConflictError,
    bound_file_stem,
    build_dashboard_text,
    plain_validation_message,
)

router = APIRouter(prefix="/api")


class SaveRequest(StrictBody):
    text: str


class PositionsRequest(StrictBody):
    positions: dict[str, Position]


class TileRequest(StrictBody):
    """Editor payload. Unknown tile keys are refused; sql is a sibling of tile."""

    tile: dict[str, Any]
    sql: str | None = None

    @field_validator("tile")
    @classmethod
    def known_tile_fields(cls, tile: dict[str, Any]) -> dict[str, Any]:
        unknown = [key for key in tile if key not in Tile.model_fields]
        parts: list[str] = []
        if "dimensions" in unknown:
            parts.append(TILE_LEVEL_DIMENSIONS_HINT)
            unknown = [key for key in unknown if key != "dimensions"]
        if unknown:
            key = unknown[0]
            close = difflib.get_close_matches(str(key), list(Tile.model_fields), n=1)
            hint = f" — did you mean '{close[0]}'?" if close else ""
            parts.append(f"unknown tile field '{key}'{hint}")
        if parts:
            raise ValueError("; ".join(parts))
        if tile.get("sql"):
            raise ValueError("sql belongs as a sibling of 'tile', not inside it")
        try:
            Tile.model_validate(tile)
        except ValidationError as exc:
            raise ValueError(plain_validation_message(exc.errors()[0])) from exc
        return tile


class MetaRequest(StrictBody):
    title: str | None = None
    description: str | None = None


class FilterWrite(FilterDef):
    """GET injects resolved_default; accept it on PUT without ignoring other extras."""

    resolved_default: Any = None


class FiltersRequest(StrictBody):
    filters: list[FilterWrite]


@router.get("/dashboards")
async def list_dashboards(request: Request):
    items = []
    for record in catalog_dashboards(request.app.state.store):
        entry = {"name": record.name, "file": record.path.name}
        if record.error is not None:
            entry.update(title=record.name, valid=False, error=record.error)
        else:
            entry.update(title=record.dashboard.title, etag=record.etag, valid=True)
        items.append(entry)
    return {"dashboards": items}


class CreateRequest(StrictBody):
    title: str
    repo: str | None = None


def _starter_source(request: Request, repo: str | None = None) -> dict[str, Any]:
    layer = request.app.state.layer
    store = request.app.state.store
    if repo is not None:
        store = store.repos[repo]
        layer = layer.layers[repo]
    try:
        metrics_file = layer.metrics_file()
        if metrics_file is not None:
            return _dump_source(metrics_file.source)
    except Exception:
        pass
    for _, dashboard in store.iter_loaded():
        return _dump_source(dashboard.source)
    return {"type": "duckdb", "database": ":memory:"}


def _dump_source(source) -> dict[str, Any]:
    dumped = source_as_project_yaml(source)
    return dumped or {"type": "duckdb", "database": ":memory:"}


@router.post("/dashboards", status_code=201)
async def create_dashboard(request: Request, body: CreateRequest):
    store = request.app.state.store
    title = body.title.strip()
    name = bound_file_stem(dashboard_stem(title))
    if not name:
        raise HTTPException(422, "title must contain at least one letter or number")
    if store.single_file is not None:
        raise HTTPException(
            422, "serving a single file — serve the directory to create new dashboards"
        )
    repos = getattr(store, "repos", None)
    repo = None
    if repos is not None:
        repo = body.repo
        if not repo or repo not in repos:
            known = ", ".join(sorted(repos))
            raise HTTPException(422, f"pick which repo to create in — one of: {known}")
        name = f"{repo}/{name}"
    # A file written by another tool can hold the same name in decomposed form.
    # It looks identical in the index. macOS resolves both forms to one file, so
    # save_text already refuses there; a normalization-sensitive filesystem would
    # create a second dashboard that looks exactly like the first.
    taken = {unicodedata.normalize("NFC", existing) for existing in store.discover()}
    if unicodedata.normalize("NFC", name) in taken:
        raise ConflictError(f"a dashboard named '{name}' already exists")
    text = build_dashboard_text(title, _starter_source(request, repo))
    etag = store.save_text(name, text)
    return {"name": name, "etag": etag}


@router.get("/dashboards/{name:dname}")
async def get_dashboard(request: Request, name: str):
    # The raw file text is deliberately not returned. `client_payload` redacts
    # `source`/`sources` through `redact_source`, and shipping the file
    # alongside it handed back every literal credential the redaction had just
    # removed. Nothing reads it: both browser callers of this endpoint take
    # `etag` only, and the whole-file `PUT` is driven from a file the caller
    # already holds.
    dashboard, _text, etag = request.app.state.store.load(name)
    payload = client_payload(name, dashboard, etag, request.app.state.layer)
    return payload


@router.patch("/dashboards/{name:dname}/positions")
async def update_positions(
    request: Request,
    name: str,
    body: PositionsRequest,
    if_match: str = Header(...),
):
    positions = {wid: pos.model_dump() for wid, pos in body.positions.items()}
    etag = request.app.state.store.update_positions(name, positions, if_match)
    return {"name": name, "etag": etag}


@router.patch("/dashboards/{name:dname}/meta")
async def update_meta(
    request: Request,
    name: str,
    body: MetaRequest,
    if_match: str = Header(...),
):
    etag = request.app.state.store.update_meta(
        name, if_match, title=body.title, description=body.description
    )
    return {"name": name, "etag": etag}


@router.put("/dashboards/{name:dname}/filters")
async def update_filters(
    request: Request,
    name: str,
    body: FiltersRequest,
    if_match: str = Header(...),
):
    existing = {}
    try:
        current, _, _ = request.app.state.store.load(name)
        existing = {f.name: f for f in current.filters}
    except Exception:
        pass
    filters = []
    for f in body.filters:
        if (
            f.options_sql is None
            and f.options is None
            and f.name in existing
            and existing[f.name].options_sql is not None
        ):
            f.options_sql = existing[f.name].options_sql
        item: dict[str, Any] = {"name": f.name, "type": f.type}
        if f.label:
            item["label"] = f.label
        if f.default is not None:
            item["default"] = f.default
        if f.options:
            item["options"] = f.options
        if f.options_sql:
            item["options_sql"] = f.options_sql
        if f.type == "daterange" and f.bind:
            item["bind"] = f.bind
        filters.append(item)
    etag = request.app.state.store.update_filters(name, filters, if_match)
    return {"name": name, "etag": etag}


def _tile_identities(dashboard: Dashboard) -> list[dict[str, str | None]]:
    return [{"id": w.id, "query": w.query} for w in dashboard.tiles]


def _copy_named_source(
    request: Request, name: str, tile: dict[str, Any], dashboard: Dashboard
) -> tuple[str, Any] | None:
    source_key = tile.get("source") or None
    if not source_key:
        return None
    store = request.app.state.store
    named_here = dict(dashboard.sources)
    if dashboard.default_source_name:
        named_here[dashboard.default_source_name] = dashboard.source
    if source_key in named_here:
        return None
    try:
        src, src_base = resolve_picker_source(
            store, request.app.state.layer, name, source_key, dashboard=dashboard
        )
    except KeyError as exc:
        known = exc.args[1] if len(exc.args) > 1 else []
        listed = ", ".join(known) or "(none defined)"
        raise HTTPException(
            404, f"no source named '{source_key}' — known sources: {listed}"
        ) from exc
    dest_base = store.path_for(name).parent
    copied = source_for_copy(src, src_base, dest_base)
    sname = alias_for_picker_key(source_key, named_here, source=copied)
    tile["source"] = sname
    return None if sname in named_here else (sname, copied)


def _write_tile(
    request: Request,
    name: str,
    tile: dict[str, Any],
    sql: str | None,
    if_match: str,
) -> dict[str, Any]:
    store = request.app.state.store
    dashboard, _, _ = store.load(name)
    named_source = _copy_named_source(request, name, tile, dashboard)
    etag = store.upsert_tile(name, tile, sql, if_match, named_source=named_source)
    dashboard, _, _ = store.load(name)
    return {"name": name, "etag": etag, "tiles": _tile_identities(dashboard)}


@router.post("/dashboards/{name:dname}/tiles")
async def create_tile(
    request: Request,
    name: str,
    body: TileRequest,
    if_match: str = Header(...),
):
    store = request.app.state.store
    dashboard, _, _ = store.load(name)
    tile = dict(body.tile)
    tile["id"] = tile.get("id") or slugify(str(tile.get("title") or ""))
    if not tile["id"]:
        raise HTTPException(422, "tile id is required")
    if tile["id"] in {t.id for t in dashboard.tiles}:
        raise HTTPException(
            409,
            {
                "message": f"tile '{tile['id']}' already exists",
                "tiles": _tile_identities(dashboard),
            },
        )
    return _write_tile(request, name, tile, body.sql, if_match)


@router.put("/dashboards/{name:dname}/tiles/{tile_id}")
async def put_tile(
    request: Request,
    name: str,
    tile_id: str,
    body: TileRequest,
    if_match: str = Header(...),
):
    store = request.app.state.store
    dashboard, _, _ = store.load(name)
    if tile_id not in {t.id for t in dashboard.tiles}:
        raise HTTPException(
            404,
            {
                "message": f"unknown tile '{tile_id}'",
                "tiles": _tile_identities(dashboard),
            },
        )
    tile = dict(body.tile)
    tile["id"] = tile_id
    return _write_tile(request, name, tile, body.sql, if_match)


@router.delete("/dashboards/{name:dname}/tiles/{tile_id}")
async def delete_tile(
    request: Request,
    name: str,
    tile_id: str,
    if_match: str = Header(...),
):
    etag = request.app.state.store.delete_tile(name, tile_id, if_match)
    # Derived ids are positional: removing a tile renumbers `tile_N` and promotes
    # a deduped `q_2` to `q`. A client holding ids across the delete is therefore
    # holding ids the server no longer agrees with, and the next delete lands on
    # the wrong tile — silently, since the stale id still resolves. Hand back the
    # ids that are now true so the caller can resync.
    dashboard, _, _ = request.app.state.store.load(name)
    # Both identities, because both are derived. A tile with inline `sql:` has
    # its query hoisted under its own derived id (see Dashboard.assign_tile_ids),
    # so the query name moves when the id does — while an authored `query:` name
    # never moves. The client cannot tell those apart by looking, and guessing
    # renames a query the file still calls something else.
    return {"name": name, "etag": etag, "tiles": _tile_identities(dashboard)}


@router.put("/dashboards/{name:dname}")
async def save_dashboard(
    request: Request,
    name: str,
    body: SaveRequest,
    if_match: str = Header(...),
):
    etag = request.app.state.store.save_text(name, body.text, if_match)
    return {"name": name, "etag": etag}


@router.delete("/dashboards/{name:dname}", status_code=204)
async def delete_dashboard(request: Request, name: str, if_match: str = Header(...)):
    request.app.state.store._check_etag(request.app.state.store.path_for(name), if_match)
    request.app.state.store.delete(name)
