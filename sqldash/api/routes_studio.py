import secrets
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import Field, StrictBool

from sqldash.api.helpers import StrictBody
from sqldash.studio.entrypoints import (
    AgentEntrypoint,
    StudioError,
    available_entrypoints,
    entrypoints_path,
    resolve_entrypoint,
    save_entrypoint,
)
from sqldash.studio.sessions import SessionRequest

router = APIRouter(prefix="/api/studio")


class EntrypointChoice(StrictBody):
    entrypoint: str = Field(min_length=1, max_length=80)


class PermissionResponse(StrictBody):
    decision: Literal["allow", "deny"]


class PermissionMode(StrictBody):
    auto_approve: StrictBool


class FinishRequest(StrictBody):
    revision: str = Field(max_length=100)
    undo: bool = False


def manager(request):
    studio = request.app.state.studio
    if studio is None:
        raise HTTPException(
            404, "Studio is disabled. Serve on a loopback host without --no-studio to enable it"
        )
    if request.client is None or request.client.host not in {"127.0.0.1", "::1"}:
        raise HTTPException(403, "Studio is available only to local connections")
    if not secrets.compare_digest(
        request.headers.get("x-sqldash-token", "").encode(), request.app.state.api_token.encode()
    ):
        raise HTTPException(403, "Studio requires the local session token")
    origin = request.headers.get("origin")
    if origin and origin != f"{request.url.scheme}://{request.headers.get('host')}":
        raise HTTPException(403, "Studio requests must come from this origin")
    return studio


@router.get("/entrypoints")
def entrypoints(request: Request):
    manager(request)
    return {
        "entrypoints": [
            {"name": p.name, "entrypoint": p.command[0], "shell": p.shell, "protocol": p.protocol}
            for p in available_entrypoints().values()
        ],
        "path": str(entrypoints_path()),
    }


@router.post("/entrypoints", status_code=201)
def configure_entrypoint(request: Request, body: AgentEntrypoint):
    manager(request)
    save_entrypoint(body, keep={"env", "pass_env"} - body.model_fields_set)
    return {"name": body.name}


@router.post("/entrypoints/check")
def check_entrypoint(request: Request, body: EntrypointChoice):
    manager(request)
    resolve_entrypoint(body.entrypoint).check()
    return {"ok": True}


@router.post("/sessions", status_code=201)
def prepare(request: Request, body: SessionRequest):
    session = manager(request).create(request.app.state.store, body)
    return {"id": session.id, "context": session.context, "cwd": str(session.cwd)}


@router.post("/sessions/{id}/launch")
def launch(request: Request, id: str, body: EntrypointChoice):
    manager(request).get(id).launch(body.entrypoint)
    return {"running": True}


@router.get("/sessions/{id}/output")
def output(request: Request, id: str, offset: int = Query(0, ge=0)):
    session = manager(request).get(id)
    with session.lock:
        process = session.process
        if process is None:
            raise StudioError("Session has not started")
        return {
            **process.read(offset),
            "turn": session.turn,
            "undone": session.undone,
            "auto_approve": session.auto_approve,
        }


@router.post("/sessions/{id}/stop")
def stop(request: Request, id: str):
    running = manager(request).get(id).stop()["running"]
    return {"ok": not running, "running": running}


@router.post("/sessions/{id}/review")
def review(request: Request, id: str):
    return manager(request).get(id).review()


@router.post("/sessions/{id}/finish")
def finish(request: Request, id: str, body: FinishRequest):
    result = manager(request).get(id).finish(body.revision, body.undo)
    return {"ok": True, **result}


@router.delete("/sessions/{id}", status_code=204)
def close(request: Request, id: str):
    studio = manager(request)
    with studio.lock:
        session = studio.get(id)
        session.close()
        del studio.sessions[id]


@router.post("/sessions/{id}/permissions/{token}")
def permission(request: Request, id: str, token: str, body: PermissionResponse):
    session = manager(request).get(id)
    with session.lock:
        if session.closed or session.process is None:
            raise StudioError("Session is not accepting permission responses")
        session.process.permission(token, body.decision)
    return {"ok": True}


@router.post("/sessions/{id}/turns")
def next_turn(request: Request, id: str, body: SessionRequest):
    return manager(request).get(id).next_turn(request.app.state.store, body)


@router.post("/sessions/{id}/undo")
def undo_turn(request: Request, id: str, body: FinishRequest):
    manager(request).get(id).undo_turn(body.revision)
    return {"ok": True}


@router.post("/sessions/{id}/permission-mode")
def permission_mode(request: Request, id: str, body: PermissionMode):
    manager(request).get(id).set_auto_approve(body.auto_approve)
    return {"auto_approve": body.auto_approve}
