from fastapi import Request

from app.config import CameraConfig
from app.pipeline import Pipeline


def get_pipeline(request: Request) -> Pipeline:
    """FastAPI dependency yielding the process-wide :class:`Pipeline`.

    The instance is created during app startup (see :func:`app.main.lifespan`)
    and stored on ``app.state.pipeline``.
    """
    return request.app.state.pipeline


def get_config(request: Request) -> CameraConfig:
    """FastAPI dependency yielding this Instance's :class:`CameraConfig`.

    Loaded during app startup (see :func:`app.main.lifespan`) and stored on
    ``app.state.config``.
    """
    return request.app.state.config
