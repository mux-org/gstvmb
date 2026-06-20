from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api.errors import install_error_handlers
from app.api.health import router as health_router
from app.api.router import router as api_router
from app.config import load_config
from app.pipeline import Pipeline


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    app.state.config = config
    app.state.pipeline = Pipeline(description=config.pipeline)
    try:
        yield
    finally:
        app.state.pipeline.stop()


app = FastAPI(title="gstvmb", version=__version__, lifespan=lifespan)
install_error_handlers(app)
app.include_router(health_router)
app.include_router(api_router)
