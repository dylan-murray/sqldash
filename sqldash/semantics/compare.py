"""The second window of a `compare:` metric, shared by every headless surface.

The browser does this in period.js; the CLI and MCP used to each carry a copy of
the same steps (shift the window, bind again, run, diff). One copy here means a
change to the window math or the delta shape reaches every surface at once.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqldash.params import ParamError
from sqldash.period import compare_window, delta_from_results
from sqldash.semantics.bind import BoundQuery


@dataclass(frozen=True)
class CompareResult:
    """The prior window, shaped once here for every surface.

    ``payload`` is the only way out, so a surface says what it wants (rows as
    dicts or lists, SQL or not) instead of deleting keys from a dict.

    ``truncated`` is the prior window's own flag: the second run hits the same
    row cap as the first and nothing else reports it, so a clipped prior window
    would otherwise arrive as rows with nothing said about them.
    """

    mode: str
    label: str
    start: Any
    end: Any
    sql: str
    columns: list[str]
    rows: list[list[Any]]
    truncated: bool
    delta: dict[str, Any] | None

    def payload(self, *, rows_as_dicts: bool = False, include_sql: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = {
            "mode": self.mode,
            "label": self.label,
            "window": {"start": self.start, "end": self.end},
        }
        if include_sql:
            out["sql"] = self.sql
        out["rows"] = (
            [dict(zip(self.columns, row, strict=True)) for row in self.rows]
            if rows_as_dicts
            else self.rows
        )
        out["truncated"] = self.truncated
        out["delta"] = self.delta
        return out


def compare_metric(
    mode: str,
    bound: BoundQuery,
    current,
    *,
    rebind: Callable[[Any, Any], BoundQuery],
    run: Callable[[BoundQuery], Any],
    grain: str | None = None,
    dimensions=None,
    dash=None,
) -> CompareResult:
    """Run the prior window for ``bound`` and package it next to ``current``.

    ``rebind(start, end)`` must bind the same metric with only the window moved;
    ``run(bound)`` executes it. Raises ParamError when the current query has no
    time range to shift, since a compare with nothing to compare against would
    otherwise return a confident 0%. ``dash`` is the dashboard the query was
    scoped to, so the error names what that dashboard is missing.
    """
    start = end = None
    if bound.time_range:
        start, end = bound.time_range
    window = compare_window(mode, start, end)
    if window is None:
        windowed = bound.resolved is not None and bound.resolved.definition.window
        if windowed and end and not start:
            raise ParamError(
                f"compare '{mode}' cannot shift a grainless windowed metric — it keeps "
                "only the daterange end, so there is no range to shift. Query with a "
                "grain, or omit compare"
            )
        raise ParamError(_no_range_message(mode, bound, dash))
    previous_bound = rebind(window["start"], window["end"])
    previous = run(previous_bound)
    return CompareResult(
        mode=mode,
        label=window["label"],
        start=window["start"],
        end=window["end"],
        sql=previous_bound.sql,
        columns=[c.name for c in previous.columns],
        rows=[list(row) for row in previous.rows],
        truncated=previous.truncated,
        delta=delta_from_results(current, previous, grain=grain, dimensions=dimensions),
    )


def _no_range_message(mode: str, bound: BoundQuery, dash) -> str:
    resolved = bound.resolved
    if resolved is not None and resolved.definition.time_dimension is None:
        return (
            f"compare '{mode}' needs a time range — metric '{resolved.name}' has no "
            "time_dimension, so there is no window to shift"
        )
    if bound.time_range is not None:
        given, missing = (
            ("a start", "an end, e.g. 'today'")
            if bound.time_range[0]
            else ("an end", "a start, e.g. '-30d'")
        )
        return (
            f"compare '{mode}' needs both a start and an end, got only {given}: pass "
            f"{missing}. A range open on one side has no length to shift"
        )
    if dash is None:
        return (
            f"compare '{mode}' needs a time range — pass start/end or run inside a "
            "dashboard with a daterange filter"
        )
    daterange = next((f for f in dash.filters if f.type == "daterange"), None)
    if daterange is None:
        return (
            f"compare '{mode}' needs a time range — the dashboard has no daterange "
            "filter, so pass start/end or add one"
        )
    return (
        f"compare '{mode}' needs a time range — daterange filter '{daterange.name}' "
        "has no value, so pass start/end or give it a default"
    )
