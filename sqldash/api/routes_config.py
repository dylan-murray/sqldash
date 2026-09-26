"""Local machine config surfaced in the UI: the workspace repo registry
(read-write, hot-reloads the running server) and credential-profile health
(strictly read-only — profile names and referenced keys, never values)."""

from fastapi import APIRouter, HTTPException, Request

from sqldash import workspace as workspace_registry
from sqldash.api.helpers import StrictBody
from sqldash.gitrepo import GitError
from sqldash.project.sources import labeled_sources
from sqldash.project.store import DashboardStore
from sqldash.redact import mask_url_userinfo
from sqldash.secrets import SecretError, load_profiles, profiles_path
from sqldash.semantics import SemanticLayer

router = APIRouter(prefix="/api")


class RepoRequest(StrictBody):
    target: str
    name: str | None = None
    branch: str | None = None


def _served_stores(request: Request) -> dict:
    return getattr(request.app.state.store, "repos", None) or {}


def _workspace_mode(request: Request) -> bool:
    return getattr(request.app.state.store, "repos", None) is not None


def repo_rows(stores: dict) -> list[dict]:
    registry = workspace_registry.load_registry()
    rows = []
    for name in sorted(set(registry) | set(stores)):
        entry = registry.get(name, {})
        store = stores.get(name)
        rows.append(
            {
                "name": name,
                "url": mask_url_userinfo(entry["url"]) if entry.get("url") else None,
                "path": entry.get("path")
                if entry.get("path")
                else (None if entry.get("url") else (str(store.root) if store else None)),
                "branch": entry.get("branch"),
                "root": str(store.root) if store else None,
                "registered": name in registry,
                "served": store is not None,
                "dashboards": len(store.discover()) if store else None,
            }
        )
    return rows


@router.get("/repos")
async def list_repos(request: Request):
    return {
        "workspace": _workspace_mode(request),
        "repos": repo_rows(_served_stores(request)),
    }


@router.post("/repos", status_code=201)
def add_repo(request: Request, body: RepoRequest):
    stores = _served_stores(request)
    try:
        name, entry = workspace_registry.add_repo(
            body.target.strip(), name=body.name, branch=body.branch
        )
    except workspace_registry.WorkspaceError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not _workspace_mode(request):
        return {"name": name, "served": False}
    if name in stores:
        raise HTTPException(409, f"repo '{name}' is already being served")
    try:
        [(_, root)] = workspace_registry.resolve_workspace({name: entry})
    except (GitError, workspace_registry.WorkspaceError) as exc:
        workspace_registry.remove_repo(name)
        raise HTTPException(422, str(exc)) from exc
    store = DashboardStore(root)
    request.app.state.store.add_repo(name, store)
    request.app.state.layer.layers[name] = SemanticLayer(store)
    request.app.state.watcher.add_root(name, store.root)
    return {
        "name": name,
        "served": True,
        "root": str(store.root),
        "dashboards": len(store.discover()),
    }


@router.delete("/repos/{name}", status_code=204)
async def remove_repo(request: Request, name: str):
    stores = _served_stores(request)
    removed = False
    try:
        workspace_registry.remove_repo(name)
        removed = True
    except workspace_registry.WorkspaceError:
        pass
    if _workspace_mode(request) and name in stores:
        request.app.state.store.remove_repo(name)
        request.app.state.layer.layers.pop(name, None)
        request.app.state.watcher.remove_root(name)
        removed = True
    if not removed:
        known = ", ".join(sorted(set(workspace_registry.load_registry()) | set(stores)))
        raise HTTPException(404, f"no repo named '{name}' — known repos: {known or '(none)'}")


def profile_health(store, layer) -> list[dict]:
    referenced: dict[str, set[str]] = {}

    def note(source, label: str) -> None:
        if getattr(source, "profile", None):
            referenced.setdefault(source.profile, set()).add(label)

    for entry in labeled_sources(store, layer):
        note(entry.source, entry.label)

    try:
        defined = set(load_profiles())
    except SecretError:
        defined = set()
    rows = [
        {
            "profile": profile,
            "defined": profile in defined,
            "referenced_by": sorted(labels),
        }
        for profile, labels in sorted(referenced.items())
    ]
    return rows


@router.get("/profiles/health")
async def profiles_health(request: Request):
    return {
        "path": str(profiles_path()),
        "profiles": profile_health(request.app.state.store, request.app.state.layer),
    }
