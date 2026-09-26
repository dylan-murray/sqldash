"""The FastAPI app factory; all serve-session state hangs off ``app.state``.

There are no user accounts: each viewer's own database credentials are the
authorization model, so the server never decides who may see what. The
localhost hardening here is a separate concern — a Host-header allowlist
defeats DNS rebinding, and the per-session token set as a cookie by served pages
(``X-Sqldash-Token``, required on every mutating /api call) only stops
cross-site requests from driving mutations; it grants no data access of its
own. Domain exceptions map to HTTP statuses in one place at the bottom.
"""

import asyncio
import json
import secrets as py_secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from sqldash.api import (
    routes_config,
    routes_dashboards,
    routes_events,
    routes_executions,
    routes_metrics,
    routes_pages,
    routes_query_library,
    routes_sources,
    routes_studio,
)
from sqldash.connectors.base import ConnectorError
from sqldash.execution import ExecutionRegistry
from sqldash.params import ParamError
from sqldash.project.sources import declared_sources
from sqldash.project.store import (
    ConflictError,
    DashboardStore,
    InvalidDashboardError,
    NotFoundError,
    WorkspaceStore,
)
from sqldash.project.watcher import ProjectWatcher
from sqldash.secrets import SecretError
from sqldash.semantics import MetricNotFoundError, SemanticError, SemanticLayer
from sqldash.semantics.layer import WorkspaceLayer
from sqldash.studio.entrypoints import StudioError
from sqldash.studio.sessions import Studio

PACKAGE_DIR = Path(__file__).parent
DEFAULT_ROW_LIMIT = 10_000
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_STATIC_DIR = PACKAGE_DIR / "static"


def _static_mtime(rel: str) -> str:
    path = _STATIC_DIR / rel
    try:
        return str(int(path.stat().st_mtime))
    except OSError:
        return "0"


def asset_url(rel: str) -> str:
    return f"/static/{rel}?v={_static_mtime(rel)}"


def app_stylesheets() -> list[str]:
    return [
        asset_url(f"css/app/{path.name}")
        for path in sorted((_STATIC_DIR / "css" / "app").glob("*.css"))
    ]


def js_importmap() -> str:
    js_dir = _STATIC_DIR / "js"
    imports = {
        f"/static/js/{path.name}": f"/static/js/{path.name}?v={_static_mtime('js/' + path.name)}"
        for path in sorted(js_dir.glob("*.js"))
    }
    return json.dumps({"imports": imports}, separators=(",", ":"))


def _host_only(header: str) -> str:
    """Strip the port from a Host header, handling bracketed IPv6."""
    header = header.strip()
    if header.startswith("["):
        return header.partition("]")[0].lstrip("[")
    return header.rsplit(":", 1)[0] if ":" in header else header


def content_security_policy(nonce: str) -> str:
    return "; ".join(
        [
            "default-src 'self'",
            f"script-src 'self' 'nonce-{nonce}' 'unsafe-eval'",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data: blob:",
            "font-src 'self'",
            "connect-src 'self'",
            "object-src 'none'",
            "base-uri 'self'",
            "form-action 'self'",
            "frame-ancestors 'self'",
        ]
    )


def token_cookie_name(url) -> str:
    port = url.port or (443 if url.scheme == "https" else 80)
    return f"sqldash-token-{port}"


def create_app(
    path: Path | None = None,
    row_limit: int = DEFAULT_ROW_LIMIT,
    allowed_hosts: list[str] | None = None,
    serve_label: str | None = None,
    workspace: list[tuple[str, Path]] | None = None,
    studio: bool = False,
) -> FastAPI:
    """Build the app for one path, or for a name→path workspace when given."""

    if studio and any(_host_only(h) not in LOCAL_HOSTS for h in (allowed_hosts or [])):
        raise ValueError("Studio requires a loopback serving host")
    studio_manager = Studio() if studio else None
    if workspace is not None:
        store = WorkspaceStore({name: DashboardStore(p) for name, p in workspace})
        watcher = ProjectWatcher(roots={name: s.root for name, s in store.repos.items()})
        layer = WorkspaceLayer({name: SemanticLayer(s) for name, s in store.repos.items()})
    else:
        store = DashboardStore(path)
        watcher = ProjectWatcher(store.root)
        layer = SemanticLayer(store)
    registry = ExecutionRegistry(still_declared=declared_sources(store, layer))
    api_token = py_secrets.token_urlsafe(32)
    hosts = LOCAL_HOSTS | {_host_only(h) for h in (allowed_hosts or [])}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        watcher.start(asyncio.get_running_loop())
        yield
        watcher.stop()
        registry.shutdown()
        if studio_manager:
            studio_manager.close()

    app = FastAPI(title="sqldash", lifespan=lifespan)
    app.state.studio = studio_manager
    app.state.store = store
    app.state.registry = registry
    app.state.watcher = watcher
    app.state.layer = layer
    app.state.row_limit = row_limit
    if serve_label is None:
        if workspace is not None:
            serve_label = f"{len(workspace)} repos: " + ", ".join(n for n, _ in workspace)
        else:
            serve_label = str(store.root)
    elif serve_label in (".", ".."):
        serve_label = str(Path(serve_label).resolve())
    app.state.serve_label = serve_label
    app.state.api_token = api_token
    app.state.templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")
    app.state.templates.env.globals["studio_enabled"] = studio
    app.state.templates.env.globals["api_token"] = api_token
    app.state.templates.env.globals["asset_url"] = asset_url
    app.state.templates.env.globals["js_importmap"] = js_importmap
    app.state.templates.env.globals["app_stylesheets"] = app_stylesheets

    @app.middleware("http")
    async def local_security(request: Request, call_next):
        host = _host_only(request.headers.get("host", ""))
        if host not in hosts:
            return JSONResponse(
                {
                    "detail": f"request host '{host}' is not allowed — sqldash only "
                    "serves the hosts it was started for"
                },
                status_code=403,
            )
        if (
            request.url.path.startswith("/api")
            and request.method in MUTATING_METHODS
            and request.headers.get("x-sqldash-token") != api_token
        ):
            return JSONResponse(
                {
                    "detail": "missing or invalid X-Sqldash-Token header — the token "
                    "is set as a cookie by served pages and printed at startup"
                },
                status_code=403,
            )
        request.state.csp_nonce = py_secrets.token_urlsafe(16)
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = content_security_policy(
            request.state.csp_nonce
        )
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        if response.headers.get("content-type", "").startswith("text/html"):
            response.set_cookie(
                token_cookie_name(request.url),
                api_token,
                path="/",
                samesite="strict",
                httponly=False,
            )
        if request.url.path.startswith("/api/studio"):
            response.headers["Cache-Control"] = "no-store"
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    app.mount("/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
    app.include_router(routes_executions.router)
    app.include_router(routes_events.router)
    app.include_router(routes_config.router)
    app.include_router(routes_sources.router)
    app.include_router(routes_query_library.router)
    app.include_router(routes_dashboards.router)
    app.include_router(routes_metrics.router)
    app.include_router(routes_pages.router)
    app.include_router(routes_studio.router)

    @app.exception_handler(StudioError)
    async def studio_error(request: Request, exc: StudioError):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status_code)

    @app.exception_handler(NotFoundError)
    async def not_found(request: Request, exc: NotFoundError):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(ConflictError)
    async def conflict(request: Request, exc: ConflictError):
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(InvalidDashboardError)
    async def invalid(request: Request, exc: InvalidDashboardError):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(ParamError)
    async def param_error(request: Request, exc: ParamError):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(SecretError)
    async def secret_error(request: Request, exc: SecretError):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(MetricNotFoundError)
    async def metric_not_found(request: Request, exc: MetricNotFoundError):
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(SemanticError)
    async def semantic_error(request: Request, exc: SemanticError):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(ConnectorError)
    async def connector_error(request: Request, exc: ConnectorError):
        return JSONResponse({"detail": str(exc)}, status_code=502)

    return app
