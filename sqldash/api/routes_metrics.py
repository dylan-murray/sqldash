from fastapi import APIRouter, Request

from sqldash.project.catalog import list_metrics as catalog_metrics

router = APIRouter(prefix="/api")


@router.get("/metrics")
async def list_metrics(request: Request, dashboard: str | None = None):
    return {"metrics": catalog_metrics(request.app.state.layer, dashboard=dashboard)}
