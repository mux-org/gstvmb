from fastapi import APIRouter

from app.api import appsink, camera, capture, pipeline

router = APIRouter()
router.include_router(camera.router, prefix="/camera", tags=["camera"])
router.include_router(pipeline.router, prefix="/pipeline", tags=["pipeline"])
router.include_router(appsink.router, prefix="/appsink", tags=["appsink"])
# A top-level sibling of /camera and /pipeline, not a child of /appsink: the
# nginx pattern in ADR-0003 is one location pair per resource, which a path with
# a parameterized segment in the middle does not fit.
router.include_router(capture.router, prefix="/capture", tags=["capture"])
