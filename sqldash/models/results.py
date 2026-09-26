"""Wire contract for query execution: typed results and async execution status."""

from typing import Any, Literal

from pydantic import BaseModel

WireType = Literal[
    "string",
    "integer",
    "float",
    "decimal",
    "boolean",
    "date",
    "timestamp",
    "time",
    "json",
    "binary",
]

ExecutionStatus = Literal["queued", "running", "done", "error", "cancelled"]


class ResultColumn(BaseModel):
    """A result column with its engine-independent wire type ("string" when unknown)."""

    name: str
    type: WireType = "string"


class QueryResult(BaseModel):
    """Rows plus typed columns from one query; `truncated` means the row cap was hit."""

    columns: list[ResultColumn]
    rows: list[list[Any]]
    row_count: int
    truncated: bool = False
    elapsed_ms: float = 0.0


def result_payload(result: QueryResult, row_limit: int) -> dict[str, Any]:
    """One query's rows for a headless caller, carrying the cap that produced them.

    `row_limit` is the cap actually applied, and `truncated` is read off the same
    run, so the two cannot disagree. A caller that cannot see the cap has no way
    to tell a short answer from a complete one, which is how MCP `query_metric`
    handed an agent 100 of 120 daily buckets stamped `truncated: false` (#674):
    its default clipped in the SQL, where the only thing that computes
    `truncated` never looks. Any surface that caps rows applies the cap at the
    fetch boundary and reports it here.
    """
    payload: dict[str, Any] = {
        "columns": [{"name": c.name, "type": c.type} for c in result.columns],
        "rows": result.rows,
        "row_count": result.row_count,
        "truncated": result.truncated,
        "row_limit": row_limit,
    }
    if result.truncated:
        payload["note"] = (
            f"truncated: first {result.row_count:,} rows only, the query returned more; "
            f"the cap that clipped it is row_limit={row_limit}. Raise it where it is set "
            "(a tool's `limit` argument, the tool's own definition, or the server's "
            "--row-limit), or narrow the query"
        )
    return payload


class Execution(BaseModel):
    """A tracked query run: `result` arrives when status is done, `error` when it isn't."""

    id: str
    status: ExecutionStatus = "queued"
    error: str | None = None
    result: QueryResult | None = None
