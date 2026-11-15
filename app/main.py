"""Application factory: builds the FastAPI app and wires its components together.

Run with ``uvicorn app.main:create_app --factory``. Using a factory instead of a
module-level ``app`` means nothing happens at import time, and tests can build an
app with their own settings.
"""

from fastapi import FastAPI

from app import __version__
from app.api import router
from app.config import Settings
from app.logging_config import configure_logging


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.log_level, settings.log_format)

    app = FastAPI(
        title="Transcription Service",
        description="Upload an audio or video file and get a transcription.",
        version=__version__,
    )
    app.state.settings = settings
    app.include_router(router)
    return app
