"""Application factory: builds the FastAPI app and wires its components together.

Run with ``uvicorn app.main:create_app --factory``. Using a factory instead of a
module-level ``app`` means nothing happens at import time, and tests can build an
app with their own settings and a fake transcriber.

The service must run as a single process: the worker thread and the model live in
this process, so several uvicorn workers would each load their own model.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import __version__
from app.api import router
from app.config import Settings
from app.errors import register_error_handlers
from app.logging_config import configure_logging
from app.media import ensure_ffmpeg_available
from app.middleware import ResponseHeaders, UploadGuard
from app.store import JobStore
from app.transcriber import FasterWhisperTranscriber, Transcriber
from app.web import mount_ui
from app.worker import TranscriptionWorker

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, transcriber: Transcriber | None = None) -> FastAPI:
    settings = settings or Settings()
    configure_logging(settings.log_level, settings.log_format)
    transcriber = transcriber or _build_transcriber(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Startup: everything here runs once, before the first request is served.
        ensure_ffmpeg_available()
        settings.uploads_dir.mkdir(parents=True, exist_ok=True)
        store = JobStore(settings.database_path)

        logger.info("Loading the speech-to-text model", extra={"model": settings.whisper_model})
        transcriber.load()
        logger.info("Model loaded")

        worker = TranscriptionWorker(
            store=store,
            transcriber=transcriber,
            uploads_dir=settings.uploads_dir,
            max_duration_seconds=settings.max_duration_seconds,
            max_attempts=settings.max_job_attempts,
        )
        worker.start()

        app.state.store = store
        app.state.worker = worker
        yield
        # Shutdown: a job in progress goes back to the queue and runs again on the next start.
        worker.stop()

    app = FastAPI(
        title="Transcription Service",
        description="Upload an audio or video file and get a transcription.",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.transcriber = transcriber
    app.include_router(router)
    mount_ui(app)
    register_error_handlers(app)
    # Middleware added later wraps the ones added before it.
    app.add_middleware(
        UploadGuard,
        path="/api/transcriptions",
        max_upload_mb=settings.max_upload_mb,
        max_pending_jobs=settings.max_pending_jobs,
    )
    app.add_middleware(ResponseHeaders)
    return app


def _build_transcriber(settings: Settings) -> Transcriber:
    return FasterWhisperTranscriber(
        model=settings.whisper_model,
        device=settings.whisper_device,
        compute_type=settings.whisper_compute_type,
        cpu_threads=settings.whisper_cpu_threads,
        beam_size=settings.whisper_beam_size,
    )
