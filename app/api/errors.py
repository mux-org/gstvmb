from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.capture import CaptureBusy, CaptureNotReady, CaptureRejected
from app.pipeline import (
    AppsinkNotPresent,
    AppsinkTimeout,
    PipelineStartError,
    VmbSrcNotPresent,
)


def install_error_handlers(app: FastAPI) -> None:
    """Register translations from domain exceptions to HTTP responses.

    Handles cross-cutting cases. ``KeyError`` and ``ValueError`` are caught
    in the individual route handlers so the response detail can include
    context (e.g. the offending property name).
    """

    @app.exception_handler(VmbSrcNotPresent)
    async def _vmbsrc_missing(_: Request, exc: VmbSrcNotPresent):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(AppsinkNotPresent)
    async def _appsink_missing(_: Request, exc: AppsinkNotPresent):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(AppsinkTimeout)
    async def _appsink_timeout(_: Request, exc: AppsinkTimeout):
        return JSONResponse(status_code=504, content={"detail": str(exc)})

    # A malformed pipeline description is a server-side configuration fault, so
    # 500 — but with the GStreamer message in the body. The bare "Internal
    # Server Error" this replaces told an operator nothing, and the reason was
    # reachable only in container logs.
    @app.exception_handler(PipelineStartError)
    async def _pipeline_start(_: Request, exc: PipelineStartError):
        return JSONResponse(status_code=500, content={"detail": str(exc)})

    # 409, not 400: the request is well-formed, it conflicts with the Instance's
    # current state (already capturing, pipeline stopped, archive unmounted,
    # disk too full). The client's fix is to change the state or wait.
    @app.exception_handler(CaptureBusy)
    async def _capture_busy(_: Request, exc: CaptureBusy):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(CaptureNotReady)
    async def _capture_not_ready(_: Request, exc: CaptureNotReady):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    # 400: the request itself is wrong — a bad label, or an extent mode that
    # does not exist yet.
    @app.exception_handler(CaptureRejected)
    async def _capture_rejected(_: Request, exc: CaptureRejected):
        return JSONResponse(status_code=400, content={"detail": str(exc)})
