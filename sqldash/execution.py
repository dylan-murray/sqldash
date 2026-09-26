"""Async query execution: submit to a threadpool, poll by id, cancel any time.

Results live only in an in-memory LRU sized by count and estimated bytes,
never on disk. The cache exists solely to serve polling and CSV download and
dies with the process.
"""

import hashlib
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqldash.connectors.base import (
    CancelToken,
    ConnectionBusy,
    ConnectionLost,
    Connector,
    ConnectorError,
)
from sqldash.connectors.engine import (
    EngineConnector,
    SharedFileConflict,
    dirs_within,
    duckdb_allowed_dirs,
)
from sqldash.connectors.engine_urls import FILE_DATABASES
from sqldash.models.results import Execution, ExecutionStatus, QueryResult
from sqldash.project.sources import source_files_dir
from sqldash.secrets import interpolate_env

MAX_CACHED = 200
MAX_CACHE_BYTES = 256 * 1024 * 1024
UNFETCHED_GRACE_SECONDS = 600.0


def _approx_bytes(result: QueryResult) -> int:
    """Estimate result size from a 100-row sample, for cache accounting."""
    if not result.rows:
        return 256
    sample = result.rows[: min(100, len(result.rows))]
    sample_bytes = sum(sum(len(str(cell)) + 16 for cell in row) + 24 for row in sample)
    return int(sample_bytes / len(sample) * len(result.rows)) + 256


@dataclass
class _ExecutionState:
    id: str
    status: ExecutionStatus = "queued"
    error: str | None = None
    result: QueryResult | None = None
    result_bytes: int = 0
    cancel_token: CancelToken = field(default_factory=CancelToken)
    finished_at: float | None = None
    fetched: bool = False

    def awaiting_fetch(self, now: float) -> bool:
        """Finished, never read back, and still inside its grace period."""
        finished_at = self.finished_at if self.finished_at is not None else now
        return not self.fetched and now - finished_at < UNFETCHED_GRACE_SECONDS

    def to_model(self) -> Execution:
        return Execution(id=self.id, status=self.status, error=self.error, result=self.result)


def _file_database_identity(source, base_dir: Path | None) -> tuple[str, str] | None:
    """What makes two file-database configs the same source: the project dir and
    the database file. None for anything else, where a retired engine holds no
    process-wide resource and the cache can keep it."""
    if source.type not in FILE_DATABASES:
        return None
    scan_dir = source_files_dir(source, Path(base_dir)) if base_dir else Path(".")
    try:
        database = interpolate_env(source.database) or ":memory:"
    except Exception:
        database = ":memory:"
    if database in (":memory:", "") or database.startswith("file:"):
        return None
    try:
        resolved = str((scan_dir / database).resolve())
    except OSError:
        resolved = str(scan_dir / database)
    return (str(base_dir), resolved)


def _confined_dirs(source, base_dir: Path | None) -> tuple[str, ...] | None:
    """The reach a duckdb source asks for: None when it opted out of confinement."""
    if source.external_access:
        return None
    scan_dir = source_files_dir(source, Path(base_dir)) if base_dir else Path(".")
    return tuple(duckdb_allowed_dirs(source, base_dir, scan_dir))


def _reach_is_within(wanted: tuple[str, ...] | None, held: tuple[str, ...] | None) -> bool:
    """Whether `wanted` asks for no more file access than `held` already has."""
    if held is None:
        return True
    if wanted is None:
        return False
    return dirs_within(wanted, held)


class ExecutionRegistry:
    """Engines pool and recover their own connections (SQLAlchemy QueuePool +
    pool_pre_ping); this registry only caches one connector per source and runs
    executions on a threadpool.

    ``still_declared(source, base_dir)`` says whether any dashboard on disk
    still declares that source. The registry alone cannot tell an edited source
    from a second dashboard on the same file; a server that can answer this lets
    an edit widen a file's reach without a restart (#642). Without it, widening
    is always refused.
    """

    def __init__(
        self,
        max_workers: int = 8,
        still_declared: Callable[[Any, Path], bool] | None = None,
    ) -> None:
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="sqldash-exec")
        self._executions: OrderedDict[str, _ExecutionState] = OrderedDict()
        self._connectors: dict[str, Connector] = {}
        self._lock = threading.Lock()
        self._still_declared = still_declared

    def submit(self, source, base_dir: Path, sql: str, bind: Any, row_limit: int) -> Execution:
        """Queue the statement on the worker pool and return a pollable handle immediately."""
        if row_limit < 0:
            raise ConnectorError("row_limit must be >= 0")
        state = _ExecutionState(id=uuid.uuid4().hex[:12])
        with self._lock:
            self._executions[state.id] = state
            self._evict_locked()
        self._pool.submit(self._run, state, source, base_dir, sql, bind, row_limit)
        return state.to_model()

    def _evict_locked(self) -> None:
        """Drop the least recently read finished executions until the budgets fit.

        The count budget never evicts a result nobody has fetched yet (inside its
        grace period), or a dashboard with more tiles than MAX_CACHED loses its
        first tiles before the browser polls them. The byte budget is the memory
        bound, so once only unfetched results are left it takes those too,
        oldest first.
        """
        now = time.monotonic()
        total_bytes = sum(s.result_bytes for s in self._executions.values())
        count = len(self._executions)
        for keep_unfetched in (True, False):
            for execution_id in list(self._executions):
                over_count = keep_unfetched and count > MAX_CACHED
                if not over_count and total_bytes <= MAX_CACHE_BYTES:
                    break
                state = self._executions[execution_id]
                if state.status in ("queued", "running"):
                    continue
                if keep_unfetched and state.awaiting_fetch(now):
                    continue
                self._executions.pop(execution_id)
                total_bytes -= state.result_bytes
                count -= 1

    def _run(
        self, state: _ExecutionState, source, base_dir: Path, sql: str, bind: Any, row_limit: int
    ) -> None:
        if state.cancel_token.cancelled:
            state.status = "cancelled"
            state.finished_at = time.monotonic()
            return
        state.status = "running"
        try:
            try:
                state.result = self._execute(
                    self._get_connector(source, base_dir), sql, bind, row_limit, state.cancel_token
                )
            except SharedFileConflict:
                if not self._make_way(source, base_dir):
                    raise
                state.result = self._execute(
                    self._get_connector(source, base_dir), sql, bind, row_limit, state.cancel_token
                )
            if state.result is not None:
                state.result_bytes = _approx_bytes(state.result)
            state.status = "done"
        except ConnectorError as exc:
            if state.cancel_token.cancelled or str(exc) == "query cancelled":
                state.status = "cancelled"
            else:
                state.status = "error"
                state.error = str(exc)
        except Exception as exc:
            state.status = "error"
            state.error = f"{type(exc).__name__}: {exc}"
        finally:
            state.finished_at = time.monotonic()
            with self._lock:
                self._evict_locked()

    @staticmethod
    def _execute(
        connector: Connector, sql: str, bind: Any, row_limit: int, cancel_token: CancelToken
    ) -> QueryResult:
        for attempt in (1, 2):
            try:
                return connector.execute(sql, bind, row_limit, cancel_token)
            except (ConnectionBusy, ConnectionLost):
                if attempt == 2 or cancel_token.cancelled:
                    raise
                time.sleep(1.0)
        raise AssertionError("unreachable")

    @staticmethod
    def _key(source, base_dir: Path) -> str:
        return hashlib.sha256((source.model_dump_json() + str(base_dir)).encode()).hexdigest()

    def _make_way(self, source, base_dir: Path) -> bool:
        """Retire what holds a refused source's file when nothing declares it any more.

        A cache hit never reaches `_drop_superseded_locked`, so a source whose
        connector was cached while it was refused (a dashboard that declared a
        confined and an external source on one file, then was edited down to the
        external one) stayed refused until restart (#642). The refused connector
        goes too: its engine counts as a holder of the file's claim, and the claim
        is only released when no engine holds it.
        """
        key = self._key(source, base_dir)
        with self._lock:
            if not self._drop_superseded_locked(source, base_dir, key):
                return False
            refused = self._connectors.pop(key, None)
            if refused is not None:
                with suppress(Exception):
                    refused.close()
        return True

    def _get_connector(self, source, base_dir: Path) -> Connector:
        key = self._key(source, base_dir)
        with self._lock:
            connector = self._connectors.get(key)
            if connector is None:
                self._drop_superseded_locked(source, base_dir, key)
                connector = EngineConnector(source, base_dir)
                connector.connect()
                self._connectors[key] = connector
            return connector

    def _drop_superseded_locked(self, source, base_dir: Path, key: str) -> bool:
        """Close the connector an edited file-database source just replaced, but
        never when that would widen the file's reach.

        The cache is keyed on the source's config, so editing one adds an entry
        and the old engine lives until shutdown, holding the file's confinement
        claim (#639 review). Retiring it lets the edit take effect. The registry
        cannot tell an edit from a second dashboard on the same file, though, and
        both look like two configs on one database: retiring the incumbent for
        *any* of them let a source with `external_access: true` dispose a
        confined one and reopen the file unrestricted, so confinement was off
        for whoever asked last (#641). Two dashboards took turns, and nothing was
        refused.

        So a replacement is served only when it asks for no more than the
        incumbent already had. Widening, including any move to
        `external_access: true`, leaves the incumbent in place and is refused by
        the connection handler. Restarting to widen a security boundary is a fair
        price; silently widening it is not.

        The exception is an incumbent no dashboard declares any more: then the
        replacement is an edit, not a second dashboard, and nothing is left to
        keep confined (#642). This decides only who holds the file. Every engine
        is still given exactly the reach it asked for or refused, so a wrong
        answer from `still_declared` costs availability, never confinement.
        """
        identity = _file_database_identity(source, base_dir)
        if identity is None:
            return False
        wanted = _confined_dirs(source, base_dir)
        retired = False
        for other_key, other in list(self._connectors.items()):
            if (
                other_key == key
                or _file_database_identity(other.source, other.base_dir) != identity
            ):
                continue
            if not _reach_is_within(
                wanted, _confined_dirs(other.source, other.base_dir)
            ) and self._declared(other.source, other.base_dir):
                continue
            del self._connectors[other_key]
            retired = True
            with suppress(Exception):
                other.close()
        return retired

    def _declared(self, source, base_dir: Path) -> bool:
        if self._still_declared is None:
            return True
        try:
            return bool(self._still_declared(source, base_dir))
        except Exception:
            return True

    @contextmanager
    def connection(self, source, base_dir: Path):
        """Hand out the cached connector for ad-hoc use (introspection, probes)."""
        yield self._get_connector(source, base_dir)

    def run_sync(
        self,
        source,
        base_dir: Path,
        sql: str,
        bind: Any,
        row_limit: int,
        timeout: float | None = 300.0,
    ) -> QueryResult:
        """Submit and poll to completion; on timeout the execution is cancelled before raising."""
        if row_limit < 0:
            raise ConnectorError("row_limit must be >= 0")
        execution = self.submit(source, base_dir, sql, bind, row_limit)
        deadline = time.monotonic() + timeout if timeout is not None else float("inf")
        while time.monotonic() < deadline:
            state = self.get(execution.id)
            if state is None:
                raise ConnectorError("execution expired")
            if state.status == "done":
                assert state.result is not None
                return state.result
            if state.status in ("error", "cancelled"):
                raise ConnectorError(state.error or state.status)
            time.sleep(0.05)
        self.cancel(execution.id)
        raise ConnectorError(f"query timed out after {timeout:.0f}s")

    def run_bound(self, bound, row_limit: int, timeout: float | None = 300.0) -> QueryResult:
        """run_sync for a BoundQuery, so callers stop unpacking the same four fields."""
        return self.run_sync(
            bound.source, bound.base_dir, bound.sql, bound.bind, row_limit, timeout=timeout
        )

    def get(self, execution_id: str) -> Execution | None:
        """Read an execution and mark it recently used; a finished read counts as fetched."""
        with self._lock:
            state = self._executions.get(execution_id)
            if state is None:
                return None
            self._executions.move_to_end(execution_id)
            execution = state.to_model()
            if execution.status not in ("queued", "running"):
                state.fetched = True
        return execution

    def cancel(self, execution_id: str) -> Execution | None:
        with self._lock:
            state = self._executions.get(execution_id)
        if state is None:
            return None
        state.cancel_token.cancel()
        return state.to_model()

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
        with self._lock:
            connectors = list(self._connectors.values())
            self._connectors.clear()
        for connector in connectors:
            with suppress(Exception):
                connector.close()
