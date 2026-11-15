"""Job storage in SQLite.

The jobs table is also the work queue: the worker claims the oldest queued row.
With a single source of truth a job cannot be "saved but never queued", and the
queue survives a restart without extra code.

Every operation opens its own short-lived connection, so the store can be used
from any thread: request handlers run in a thread pool and the worker has its own
thread. WAL mode lets readers carry on while another connection is writing.
"""

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from app.models import Job, JobStatus, Segment, Transcript

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    seq                INTEGER PRIMARY KEY AUTOINCREMENT,  -- arrival order = queue order
    id                 TEXT NOT NULL UNIQUE,
    status             TEXT NOT NULL,
    filename           TEXT NOT NULL,
    extension          TEXT NOT NULL,
    size_bytes         INTEGER NOT NULL,
    duration_seconds   REAL,
    requested_language TEXT,
    language           TEXT,
    progress           REAL NOT NULL DEFAULT 0,
    attempts           INTEGER NOT NULL DEFAULT 0,
    error              TEXT,
    segments           TEXT,  -- JSON array of {start, end, text}
    created_at         TEXT NOT NULL,
    started_at         TEXT,
    finished_at        TEXT
);
CREATE INDEX IF NOT EXISTS jobs_by_status ON jobs (status, seq);
"""

# How long a connection waits for another connection's write to finish.
_LOCK_TIMEOUT_SECONDS = 5.0


class JobStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(_SCHEMA)

    def create(
        self,
        *,
        job_id: str,
        filename: str,
        extension: str,
        size_bytes: int,
        duration_seconds: float | None,
        requested_language: str | None,
    ) -> Job:
        with self._connect() as connection:
            row = connection.execute(
                """
                INSERT INTO jobs (id, status, filename, extension, size_bytes,
                                  duration_seconds, requested_language, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                RETURNING *
                """,
                (
                    job_id,
                    JobStatus.QUEUED,
                    filename,
                    extension,
                    size_bytes,
                    duration_seconds,
                    requested_language,
                    _now(),
                ),
            ).fetchone()
        return _to_job(row)

    def get(self, job_id: str) -> Job | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _to_job(row) if row else None

    def claim_next(self) -> Job | None:
        """Move the oldest queued job to 'processing' and return it, or None if the queue is empty.

        One UPDATE statement finds the job and claims it, so two workers can never
        claim the same job.
        """
        with self._connect() as connection:
            row = connection.execute(
                """
                UPDATE jobs
                   SET status = ?, attempts = attempts + 1, started_at = ?, progress = 0
                 WHERE seq = (SELECT seq FROM jobs WHERE status = ? ORDER BY seq LIMIT 1)
                RETURNING *
                """,
                (JobStatus.PROCESSING, _now(), JobStatus.QUEUED),
            ).fetchone()
        return _to_job(row) if row else None

    def update_progress(self, job_id: str, progress: float) -> None:
        with self._connect() as connection:
            connection.execute("UPDATE jobs SET progress = ? WHERE id = ?", (progress, job_id))

    def complete(self, job_id: str, transcript: Transcript, *, duration_seconds: float) -> None:
        segments = [
            {"start": segment.start, "end": segment.end, "text": segment.text}
            for segment in transcript.segments
        ]
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                   SET status = ?, progress = 1, language = ?, segments = ?,
                       duration_seconds = ?, finished_at = ?
                 WHERE id = ?
                """,
                (
                    JobStatus.COMPLETED,
                    transcript.language,
                    json.dumps(segments, ensure_ascii=False),
                    duration_seconds,
                    _now(),
                    job_id,
                ),
            )

    def fail(self, job_id: str, error: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET status = ?, error = ?, finished_at = ? WHERE id = ?",
                (JobStatus.FAILED, error, _now(), job_id),
            )

    def release(self, job_id: str) -> None:
        """Put a job that is being processed back in the queue, without counting the attempt.

        Used when the service shuts down in the middle of a job: that is not the job's fault.
        """
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                   SET status = ?, attempts = MAX(attempts - 1, 0), progress = 0, started_at = NULL
                 WHERE id = ? AND status = ?
                """,
                (JobStatus.QUEUED, job_id, JobStatus.PROCESSING),
            )

    def recover_interrupted(self, *, max_attempts: int) -> tuple[int, int]:
        """Deal with jobs left in 'processing' by a crash. Call once at startup.

        A job goes back to the queue, unless it has already been attempted
        ``max_attempts`` times: a file that takes the service down every time must
        not be retried forever. Returns (re-queued, failed) counts.
        """
        with self._connect() as connection:
            failed = connection.execute(
                """
                UPDATE jobs SET status = ?, error = ?, finished_at = ?
                 WHERE status = ? AND attempts >= ?
                """,
                (
                    JobStatus.FAILED,
                    "Processing was interrupted repeatedly, so this file was not retried again.",
                    _now(),
                    JobStatus.PROCESSING,
                    max_attempts,
                ),
            ).rowcount
            requeued = connection.execute(
                "UPDATE jobs SET status = ?, progress = 0, started_at = NULL WHERE status = ?",
                (JobStatus.QUEUED, JobStatus.PROCESSING),
            ).rowcount
        return requeued, failed

    def queue_position(self, job_id: str) -> int:
        """How many unfinished jobs arrived before this one."""
        with self._connect() as connection:
            (count,) = connection.execute(
                """
                SELECT COUNT(*) FROM jobs
                 WHERE status IN (?, ?) AND seq < (SELECT seq FROM jobs WHERE id = ?)
                """,
                (JobStatus.QUEUED, JobStatus.PROCESSING, job_id),
            ).fetchone()
        return count

    def count_unfinished(self) -> int:
        with self._connect() as connection:
            (count,) = connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE status IN (?, ?)",
                (JobStatus.QUEUED, JobStatus.PROCESSING),
            ).fetchone()
        return count

    def unfinished_jobs(self) -> list[Job]:
        """Jobs that are queued or being processed, oldest first."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs WHERE status IN (?, ?) ORDER BY seq",
                (JobStatus.QUEUED, JobStatus.PROCESSING),
            ).fetchall()
        return [_to_job(row) for row in rows]

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=_LOCK_TIMEOUT_SECONDS)
        connection.row_factory = sqlite3.Row
        try:
            with connection:  # commits on success, rolls back on an exception
                yield connection
        finally:
            connection.close()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _to_job(row: sqlite3.Row) -> Job:
    segments = row["segments"]
    return Job(
        id=row["id"],
        status=JobStatus(row["status"]),
        filename=row["filename"],
        extension=row["extension"],
        size_bytes=row["size_bytes"],
        duration_seconds=row["duration_seconds"],
        requested_language=row["requested_language"],
        language=row["language"],
        progress=row["progress"],
        attempts=row["attempts"],
        error=row["error"],
        segments=None if segments is None else [Segment(**item) for item in json.loads(segments)],
        created_at=datetime.fromisoformat(row["created_at"]),
        started_at=_parse_time(row["started_at"]),
        finished_at=_parse_time(row["finished_at"]),
    )


def _parse_time(value: str | None) -> datetime | None:
    return None if value is None else datetime.fromisoformat(value)
