import asyncio
import contextlib
import threading
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from sqldash.project.store import compute_etag

DEBOUNCE_SECONDS = 0.3


class _Handler(FileSystemEventHandler):
    def __init__(self, watcher: "ProjectWatcher", prefix: str | None, root: Path) -> None:
        self.watcher = watcher
        self.prefix = prefix
        self.root = root

    def on_any_event(self, event) -> None:
        if event.is_directory:
            return
        if event.event_type not in ("modified", "created", "moved", "deleted"):
            return
        path = Path(getattr(event, "dest_path", "") or event.src_path)
        if path.suffix not in (".yaml", ".yml"):
            return
        self.watcher._schedule(path, self.prefix)


class ProjectWatcher:
    def __init__(self, root: Path | None = None, roots: dict[str, Path] | None = None) -> None:
        self.roots: dict[str | None, Path] = dict(roots) if roots is not None else {None: root}
        self._observer: Observer | None = None
        self._watches: dict[str | None, object] = {}
        self._subscribers: set[asyncio.Queue] = set()
        self._lock = threading.Lock()
        self._pending: dict[str, asyncio.TimerHandle | None] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._observer = Observer()
        for prefix, root in self.roots.items():
            self._watches[prefix] = self._observer.schedule(
                _Handler(self, prefix, root), str(root), recursive=False
            )
        self._observer.daemon = True
        self._observer.start()

    def add_root(self, prefix: str, root: Path) -> None:
        self.roots[prefix] = root
        if self._observer is not None:
            self._watches[prefix] = self._observer.schedule(
                _Handler(self, prefix, root), str(root), recursive=False
            )

    def remove_root(self, prefix: str) -> None:
        self.roots.pop(prefix, None)
        watch = self._watches.pop(prefix, None)
        if self._observer is not None and watch is not None:
            self._observer.unschedule(watch)

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer = None
        with self._lock:
            handles = list(self._pending.values())
            self._pending.clear()
        for handle in handles:
            if handle is not None:
                handle.cancel()

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=32)
        with self._lock:
            self._subscribers.add(queue)
        return queue

    def revision(self, name: str) -> str | None:
        """Etag of the file behind an event name (`demo`, `repo/metrics`), or None.

        The inverse of `_schedule`'s naming, so a page can compare what it has
        against what an event would have told it. `.yaml` wins a stem collision,
        as it does in `discover()`.
        """
        prefix, _, stem = name.rpartition("/")
        root = self.roots.get(prefix or None)
        if root is None or not stem:
            return None
        for suffix in (".yaml", ".yml"):
            try:
                return compute_etag((root / f"{stem}{suffix}").read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue
            except (OSError, UnicodeDecodeError):
                return None
        return None

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers.discard(queue)

    def _schedule(self, path: Path, prefix: str | None = None) -> None:
        name = f"{prefix}/{path.stem}" if prefix else path.stem
        with self._lock:
            self._pending.setdefault(name, None)
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._arm, name)

    def _arm(self, name: str) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        with self._lock:
            handle = self._pending.get(name)
            if handle is not None:
                handle.cancel()
            self._pending[name] = loop.call_later(DEBOUNCE_SECONDS, self._fire, name)

    def _fire(self, name: str) -> None:
        with self._lock:
            self._pending.pop(name, None)
        self._emit(name)

    def _emit(self, name: str) -> None:
        with self._lock:
            subscribers = list(self._subscribers)
        for queue in subscribers:
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait({"type": "changed", "name": name})
