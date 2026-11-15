"""The background worker: takes queued jobs one at a time and transcribes them.

One thread owns the model. A job already uses several CPU cores, so running jobs in
parallel on the same machine would only make each of them slower. The heavy work
happens in native code (ffmpeg in a subprocess, CTranslate2 for inference), which
does not hold Python's GIL, so the API stays responsive while a job is running.
"""

import logging
import re
import threading
import time
from pathlib import Path

from app.errors import AppError
from app.media import SAMPLE_RATE, decode_audio
from app.models import Job
from app.store import JobStore
from app.transcriber import ProgressCallback, Transcriber

logger = logging.getLogger(__name__)

# Progress is written to the database at most this often.
_PROGRESS_INTERVAL_SECONDS = 1.0

# Uploads are stored as <32 hex characters><extension>; decoding adds a .pcm file next to them.
_UPLOAD_NAME = re.compile(r"^[0-9a-f]{32}\.[a-z0-9]+$")


class _ShutdownRequested(Exception):
    """Raised inside a running job when the service is stopping."""


class TranscriptionWorker:
    def __init__(
        self,
        *,
        store: JobStore,
        transcriber: Transcriber,
        uploads_dir: Path,
        max_duration_seconds: float,
        max_attempts: int,
        poll_seconds: float = 2.0,
    ) -> None:
        self._store = store
        self._transcriber = transcriber
        self._uploads_dir = uploads_dir
        self._max_duration_seconds = max_duration_seconds
        self._max_attempts = max_attempts
        self._poll_seconds = poll_seconds
        self._wake = threading.Event()
        self._stopping = threading.Event()
        # daemon=True: a job stuck in native code must not keep the process alive at exit.
        self._thread = threading.Thread(target=self._run, name="transcription-worker", daemon=True)

    def start(self) -> None:
        """Clean up after the previous run, then start taking jobs."""
        requeued, failed = self._store.recover_interrupted(max_attempts=self._max_attempts)
        if requeued or failed:
            logger.warning(
                "Recovered jobs that an earlier run left unfinished",
                extra={"requeued": requeued, "failed": failed},
            )
        self._remove_unneeded_uploads()
        self._thread.start()

    def notify(self) -> None:
        """Tell the worker that a new job was queued, so it does not wait for the next poll."""
        self._wake.set()

    def stop(self, timeout: float = 10.0) -> None:
        """Ask the worker to stop. A job in progress goes back to the queue."""
        self._stopping.set()
        self._wake.set()
        if self._thread.is_alive():
            self._thread.join(timeout)

    @property
    def is_alive(self) -> bool:
        """Whether the worker thread is running. The health check reports this."""
        return self._thread.is_alive()

    def _run(self) -> None:
        while not self._stopping.is_set():
            try:
                job = self._store.claim_next()
                if job is None:
                    # Sleep until notify() or the poll interval, whichever comes first.
                    self._wake.wait(self._poll_seconds)
                    self._wake.clear()
                    continue
                self._process(job)
            except Exception:
                # For example the database is briefly unavailable. The worker must outlive
                # that, or uploads would pile up with nothing left to process them.
                logger.exception("Unexpected error in the worker loop, retrying")
                self._stopping.wait(self._poll_seconds)

    def _process(self, job: Job) -> None:
        upload = self._uploads_dir / f"{job.id}{job.extension}"
        context = {"job_id": job.id}
        logger.info(
            "Transcription started",
            extra={**context, "file_name": job.filename, "attempt": job.attempts},
        )
        started = time.monotonic()
        try:
            audio = decode_audio(upload, max_duration_seconds=self._max_duration_seconds)
            transcript = self._transcriber.transcribe(
                audio, language=job.requested_language, on_progress=self._progress_reporter(job.id)
            )
        except _ShutdownRequested:
            self._store.release(job.id)
            logger.info("Transcription interrupted by shutdown, job re-queued", extra=context)
            return  # keep the upload: the job will run again
        except AppError as error:
            # An expected failure (bad media, for example): the message is meant for the user.
            self._store.fail(job.id, error.message)
            logger.info("Transcription failed", extra={**context, "reason": error.message})
        except Exception:
            # A bug or an environment problem: log everything, tell the user nothing internal.
            self._store.fail(job.id, "Transcription failed because of an internal error.")
            logger.exception("Transcription crashed", extra=context)
        else:
            audio_seconds = len(audio) / SAMPLE_RATE
            self._store.complete(job.id, transcript, duration_seconds=audio_seconds)
            logger.info(
                "Transcription completed",
                extra={
                    **context,
                    "language": transcript.language,
                    "segments": len(transcript.segments),
                    "audio_seconds": round(audio_seconds, 1),
                    "elapsed_seconds": round(time.monotonic() - started, 1),
                },
            )
        # The job reached a final state, so the media file is no longer needed.
        upload.unlink(missing_ok=True)

    def _progress_reporter(self, job_id: str) -> ProgressCallback:
        last_write = time.monotonic()

        def report(fraction: float) -> None:
            nonlocal last_write
            if self._stopping.is_set():
                raise _ShutdownRequested
            now = time.monotonic()
            if now - last_write >= _PROGRESS_INTERVAL_SECONDS:
                self._store.update_progress(job_id, fraction)
                last_write = now

        return report

    def _remove_unneeded_uploads(self) -> None:
        """Delete media files left behind by a crash: uploads without a job, decode leftovers."""
        needed = {f"{job.id}{job.extension}" for job in self._store.unfinished_jobs()}
        for path in self._uploads_dir.iterdir():
            if _UPLOAD_NAME.match(path.name) and path.name not in needed:
                path.unlink(missing_ok=True)
                logger.info("Removed a leftover upload", extra={"file_name": path.name})
