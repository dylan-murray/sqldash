"""The contract between the execution layer and database drivers.

Connectors are called from worker threads and must tolerate concurrent
``execute`` calls. Cancellation is out-of-band by necessity: most DBAPI
drivers block inside ``execute()`` and cannot be interrupted from the same
thread, so a driver-specific canceller must be attached to the
:class:`CancelToken` *before* the blocking call begins.

Error taxonomy, as read by the retry layer (ExecutionRegistry): plain
:class:`ConnectorError` is terminal and surfaced to the caller;
:class:`ConnectionBusy` and :class:`ConnectionLost` each earn one retry —
busy because a pooled connection may free up, lost because the engine has
already invalidated the dead connection and a retry gets a fresh one.
"""

import contextlib
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.engine.url import URL, make_url
from sqlalchemy.exc import ArgumentError

from sqldash.connectors.wire import to_cell
from sqldash.models.results import QueryResult, ResultColumn
from sqldash.models.source import scrub_error_text
from sqldash.secrets import interpolate_env


class ConnectorError(RuntimeError):
    """Query or connection failure with a user-presentable message; terminal unless subclassed."""


def source_url(source) -> URL:
    """Parse a source's resolved ``url:``; a URL sqlalchemy rejects is a ConnectorError."""
    try:
        return make_url(interpolate_env(source.url))
    except (ArgumentError, ValueError) as exc:
        message = scrub_error_text(str(exc), source)
        raise ConnectorError(f"cannot resolve dialect for source: {message}") from exc


class ConnectionBusy(ConnectorError):
    """No pooled connection became free in time; worth one retry after a short pause."""


class ConnectionLost(ConnectorError):
    """The connection died mid-query and was invalidated; a retry gets a fresh one."""


class CancelToken:
    """Thread-safe cancellation handoff between the API and a query worker.

    If cancellation fires before the worker attaches its canceller,
    :meth:`attach` invokes it immediately — the race between "cancel
    requested" and "query started" can never drop a cancellation.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._on_cancel = None

    def attach(self, canceller) -> None:
        """Register the query's canceller (None to detach); fires it if already cancelled."""
        self._on_cancel = canceller
        if canceller is not None and self._event.is_set():
            canceller()

    def cancel(self) -> None:
        """Mark cancelled and invoke the attached canceller, swallowing its errors."""
        self._event.set()
        canceller = self._on_cancel
        if canceller is not None:
            with contextlib.suppress(Exception):
                canceller()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float) -> bool:
        """Sleep up to ``timeout`` seconds, returning early (True) once cancelled."""
        return self._event.wait(timeout)


@dataclass
class TableInfo:
    """One table's introspected structure; columns are (name, type) pairs."""

    name: str
    schema: str | None = None
    columns: list[tuple[str, str]] = field(default_factory=list)


class Connector(ABC):
    """What a database driver must provide to run sqldash queries.

    Implementations are shared across worker threads, so every method must be
    safe to call concurrently. ``execute`` must attach a canceller to its
    token before issuing the blocking driver call and detach it on the way
    out; failures are raised as :class:`ConnectorError` (or the retryable
    subclasses) with messages fit to show a user.
    """

    @abstractmethod
    def connect(self) -> None:
        """Prepare the underlying engine; called once when the connector enters the cache."""

    @abstractmethod
    def execute(
        self, sql: str, bind: list[Any], row_limit: int, cancel_token: CancelToken
    ) -> QueryResult:
        """Run one statement and return at most ``row_limit`` rows, honouring cancellation."""

    @abstractmethod
    def introspect(self) -> list[TableInfo]:
        """Describe tables and columns — structure only, never data rows."""

    @abstractmethod
    def close(self) -> None:
        """Dispose pooled connections; the connector may be used again afterwards."""

    def build_result(
        self,
        columns: list[ResultColumn],
        raw_rows: list[tuple],
        row_limit: int,
        started_at: float,
    ) -> QueryResult:
        """Truncate to ``row_limit``, convert cells to JSON-safe values, and stamp timing."""
        truncated = len(raw_rows) > row_limit
        types = [c.type for c in columns]
        rows = [
            [to_cell(v, t) for v, t in zip(row, types, strict=True)] for row in raw_rows[:row_limit]
        ]
        return QueryResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            elapsed_ms=(time.monotonic() - started_at) * 1000,
        )
