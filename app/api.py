"""HTTP routes.

The endpoints are plain ``def`` functions, so FastAPI runs them in its thread pool.
They do blocking work (copying the upload, running ffprobe, SQLite queries), which
must not run on the event loop.
"""

import logging
import shutil
import uuid
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request, Response, UploadFile

from app.config import Settings
from app.errors import (
    FileTooLargeError,
    JobNotFoundError,
    JobNotReadyError,
    QueueFullError,
    UnsupportedLanguageError,
)
from app.formats import MEDIA_TYPES, ExportFormat, render
from app.media import inspect_media, validate_extension
from app.models import Job, JobStatus
from app.schemas import ErrorResponse, HealthResponse, JobResponse
from app.store import JobStore
from app.transcriber import Transcriber
from app.worker import TranscriptionWorker

logger = logging.getLogger(__name__)

router = APIRouter()

_COPY_CHUNK_BYTES = 1024 * 1024


# The components are created once at startup (see app/main.py) and handed to the endpoints.
def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _store(request: Request) -> JobStore:
    return request.app.state.store


def _worker(request: Request) -> TranscriptionWorker:
    return request.app.state.worker


def _transcriber(request: Request) -> Transcriber:
    return request.app.state.transcriber


SettingsDep = Annotated[Settings, Depends(_settings)]
StoreDep = Annotated[JobStore, Depends(_store)]
WorkerDep = Annotated[TranscriptionWorker, Depends(_worker)]
TranscriberDep = Annotated[Transcriber, Depends(_transcriber)]


def _errors(*status_codes: int) -> dict[int | str, dict]:
    """Document the error responses of an endpoint in the generated API docs."""
    return {status_code: {"model": ErrorResponse} for status_code in status_codes}


@router.post(
    "/api/transcriptions",
    status_code=202,
    response_model=JobResponse,
    responses=_errors(413, 415, 422, 503),
    tags=["transcriptions"],
    summary="Upload a media file to transcribe",
)
def create_transcription(
    response: Response,
    file: UploadFile,
    settings: SettingsDep,
    store: StoreDep,
    worker: WorkerDep,
    transcriber: TranscriberDep,
    language: Annotated[
        str | None,
        Form(description='Language code such as "en" or "he". Leave empty to detect it.'),
    ] = None,
) -> JobResponse:
    """Validate and store the upload, queue a job, and answer straight away.

    The transcription itself runs in the background: poll the URL in the
    ``Location`` header for the status and the result.
    """
    extension = validate_extension(file.filename or "")
    requested_language = _requested_language(language, transcriber)
    if file.size is not None and file.size > settings.max_upload_bytes:
        raise FileTooLargeError(settings.max_upload_mb)
    if store.count_unfinished() >= settings.max_pending_jobs:
        raise QueueFullError()

    job_id = uuid.uuid4().hex
    upload_path = settings.uploads_dir / f"{job_id}{extension}"
    try:
        with upload_path.open("wb") as destination:
            shutil.copyfileobj(file.file, destination, _COPY_CHUNK_BYTES)
        info = inspect_media(upload_path, max_duration_seconds=settings.max_duration_seconds)
        job = store.create(
            job_id=job_id,
            filename=_display_name(file.filename),
            extension=extension,
            size_bytes=upload_path.stat().st_size,
            duration_seconds=info.duration_seconds,
            requested_language=requested_language,
        )
    except BaseException:
        upload_path.unlink(missing_ok=True)  # never keep a file that has no job
        raise

    worker.notify()
    logger.info(
        "Job queued",
        extra={"job_id": job.id, "file_name": job.filename, "size_bytes": job.size_bytes},
    )
    response.headers["Location"] = f"/api/transcriptions/{job.id}"
    return JobResponse.from_job(job, queue_position=store.queue_position(job.id))


@router.get(
    "/api/transcriptions/{job_id}",
    response_model=JobResponse,
    responses=_errors(404),
    tags=["transcriptions"],
    summary="Get the status of a transcription, and the transcript once it is ready",
)
def get_transcription(job_id: str, store: StoreDep) -> JobResponse:
    job = _find_job(store, job_id)
    unfinished = job.status in (JobStatus.QUEUED, JobStatus.PROCESSING)
    return JobResponse.from_job(
        job, queue_position=store.queue_position(job.id) if unfinished else None
    )


@router.get(
    "/api/transcriptions/{job_id}/download",
    responses=_errors(404, 409, 422),
    tags=["transcriptions"],
    summary="Download a finished transcript as a file",
)
def download_transcription(
    job_id: str,
    store: StoreDep,
    export_format: Annotated[ExportFormat, Query(alias="format")] = ExportFormat.TXT,
) -> Response:
    job = _find_job(store, job_id)
    if job.status is not JobStatus.COMPLETED:
        raise JobNotReadyError(f"The transcript is not available: the job is {job.status}.")
    return Response(
        content=render(export_format, job.segments or [], language=job.language or ""),
        media_type=MEDIA_TYPES[export_format],
        headers={"Content-Disposition": _attachment(job.filename, export_format)},
    )


@router.get(
    "/health",
    response_model=HealthResponse,
    responses={503: {"model": HealthResponse}},
    tags=["operations"],
    summary="Health check",
)
def health(
    response: Response, settings: SettingsDep, store: StoreDep, worker: WorkerDep
) -> HealthResponse:
    """200 while the service can do its job; 503 if the background worker has died."""
    worker_alive = worker.is_alive
    if not worker_alive:
        response.status_code = 503
    return HealthResponse(
        status="ok" if worker_alive else "unavailable",
        model=settings.whisper_model,
        worker_alive=worker_alive,
        pending_jobs=store.count_unfinished(),
    )


def _find_job(store: JobStore, job_id: str) -> Job:
    job = store.get(job_id)
    if job is None:
        raise JobNotFoundError()
    return job


def _requested_language(raw: str | None, transcriber: Transcriber) -> str | None:
    """Return a supported language code, or None when the language should be detected."""
    language = (raw or "").strip().lower()
    if language in ("", "auto"):
        return None
    if language not in transcriber.supported_languages:
        raise UnsupportedLanguageError(
            f"Unsupported language {language[:16]!r}. "
            'Use a code such as "en" or "he", or leave it empty to detect the language.'
        )
    return language


def _display_name(raw: str | None) -> str:
    """The base name of the uploaded file, kept for display only. It is never used as a path."""
    base_name = (raw or "").replace("\\", "/").rsplit("/", 1)[-1]
    printable = "".join(char for char in base_name if char.isprintable()).strip()
    return printable[:255] or "upload"


def _attachment(original_name: str, extension: str) -> str:
    """A Content-Disposition header that names the download after the uploaded file.

    ``filename`` must be plain ASCII, so it falls back to a generic name when needed;
    ``filename*`` carries the real name, percent-encoded as UTF-8 (RFC 6266).
    """
    name = f"{Path(original_name).stem or 'transcript'}.{extension}"
    is_plain = name.isascii() and '"' not in name and "\\" not in name
    fallback = name if is_plain else f"transcript.{extension}"
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(name, safe='')}"
