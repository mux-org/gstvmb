from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api.errors import install_error_handlers
from app.api.health import router as health_router
from app.api.router import router as api_router
from app.capture import CaptureManager
from app.config import load_config
from app.display import DisplayPump
from app.pipeline import Pipeline


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    app.state.config = config
    app.state.pipeline = Pipeline(description=config.pipeline)
    app.state.capture = CaptureManager(app.state.pipeline, config)
    app.state.display = DisplayPump(app.state.pipeline, config)
    # Started here, not on demand: the display branch has to be pumping before
    # the first viewer connects, and it costs nothing while no pipeline is
    # running (it waits, and disables itself outright on a description with no
    # display appsink).
    app.state.display.start()
    try:
        yield
    finally:
        # Stop the capture first: it holds an open file and is pulling from the
        # appsink, and tearing the pipeline out from under it would abort the
        # write mid-frame rather than closing a conformant cube.
        app.state.capture.stop()
        # Then the display pump, which is also pulling — before the pipeline
        # goes away underneath it, so it exits through its own stop check rather
        # than through a flush.
        app.state.display.stop()
        app.state.pipeline.stop()


app = FastAPI(title="gstvmb", version=__version__, lifespan=lifespan)
install_error_handlers(app)
app.include_router(health_router)
app.include_router(api_router)
