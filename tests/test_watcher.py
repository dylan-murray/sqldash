"""The Linux inotify backend emits opened/closed events for every file READ
(macOS FSEvents never does), so the handler must react only to genuine
mutations — otherwise every page render that loads a dashboard's YAML fires
an SSE "changed" event and connected browsers reload in a storm.

The debounce is trailing-edge: a burst of writes coalesces into one "changed"
event emitted after the burst settles, so the last write is always announced.
The original leading-edge gate dropped the tail of a burst and left open pages
rendering a stale file (#457)."""

import asyncio
import json
import threading
import time

import httpx
import pytest
from watchdog.events import (
    DirModifiedEvent,
    FileClosedEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
    FileOpenedEvent,
)

from sqldash.project.store import compute_etag
from sqldash.project.watcher import DEBOUNCE_SECONDS, ProjectWatcher, _Handler
from sqldash.server import create_app
from sqldash.snapshot import _start_server


def _handler(tmp_path):
    watcher = ProjectWatcher(root=tmp_path)
    return watcher, _Handler(watcher, None, tmp_path)


def _scheduled(watcher):
    return set(watcher._pending)


def test_mutation_events_schedule(tmp_path):
    watcher, handler = _handler(tmp_path)
    handler.on_any_event(FileModifiedEvent(str(tmp_path / "demo.yaml")))
    handler.on_any_event(FileCreatedEvent(str(tmp_path / "created.yaml")))
    handler.on_any_event(FileDeletedEvent(str(tmp_path / "deleted.yml")))
    handler.on_any_event(FileMovedEvent(str(tmp_path / "old.yaml"), str(tmp_path / "moved.yaml")))
    assert _scheduled(watcher) == {"demo", "created", "deleted", "moved"}


def test_read_only_events_are_ignored(tmp_path):
    watcher, handler = _handler(tmp_path)
    handler.on_any_event(FileOpenedEvent(str(tmp_path / "demo.yaml")))
    handler.on_any_event(FileClosedEvent(str(tmp_path / "demo.yaml")))
    assert _scheduled(watcher) == set()


def test_non_yaml_and_directory_events_are_ignored(tmp_path):
    watcher, handler = _handler(tmp_path)
    handler.on_any_event(FileModifiedEvent(str(tmp_path / "demo.duckdb")))
    handler.on_any_event(DirModifiedEvent(str(tmp_path)))
    assert _scheduled(watcher) == set()


def test_prefix_namespaces_the_event(tmp_path):
    watcher = ProjectWatcher(roots={"acme": tmp_path})
    handler = _Handler(watcher, "acme", tmp_path)
    handler.on_any_event(FileModifiedEvent(str(tmp_path / "demo.yaml")))
    assert _scheduled(watcher) == {"acme/demo"}


def test_moved_event_uses_destination_name(tmp_path):
    watcher, handler = _handler(tmp_path)
    handler.on_any_event(
        FileMovedEvent(str(tmp_path / "scratch.tmp"), str(tmp_path / "final.yaml"))
    )
    assert _scheduled(watcher) == {"final"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_debounce_coalesces_burst_into_one_trailing_emit(tmp_path):
    watcher, handler = _handler(tmp_path)
    watcher._loop = asyncio.get_running_loop()
    queue = watcher.subscribe()
    event = FileModifiedEvent(str(tmp_path / "demo.yaml"))
    for _ in range(3):
        handler.on_any_event(event)
        await asyncio.sleep(DEBOUNCE_SECONDS / 3)
    assert queue.empty()
    emitted = await asyncio.wait_for(queue.get(), timeout=DEBOUNCE_SECONDS * 4)
    assert emitted == {"type": "changed", "name": "demo"}
    await asyncio.sleep(DEBOUNCE_SECONDS * 2)
    assert queue.empty()
    assert _scheduled(watcher) == set()


@pytest.mark.anyio
async def test_stop_cancels_pending_emit(tmp_path):
    watcher, handler = _handler(tmp_path)
    watcher._loop = asyncio.get_running_loop()
    queue = watcher.subscribe()
    handler.on_any_event(FileModifiedEvent(str(tmp_path / "demo.yaml")))
    await asyncio.sleep(0)
    watcher.stop()
    await asyncio.sleep(DEBOUNCE_SECONDS * 2)
    assert queue.empty()
    assert _scheduled(watcher) == set()


@pytest.mark.anyio
async def test_two_writes_apart_each_announce_and_the_last_is_final(tmp_path):
    path = tmp_path / "demo.yaml"
    path.write_text("title: original\n")
    watcher = ProjectWatcher(root=tmp_path)
    watcher.start(asyncio.get_running_loop())
    try:
        queue = watcher.subscribe()
        await asyncio.sleep(DEBOUNCE_SECONDS * 3)
        while not queue.empty():
            queue.get_nowait()
        path.write_text("title: first\n")
        await asyncio.sleep(0.5)
        path.write_text("title: second\n")
        seen = []
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=1.0)
            except TimeoutError:
                break
            seen.append((event["name"], compute_etag(path.read_text())))
    finally:
        watcher.stop()
    assert [name for name, _ in seen] == ["demo", "demo"]
    assert seen[-1][1] == compute_etag("title: second\n")


def _read_sse(url, count, deadline_seconds, after_connect):
    frames = []
    kind = "message"
    deadline = time.monotonic() + deadline_seconds
    timeout = httpx.Timeout(5.0, read=DEBOUNCE_SECONDS * 6)
    with httpx.Client(timeout=timeout) as client, client.stream("GET", url) as res:
        assert res.status_code == 200
        after_connect()
        try:
            for line in res.iter_lines():
                if line.startswith("event: "):
                    kind = line[len("event: ") :]
                elif line.startswith("data: "):
                    frames.append((kind, json.loads(line[len("data: ") :])))
                elif not line:
                    kind = "message"
                messages = [frame for frame in frames if frame[0] == "message"]
                ready = any(frame[0] == "ready" for frame in frames)
                if (ready and len(messages) >= count) or time.monotonic() > deadline:
                    break
        except httpx.ReadTimeout:
            pass
    return frames


def _read_sse_events(url, count, deadline_seconds, after_connect):
    frames = _read_sse(url, count, deadline_seconds, after_connect)
    return [data for kind, data in frames if kind == "message"]


@pytest.mark.parametrize(
    ("gap", "expected_events"),
    [
        pytest.param(0.12, 1, id="burst-within-debounce-coalesces"),
        pytest.param(0.5, 2, id="writes-past-debounce-each-announce"),
    ],
)
def test_sse_stream_always_delivers_the_final_etag(tmp_path, gap, expected_events):
    path = tmp_path / "demo.yaml"
    original = (
        "title: Original\n"
        "source: {type: duckdb, database: ':memory:'}\n"
        "tiles: [{title: W, sql: 'SELECT 1'}]\n"
    )
    path.write_text(original)
    app = create_app(tmp_path, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        first = original.replace("Original", "First")
        second = original.replace("Original", "Second")

        def writer():
            time.sleep(0.5)
            path.write_text(first)
            time.sleep(gap)
            path.write_text(second)

        time.sleep(DEBOUNCE_SECONDS * 3)
        events = _read_sse_events(
            f"http://127.0.0.1:{port}/api/events",
            count=2,
            deadline_seconds=8,
            after_connect=lambda: threading.Thread(target=writer, daemon=True).start(),
        )
        path.write_text(original)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive(), "embedded server did not stop after SSE client disconnected"
    payloads = events
    assert [p["name"] for p in payloads] == ["demo"] * expected_events
    assert payloads[-1]["etag"] == compute_etag(second)


DASHBOARD = (
    "title: Demo\n"
    "source: {type: duckdb, database: ':memory:'}\n"
    "tiles: [{title: W, sql: 'SELECT 1'}]\n"
)


def test_revision_hashes_the_file_an_event_names(tmp_path):
    (tmp_path / "demo.yaml").write_text(DASHBOARD)
    (tmp_path / "metrics.yml").write_text("metrics: {}\n")
    watcher = ProjectWatcher(root=tmp_path)
    assert watcher.revision("demo") == compute_etag(DASHBOARD)
    assert watcher.revision("metrics") == compute_etag("metrics: {}\n")


def test_revision_prefers_yaml_over_yml(tmp_path):
    (tmp_path / "demo.yaml").write_text("title: yaml\n")
    (tmp_path / "demo.yml").write_text("title: yml\n")
    assert ProjectWatcher(root=tmp_path).revision("demo") == compute_etag("title: yaml\n")


@pytest.mark.parametrize("name", ["missing", "", "nope/demo", "acme/"])
def test_revision_is_none_for_names_it_cannot_resolve(tmp_path, name):
    (tmp_path / "demo.yaml").write_text(DASHBOARD)
    assert ProjectWatcher(root=tmp_path).revision(name) is None


def test_revision_resolves_a_workspace_repo_prefix(tmp_path):
    acme, beta = tmp_path / "acme", tmp_path / "beta"
    acme.mkdir()
    beta.mkdir()
    (acme / "metrics.yaml").write_text("metrics: {a: 1}\n")
    (beta / "metrics.yaml").write_text("metrics: {b: 1}\n")
    watcher = ProjectWatcher(roots={"acme": acme, "beta": beta})
    assert watcher.revision("acme/metrics") == compute_etag("metrics: {a: 1}\n")
    assert watcher.revision("beta/metrics") == compute_etag("metrics: {b: 1}\n")
    assert watcher.revision("metrics") is None


def _ready(tmp_path, query):
    app = create_app(tmp_path, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        frames = _read_sse(
            f"http://127.0.0.1:{port}/api/events{query}",
            count=0,
            deadline_seconds=5,
            after_connect=lambda: None,
        )
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    return frames


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        pytest.param("?name=demo", "demo", id="known-dashboard"),
        pytest.param("?name=ghost", None, id="unknown-dashboard"),
        pytest.param("", {}, id="no-name"),
    ],
)
def test_the_stream_opens_with_the_current_revisions(tmp_path, query, expected):
    (tmp_path / "demo.yaml").write_text(DASHBOARD)
    (tmp_path / "metrics.yaml").write_text("metrics: {}\n")
    frames = _ready(tmp_path, query)
    assert frames[0][0] == "ready"
    if expected == {}:
        assert frames[0][1] == {}
        return
    name = query.removeprefix("?name=")
    assert frames[0][1] == {
        "name": name,
        "etag": compute_etag(DASHBOARD) if expected else None,
        "metrics_etag": compute_etag("metrics: {}\n"),
    }


def test_the_dashboard_page_renders_the_metrics_baseline(tmp_path):
    (tmp_path / "demo.yaml").write_text(DASHBOARD)
    (tmp_path / "metrics.yaml").write_text("metrics: {}\n")
    app = create_app(tmp_path, allowed_hosts=["127.0.0.1", "localhost"])
    server, thread, port = _start_server(app)
    try:
        html = httpx.get(f"http://127.0.0.1:{port}/d/demo").text
    finally:
        server.should_exit = True
        thread.join(timeout=10)
    baseline = compute_etag("metrics: {}\n")
    assert f'"metrics_etag": "{baseline}"' in html
