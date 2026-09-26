import time
from pathlib import Path
from typing import ClassVar

import pytest
from fastapi.testclient import TestClient

import sqldash.execution as ex_mod
from sqldash.connectors.base import CancelToken, Connector, ConnectorError
from sqldash.execution import ExecutionRegistry, _ExecutionState
from sqldash.models.results import QueryResult, ResultColumn
from sqldash.models.source import Source
from sqldash.server import create_app


class SlowConnector(Connector):
    spans: ClassVar[list[tuple[float, float]]] = []
    instances = 0
    calls = 0

    def __init__(self, *args, **kwargs):
        type(self).instances += 1

    def connect(self):
        pass

    def execute(self, sql, bind, row_limit, cancel_token):
        type(self).calls += 1
        start = time.monotonic()
        time.sleep(0.25)
        type(self).spans.append((start, time.monotonic()))
        return QueryResult(columns=[ResultColumn(name="n")], rows=[[1]], row_count=1)

    def introspect(self):
        return []

    def close(self):
        pass


def _patch(monkeypatch, connector_cls):
    connector_cls.spans = []
    connector_cls.instances = 0
    connector_cls.calls = 0
    monkeypatch.setattr(
        "sqldash.execution.EngineConnector", lambda source, base_dir: connector_cls()
    )


def _wait_all(registry, executions, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        states = [registry.get(e.id) for e in executions]
        if all(s.status not in ("queued", "running") for s in states):
            return states
        time.sleep(0.02)
    pytest.fail("executions did not finish")


def test_queries_on_one_source_run_in_parallel(monkeypatch):
    _patch(monkeypatch, SlowConnector)
    registry = ExecutionRegistry(max_workers=8)
    source = Source(type="duckdb")
    try:
        started = time.monotonic()
        executions = [registry.submit(source, Path("."), "SELECT 1", [], 10) for _ in range(4)]
        states = _wait_all(registry, executions)
        wall = time.monotonic() - started
        assert all(s.status == "done" for s in states)
        assert wall < 0.7, f"4×0.25s queries took {wall:.2f}s — they serialized"
        assert SlowConnector.instances == 1
        overlaps = sum(
            1
            for i, (s1, e1) in enumerate(SlowConnector.spans)
            for s2, e2 in SlowConnector.spans[i + 1 :]
            if s1 < e2 and s2 < e1
        )
        assert overlaps >= 3
    finally:
        registry.shutdown()


class FlakyConnector(SlowConnector):
    fail_first = True

    def execute(self, sql, bind, row_limit, cancel_token):
        from sqldash.connectors.base import ConnectionLost

        type(self).calls += 1
        if type(self).fail_first:
            type(self).fail_first = False
            raise ConnectionLost("simulated dead connection")
        return QueryResult(columns=[ResultColumn(name="n")], rows=[[42]], row_count=1)


class AlwaysDeadConnector(SlowConnector):
    def execute(self, sql, bind, row_limit, cancel_token):
        from sqldash.connectors.base import ConnectionLost

        type(self).calls += 1
        raise ConnectionLost("still dead")


class BrokenConnector(SlowConnector):
    def execute(self, sql, bind, row_limit, cancel_token):
        from sqldash.connectors.base import ConnectorError

        type(self).calls += 1
        raise ConnectorError("syntax error: this must NOT retry")


def _run_one(monkeypatch, connector_cls):
    _patch(monkeypatch, connector_cls)
    registry = ExecutionRegistry(max_workers=2)
    try:
        execution = registry.submit(Source(type="duckdb"), Path("."), "SELECT 1", [], 10)
        return _wait_all(registry, [execution])[0]
    finally:
        registry.shutdown()


def test_dead_connection_retried_once(monkeypatch):
    FlakyConnector.fail_first = True
    state = _run_one(monkeypatch, FlakyConnector)
    assert state.status == "done", state.error
    assert state.result.rows == [[42]]
    assert FlakyConnector.calls == 2
    assert FlakyConnector.instances == 1


def test_persistent_connection_loss_surfaces_error(monkeypatch):
    state = _run_one(monkeypatch, AlwaysDeadConnector)
    assert state.status == "error"
    assert "still dead" in state.error
    assert AlwaysDeadConnector.calls == 2


def test_query_errors_do_not_retry(monkeypatch):
    state = _run_one(monkeypatch, BrokenConnector)
    assert state.status == "error"
    assert "must NOT retry" in state.error
    assert BrokenConnector.calls == 1


class CancelledConnector(SlowConnector):
    def execute(self, sql, bind, row_limit, cancel_token):
        from sqldash.connectors.base import ConnectorError

        raise ConnectorError("query cancelled")


class DriverTimeoutWordingConnector(SlowConnector):
    def execute(self, sql, bind, row_limit, cancel_token):
        from sqldash.connectors.base import ConnectorError

        raise ConnectorError("SQL execution was cancelled by the client due to a timeout.")


def test_the_drivers_timeout_wording_is_not_a_user_cancel(monkeypatch):
    state = _run_one(monkeypatch, DriverTimeoutWordingConnector)
    assert state.status == "error", state.status


def test_an_actual_cancel_still_reports_cancelled(monkeypatch):
    state = _run_one(monkeypatch, CancelledConnector)
    assert state.status == "cancelled"


def test_cache_evicts_by_bytes_and_skips_running(monkeypatch):
    import sqldash.execution as ex_mod
    from sqldash.execution import _ExecutionState

    monkeypatch.setattr(ex_mod, "MAX_CACHE_BYTES", 1000)
    registry = ExecutionRegistry(max_workers=2)
    try:
        running = _ExecutionState(id="running1", status="running")
        running.result_bytes = 600
        big_old = _ExecutionState(id="old_done", status="done")
        big_old.result_bytes = 600
        newer = _ExecutionState(id="new_done", status="done")
        newer.result_bytes = 100
        with registry._lock:
            registry._executions["running1"] = running
            registry._executions["old_done"] = big_old
            registry._executions["new_done"] = newer
            registry._evict_locked()
        assert registry.get("running1") is not None
        assert registry.get("old_done") is None
        assert registry.get("new_done") is not None
    finally:
        registry.shutdown()


def test_cache_count_eviction_continues_past_running(monkeypatch):
    import sqldash.execution as ex_mod
    from sqldash.execution import _ExecutionState

    monkeypatch.setattr(ex_mod, "MAX_CACHED", 2)
    registry = ExecutionRegistry(max_workers=2)
    try:
        with registry._lock:
            registry._executions["stuck"] = _ExecutionState(id="stuck", status="running")
            for execution_id in ("a", "b", "c"):
                registry._executions[execution_id] = _ExecutionState(
                    id=execution_id, status="done", fetched=True
                )
            registry._evict_locked()
        assert registry.get("stuck") is not None
        assert registry.get("a") is None
        assert registry.get("b") is None
        assert registry.get("c") is not None
    finally:
        registry.shutdown()


class FastConnector(SlowConnector):
    def execute(self, sql, bind, row_limit, cancel_token):
        return QueryResult(columns=[ResultColumn(name="n")], rows=[[1]], row_count=1)


def _submit_and_finish(registry, count):
    source = Source(type="duckdb")
    executions = [registry.submit(source, Path("."), "SELECT 1", [], 10) for _ in range(count)]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with registry._lock:
            states = [registry._executions.get(e.id) for e in executions]
        if all(s is None or s.status not in ("queued", "running") for s in states):
            return executions
        time.sleep(0.02)
    pytest.fail("executions did not finish")


def test_unfetched_results_survive_past_max_cached(monkeypatch):
    _patch(monkeypatch, FastConnector)
    registry = ExecutionRegistry(max_workers=8)
    try:
        executions = _submit_and_finish(registry, ex_mod.MAX_CACHED + 10)
        states = [registry.get(e.id) for e in executions]
        assert all(s is not None and s.status == "done" for s in states)
    finally:
        registry.shutdown()


def test_fetched_results_are_evicted_past_max_cached(monkeypatch):
    _patch(monkeypatch, FastConnector)
    registry = ExecutionRegistry(max_workers=8)
    try:
        first = _submit_and_finish(registry, ex_mod.MAX_CACHED)
        assert all(registry.get(e.id) is not None for e in first)
        second = _submit_and_finish(registry, 10)
        assert all(registry.get(e.id) is not None for e in second)
        assert sum(registry.get(e.id) is None for e in first) == 10
        with registry._lock:
            assert len(registry._executions) == ex_mod.MAX_CACHED
    finally:
        registry.shutdown()


def test_unfetched_results_expire_after_the_grace_period(monkeypatch):
    monkeypatch.setattr(ex_mod, "UNFETCHED_GRACE_SECONDS", 0.0)
    _patch(monkeypatch, FastConnector)
    registry = ExecutionRegistry(max_workers=8)
    try:
        _submit_and_finish(registry, ex_mod.MAX_CACHED + 10)
        with registry._lock:
            assert len(registry._executions) == ex_mod.MAX_CACHED
    finally:
        registry.shutdown()


def test_get_refreshes_recency(monkeypatch):
    _patch(monkeypatch, FastConnector)
    registry = ExecutionRegistry(max_workers=8)
    try:
        first = _submit_and_finish(registry, ex_mod.MAX_CACHED)
        for execution in first:
            registry.get(execution.id)
        registry.get(first[0].id)
        second = _submit_and_finish(registry, 1)
        registry.get(second[0].id)
        _submit_and_finish(registry, 1)
        assert registry.get(first[0].id) is not None
        assert registry.get(first[1].id) is None
    finally:
        registry.shutdown()


def test_byte_cap_evicts_unfetched_results_when_nothing_else_is_left(monkeypatch):
    monkeypatch.setattr(ex_mod, "MAX_CACHE_BYTES", 1000)
    registry = ExecutionRegistry(max_workers=2)
    try:
        now = time.monotonic()
        with registry._lock:
            for execution_id in ("old", "mid", "new"):
                state = _ExecutionState(id=execution_id, status="done", finished_at=now)
                state.result_bytes = 400
                registry._executions[execution_id] = state
            registry._evict_locked()
        assert registry.get("old") is None
        assert registry.get("mid") is not None
        assert registry.get("new") is not None
    finally:
        registry.shutdown()


def test_byte_cap_evicts_fetched_results_before_unfetched(monkeypatch):
    monkeypatch.setattr(ex_mod, "MAX_CACHE_BYTES", 1000)
    registry = ExecutionRegistry(max_workers=2)
    try:
        now = time.monotonic()
        with registry._lock:
            unread = _ExecutionState(id="unread", status="done", finished_at=now)
            unread.result_bytes = 600
            read = _ExecutionState(id="read", status="done", finished_at=now, fetched=True)
            read.result_bytes = 600
            registry._executions["unread"] = unread
            registry._executions["read"] = read
            registry._evict_locked()
        assert registry.get("unread") is not None
        assert registry.get("read") is None
    finally:
        registry.shutdown()


def test_csv_of_a_recently_read_result_survives_newer_executions(tmp_path):
    (tmp_path / "d.yaml").write_text(
        "title: D\nsource: {type: duckdb, database: ':memory:'}\n"
        "tiles: [{title: W, sql: 'SELECT 1'}]\n"
    )
    app = create_app(tmp_path, allowed_hosts=["testserver"])
    with TestClient(app) as client:
        registry = app.state.registry
        older = _submit_and_finish(registry, 1)[0]
        assert client.get(f"/api/executions/{older.id}").json()["status"] == "done"
        for _ in range(3):
            for execution in _submit_and_finish(registry, ex_mod.MAX_CACHED // 2):
                registry.get(execution.id)
            res = client.get(f"/api/executions/{older.id}/csv")
            assert res.status_code == 200
            assert res.text.splitlines()[0] == "1"


def test_approx_bytes_scales_with_rows():
    from sqldash.execution import _approx_bytes

    small = QueryResult(columns=[ResultColumn(name="v")], rows=[["x" * 10]] * 10, row_count=10)
    big = QueryResult(
        columns=[ResultColumn(name="v")], rows=[["x" * 10]] * 10_000, row_count=10_000
    )
    assert _approx_bytes(big) > _approx_bytes(small) * 500


def test_real_duckdb_tiles_parallel(tmp_path):
    from sqldash.scaffold import create_demo

    create_demo(tmp_path)
    registry = ExecutionRegistry(max_workers=8)
    source = Source(type="duckdb", attach_files=True)
    base = tmp_path / ".sqldash"
    try:
        executions = [
            registry.submit(source, base, "SELECT COUNT(*) FROM orders", [], 10) for _ in range(4)
        ]
        states = _wait_all(registry, executions)
        assert all(s.status == "done" for s in states)
    finally:
        registry.shutdown()


def test_pool_exhaustion_is_classified_and_friendly(tmp_path, monkeypatch):
    import sqldash.connectors.engine as engine_mod
    from sqldash.connectors.base import ConnectionBusy
    from sqldash.connectors.engine import EngineConnector

    monkeypatch.setattr(engine_mod, "POOL_SIZE", 1)
    monkeypatch.setattr(engine_mod, "POOL_TIMEOUT", 0.2)
    monkeypatch.setattr(engine_mod, "MAX_OVERFLOW", 0)
    import duckdb

    duckdb.connect(str(tmp_path / "t.duckdb")).close()
    source = Source(type="duckdb", database=str(tmp_path / "t.duckdb"))
    connector = EngineConnector(source, tmp_path)
    connector.connect()
    try:
        held = connector.engine.connect()
        try:
            with pytest.raises(ConnectionBusy, match="pooled connections are busy"):
                connector.execute("SELECT 1", [], 10, CancelToken())
        finally:
            held.close()
        result = connector.execute("SELECT 1", [], 10, CancelToken())
        assert result.rows == [[1]]
    finally:
        connector.close()


def test_registry_retries_once_on_busy(monkeypatch, tmp_path):
    from sqldash.connectors.base import ConnectionBusy

    class BusyOnceConnector(SlowConnector):
        busy_calls = 0

        def execute(self, sql, bind, row_limit, cancel_token):
            type(self).busy_calls += 1
            if type(self).busy_calls == 1:
                raise ConnectionBusy("all pooled connections are busy")
            return QueryResult(columns=[ResultColumn(name="n")], rows=[[1]], row_count=1)

    BusyOnceConnector.busy_calls = 0
    _patch(monkeypatch, BusyOnceConnector)
    registry = ExecutionRegistry(max_workers=2)
    try:
        result = registry.run_sync(Source(type="duckdb"), tmp_path, "SELECT 1", [], 10)
        assert result.rows == [[1]]
        assert BusyOnceConnector.busy_calls == 2
    finally:
        registry.shutdown()


def test_deprecated_connector_shims_still_importable():
    from sqldash.connectors import create_connector, get_connector_class
    from sqldash.connectors.engine import EngineConnector

    with pytest.warns(DeprecationWarning, match="EngineConnector"):
        assert get_connector_class() is EngineConnector
    with pytest.warns(DeprecationWarning, match="EngineConnector"):
        connector = create_connector(
            Source.model_validate({"type": "duckdb", "database": ":memory:"}), None
        )
    assert isinstance(connector, EngineConnector)


def test_run_sync_rejects_negative_row_limit():
    registry = ExecutionRegistry(max_workers=1)
    try:
        with pytest.raises(ConnectorError, match="row_limit must be >= 0"):
            registry.run_sync(Source(type="duckdb"), Path("."), "SELECT 1", [], -1)
        with pytest.raises(ConnectorError, match="row_limit must be >= 0"):
            registry.submit(Source(type="duckdb"), Path("."), "SELECT 1", [], -1)
    finally:
        registry.shutdown()


def test_run_sync_accepts_unbounded_timeout():
    registry = ExecutionRegistry(max_workers=1)
    try:
        source = Source.model_validate({"type": "duckdb", "database": ":memory:"})
        result = registry.run_sync(source, None, "SELECT 42 AS v", [], 10, timeout=None)
        assert result.rows == [[42]]
    finally:
        registry.shutdown()
