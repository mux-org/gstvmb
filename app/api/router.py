from fastapi import APIRouter

from app.api import appsink, camera, pipeline

router = APIRouter()
router.include_router(camera.router, prefix="/camera", tags=["camera"])
router.include_router(pipeline.router, prefix="/pipeline", tags=["pipeline"])
router.include_router(appsink.router, prefix="/appsink", tags=["appsink"])
