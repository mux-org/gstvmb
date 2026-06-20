"""Liveness and readiness probes, mounted at the application root.

The probes sit at the root alongside the API surface but are deliberately
kept out of the API's control surface: they carry no contract version and
never will, so an external proxy/orchestrator can rely on them being stable
regardless of how the API itself evolves.
"""

from fastapi import APIRouter, Depends, Response

from app.api.deps import get_pipeline
from app.pipeline import Pipeline

router = APIRouter()


@router.get("/healthz", summary="Liveness probe (process is up)")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get(
    "/readyz",
    summary="Readiness probe (pipeline is running)",
    responses={
        200: {"description": "Pipeline is running."},
        503: {"description": "Pipeline is not running."},
    },
)
def readyz(response: Response, pipeline: Pipeline = Depends(get_pipeline)) -> dict[str, str]:
    if pipeline.is_running():
        return {"status": "ready"}
    response.status_code = 503
    return {"status": "not ready"}
