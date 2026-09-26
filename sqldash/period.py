"""Period comparison windows. Same math as `static/js/period.js` so headless
`--compare` matches the tile delta."""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

from sqldash.params import ParamError

COMPARE_MODES = ("previous_period", "yoy")


def shift_iso(iso: str, *, days: int = 0, years: int = 0) -> str:
    """Shift a YYYY-MM-DD. YoY clamps Feb 29 to the prior Feb 28 — JS
    `setUTCFullYear` overflows to Mar 1 and the browser then `setUTCDate(0)`."""
    current = date.fromisoformat(str(iso)[:10])
    if years:
        month = current.month
        try:
            current = current.replace(year=current.year - years)
        except ValueError:
            nxt = date(
                current.year - years + (1 if month == 12 else 0),
                1 if month == 12 else month + 1,
                1,
            )
            current = nxt - timedelta(days=1)
        if current.month != month:
            current = current.replace(day=1) - timedelta(days=1)
    if days:
        current = current - timedelta(days=days)
    return current.isoformat()


def compare_window(mode: str, start: str | None, end: str | None) -> dict[str, str] | None:
    """The prior window, or None without a full range. A bound the warehouse
    takes but Python's calendar cannot shift is a ParamError, not a traceback
    or an MCP protocol error. Both halves matter: year 0000 raises ValueError
    from the date constructor, and a window whose prior period crosses below
    year 1 raises OverflowError from the shift, which is not an MCP domain
    error either."""
    if not start or not end:
        return None
    start_s, end_s = str(start)[:10], str(end)[:10]
    try:
        return _compare_window(mode, start_s, end_s)
    except (ValueError, OverflowError) as exc:
        raise ParamError(
            f"compare '{mode}' cannot shift the window {start!r}..{end!r} — {exc}"
        ) from exc


def _compare_window(mode: str, start_s: str, end_s: str) -> dict[str, str]:
    if mode == "yoy":
        return {
            "start": shift_iso(start_s, years=1),
            "end": shift_iso(end_s, years=1),
            "label": "last year",
        }
    span = (date.fromisoformat(end_s) - date.fromisoformat(start_s)).days + 1
    return {
        "start": shift_iso(start_s, days=span),
        "end": shift_iso(end_s, days=span),
        "label": "previous period",
    }


def delta_from_results(
    current, previous, *, grain: str | None = None, dimensions=None
) -> dict[str, Any] | None:
    """Big-number shape only: one ungrouped row, one numeric column.

    Grouped or grained results have no single pct — the browser draws a dashed
    series, it does not invent a delta from row 0. A numeric dimension as the
    first column would otherwise report 0% while the measure moved.
    """
    if grain or dimensions:
        return None
    if len(current.rows) != 1 or len(previous.rows) != 1:
        return None
    numeric = [
        i for i, col in enumerate(current.columns) if col.type in ("integer", "float", "decimal")
    ]
    if len(numeric) != 1:
        return None
    idx = numeric[0]
    if idx >= len(previous.rows[0]):
        return None
    cur = current.rows[0][idx]
    prev = previous.rows[0][idx]
    if cur is None or prev is None:
        return None
    cur_n, prev_n = float(cur), float(prev)
    if prev_n == 0 or not math.isfinite(cur_n) or not math.isfinite(prev_n):
        return None
    return {
        "current": cur_n,
        "previous": prev_n,
        "pct": (cur_n - prev_n) / abs(prev_n),
    }
