from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api.errors import install_error_handlers
from app.api.health import router as health_router
from app.api.router import router as api_router
from app.capture import CaptureManager
from app.config import load_config
from app.pipeline import Pipeline


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    app.state.config = config
    app.state.pipeline = Pipeline(description=config.pipeline)
    app.state.capture = CaptureManager(app.state.pipeline, config)
    try:
        yield
    finally:
        # Stop the capture first: it holds an open file and is pulling from the
        # appsink, and tearing the pipeline out from under it would abort the
        # write mid-frame rather than closing a conformant cube.
        app.state.capture.stop()
        app.state.pipeline.stop()


app = FastAPI(title="gstvmb", version=__version__, lifespan=lifespan)
install_error_handlers(app)
app.include_router(health_router)
app.include_router(api_router)
