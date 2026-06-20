from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.pipeline import AppsinkNotPresent, AppsinkTimeout, VmbSrcNotPresent


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
