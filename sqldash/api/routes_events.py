import asyncio
import json

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

router = APIRouter(prefix="/api")


def semantic_layer_event(name: str) -> str:
    """The event a dashboard's metrics.yaml fires: `metrics`, or `repo/metrics`
    in a workspace. Mirrors `semanticLayerEventName` in runner.js."""
    repo, slash, _ = name.partition("/")
    return f"{repo}/metrics" if slash else "metrics"


def _revisions(watcher, store, name: str | None) -> dict:
    if not name:
        return {}
    return {
        "name": name,
        "etag": watcher.revision(name) if name in store.discover() else None,
        "metrics_etag": watcher.revision(semantic_layer_event(name)),
    }


@router.get("/events")
async def events(request: Request, name: str | None = None):
    """Change events for the project, opened by a `ready` handshake.

    A page renders its dashboard, then subscribes. A write between the two used
    to be announced to nobody (#577), and so was one during a reconnect. `ready`
    is sent only once the queue is registered, carrying the current revisions of
    `name` and its metrics file: a write before it shows up as a revision the page
    does not have, and a write after it arrives as an ordinary event. Headers go
    out before this generator first runs, so the browser's `open` event is too
    early to rely on; `ready` is the point the subscription is known to exist.
    """
    watcher = request.app.state.watcher
    store = request.app.state.store

    async def stream():
        queue = watcher.subscribe()
        try:
            yield "retry: 2000\n\n"
            yield f"event: ready\ndata: {json.dumps(_revisions(watcher, store, name))}\n\n"
            while True:
                if await request.is_disconnected():
                    return
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                revision = watcher.revision(event["name"])
                if revision is not None:
                    event["etag"] = revision
                yield f"data: {json.dumps(event)}\n\n"
        finally:
            watcher.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
