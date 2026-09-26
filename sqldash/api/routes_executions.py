import csv
import io
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import Field, model_validator

from sqldash.api.helpers import StrictBody
from sqldash.connectors.engine import paramstyle_for
from sqldash.connectors.wire import flat_text, for_browser
from sqldash.csv_safe import spreadsheet_safe
from sqldash.models.results import Execution
from sqldash.params import ParamError, bind_sql, extract_params
from sqldash.project.sources import resolve_picker_source
from sqldash.semantics.bind import bind_metric, bind_named_query, dashboard_param_names
from sqldash.sqlguard import read_only_violation

router = APIRouter(prefix="/api")

FILTER_OPTIONS_ROW_CAP = 500
CSV_FLUSH_BYTES = 64_000


def truncation_note(row_count: int, width: int = 1) -> list[str]:
    """The trailing note row, padded to the header's width.

    A one-cell last row is a ragged record: pandas pads it, a spreadsheet
    shows it in column A, but a strict importer with a fixed schema
    (`psql \\copy ... CSV HEADER`) refuses the whole file. Padding costs
    nothing and keeps the export loadable everywhere.
    """
    note = (
        f"# truncated: first {row_count:,} rows only, the query returned more; "
        "raise the server row limit (sqldash serve --row-limit) to export the rest"
    )
    return [note] + [""] * max(width - 1, 0)


class RunRequest(StrictBody):
    dashboard: str | None = None
    query: str | None = None
    sql: str | None = None
    metric: str | None = None
    dimensions: list[str] = Field(default_factory=list)
    grain: str | None = None
    start: str | None = None
    end: str | None = None
    filters: dict[str, Any] | None = None
    source: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)
    row_limit: int | None = None
    filter_options: str | None = None

    @model_validator(mode="after")
    def one_run_kind(self):
        kinds = [name for name in ("metric", "query", "sql") if getattr(self, name)]
        if len(kinds) > 1:
            raise ValueError(
                "provide only one of 'metric', 'query', or 'sql' — got " + ", ".join(kinds)
            )
        return self


@router.post("/run", status_code=202, response_model=Execution)
async def run(request: Request, body: RunRequest):
    store = request.app.state.store
    if body.row_limit is None:
        row_limit = request.app.state.row_limit
    elif body.row_limit < 0:
        raise HTTPException(422, "row_limit must be >= 0")
    else:
        row_limit = min(body.row_limit, request.app.state.row_limit)

    if body.dashboard is None:
        if body.metric is None:
            raise HTTPException(422, "provide a 'dashboard', or a 'metric' to run standalone")
        if body.source:
            raise HTTPException(422, "source requires a 'dashboard'")
        if body.params:
            raise HTTPException(
                422,
                "'params' are dashboard filter values and need a 'dashboard'; "
                "to filter a standalone metric use 'filters'",
            )
        bound = bind_metric(
            request.app.state.layer,
            body.metric,
            dimensions=body.dimensions,
            grain=body.grain,
            filters=body.filters,
            start=body.start,
            end=body.end,
        )
        return request.app.state.registry.submit(
            bound.source, bound.base_dir, bound.sql, bound.bind, row_limit
        )

    dashboard, _, _ = store.load(body.dashboard)

    if body.filter_options is not None:
        filter_def = next((f for f in dashboard.filters if f.name == body.filter_options), None)
        if filter_def is None or filter_def.options_sql is None:
            raise HTTPException(
                404, f"no select filter with options_sql named '{body.filter_options}'"
            )
        if extract_params(filter_def.options_sql):
            raise HTTPException(
                422, "options_sql cannot reference {{ params }} — it runs before filters exist"
            )
        source = dashboard.source
        base_dir = store.path_for(body.dashboard).parent
        bound_sql, bind = bind_sql(filter_def.options_sql, {}, paramstyle_for(source))
        return request.app.state.registry.submit(
            source, base_dir, bound_sql, bind, min(FILTER_OPTIONS_ROW_CAP, row_limit)
        )

    source = None
    base_dir = store.path_for(body.dashboard).parent
    picked = body.source
    if picked is None and body.query is not None and body.query in dashboard.queries:
        try:
            picked = dashboard.query_owner_source(body.query)
        except ValueError as exc:
            raise HTTPException(
                422, f"{exc} — pass 'source' to choose ('' is the dashboard default)"
            ) from exc
    if picked:
        try:
            source, base_dir = resolve_picker_source(
                store, request.app.state.layer, body.dashboard, picked, dashboard=dashboard
            )
        except KeyError as exc:
            known = exc.args[1] if len(exc.args) > 1 else []
            named = ", ".join(known) or "(none defined)"
            raise HTTPException(
                404, f"no source named '{picked}' — known sources: {named}"
            ) from exc

    if body.metric is not None:
        caller_named = {
            k: v for k, v in body.params.items() if k not in dashboard_param_names(dashboard)
        }
        bound = bind_metric(
            request.app.state.layer,
            body.metric,
            scope=body.dashboard,
            dash=dashboard,
            params=body.params,
            dimensions=body.dimensions,
            grain=body.grain,
            start=body.start,
            end=body.end,
            filters={**caller_named, **(body.filters or {})} or None,
            source=source,
            base_dir=base_dir if body.source else None,
        )
        return request.app.state.registry.submit(
            bound.source, bound.base_dir, bound.sql, bound.bind, row_limit
        )

    if body.query is not None:
        if body.query not in dashboard.queries:
            raise HTTPException(
                404, f"no query named '{body.query}' in dashboard '{body.dashboard}'"
            )
        sql = dashboard.queries[body.query]
        surface, scan_body = "tile SQL", False
    elif body.sql:
        violation = read_only_violation(body.sql, surface="ad-hoc sql")
        if violation is not None:
            raise HTTPException(422, violation)
        sql = body.sql
        surface, scan_body = "ad-hoc sql", True
    else:
        raise HTTPException(422, "provide one of 'query', 'sql', or 'metric'")

    if (
        body.start is not None
        or body.end is not None
        or body.filters
        or body.grain is not None
        or body.dimensions
    ):
        raise HTTPException(
            422,
            "start/end/filters apply to metrics; this resolved as a query — "
            "pass date params in 'params', or run a metric",
        )

    try:
        bound = bind_named_query(
            dashboard,
            sql,
            params=body.params,
            source=source,
            base_dir=base_dir,
            surface=surface,
            scan_body=scan_body,
        )
    except ParamError as exc:
        raise HTTPException(422, str(exc)) from exc
    return request.app.state.registry.submit(
        bound.source, bound.base_dir, bound.sql, bound.bind, row_limit
    )


@router.get("/executions/{execution_id}", response_model=Execution)
async def get_execution(request: Request, execution_id: str):
    execution = request.app.state.registry.get(execution_id)
    if execution is None:
        raise HTTPException(404, "unknown or expired execution, re-run the query")
    return for_browser(execution)


@router.post("/executions/{execution_id}/cancel", response_model=Execution)
async def cancel_execution(request: Request, execution_id: str):
    execution = request.app.state.registry.cancel(execution_id)
    if execution is None:
        raise HTTPException(404, "unknown or expired execution, re-run the query")
    return for_browser(execution)


@router.get("/executions/{execution_id}/csv")
async def execution_csv(request: Request, execution_id: str):
    execution = request.app.state.registry.get(execution_id)
    if execution is None:
        raise HTTPException(404, "unknown or expired execution, re-run the query")
    if execution.status != "done" or execution.result is None:
        raise HTTPException(409, f"execution is {execution.status}, not done")
    result = execution.result

    def generate():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow([spreadsheet_safe(c.name) for c in result.columns])
        for row in result.rows:
            writer.writerow([spreadsheet_safe(flat_text(value)) for value in row])
            if buffer.tell() > CSV_FLUSH_BYTES:
                yield buffer.getvalue()
                buffer.seek(0)
                buffer.truncate()
        if result.truncated:
            writer.writerow(truncation_note(result.row_count, len(result.columns)))
        yield buffer.getvalue()

    raw = request.query_params.get("name") or execution_id
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-.") or execution_id
    if result.truncated:
        stem += "-partial"
    return StreamingResponse(
        generate(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{stem}.csv"'},
    )
