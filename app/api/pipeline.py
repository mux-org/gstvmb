from fastapi import APIRouter, Depends

from app.api.deps import get_pipeline
from app.api.models import PipelineStatus
from app.pipeline import Pipeline

router = APIRouter()


def _status(pipeline: Pipeline) -> PipelineStatus:
    return PipelineStatus(
        state=pipeline.state,
        detail=pipeline.detail,
        description=pipeline.description,
    )


@router.get("", response_model=PipelineStatus, summary="Get pipeline status")
def get_status(pipeline: Pipeline = Depends(get_pipeline)) -> PipelineStatus:
    return _status(pipeline)


@router.post("/start", response_model=PipelineStatus, summary="Start the pipeline")
def start(pipeline: Pipeline = Depends(get_pipeline)) -> PipelineStatus:
    pipeline.start()
    return _status(pipeline)


@router.post("/stop", response_model=PipelineStatus, summary="Stop the pipeline")
def stop(pipeline: Pipeline = Depends(get_pipeline)) -> PipelineStatus:
    pipeline.stop()
    return _status(pipeline)


@router.post("/restart", response_model=PipelineStatus, summary="Restart the pipeline")
def restart(pipeline: Pipeline = Depends(get_pipeline)) -> PipelineStatus:
    pipeline.restart()
    return _status(pipeline)
