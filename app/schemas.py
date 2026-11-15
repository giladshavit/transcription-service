"""The JSON shapes the API returns. They also appear in the generated API documentation."""

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, Field

from app.formats import plain_text
from app.models import Job, JobStatus


class SegmentResponse(BaseModel):
    start: float = Field(description="Seconds from the beginning of the recording")
    end: float
    text: str


class JobResponse(BaseModel):
    id: str
    status: JobStatus
    filename: str
    size_bytes: int
    duration_seconds: float | None = Field(description="Length of the recording, once known")
    language: str | None = Field(description="Requested language, or the detected one when done")
    progress: float = Field(description="Fraction of the recording transcribed, 0.0 to 1.0")
    queue_position: int | None = Field(description="Jobs ahead of this one; null once finished")
    error: str | None = Field(description="Why the job failed")
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    text: str | None = Field(description="The whole transcript; null until completed")
    segments: list[SegmentResponse] | None

    @classmethod
    def from_job(cls, job: Job, *, queue_position: int | None) -> Self:
        segments = job.segments
        return cls(
            id=job.id,
            status=job.status,
            filename=job.filename,
            size_bytes=job.size_bytes,
            duration_seconds=job.duration_seconds,
            language=job.language or job.requested_language,
            progress=job.progress,
            queue_position=queue_position,
            error=job.error,
            created_at=job.created_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
            text=None if segments is None else plain_text(segments),
            segments=None
            if segments is None
            else [SegmentResponse(start=s.start, end=s.end, text=s.text) for s in segments],
        )


class ErrorResponse(BaseModel):
    code: str = Field(description="Stable, machine-readable error code")
    message: str = Field(description="Explanation for a person")


class HealthResponse(BaseModel):
    status: Literal["ok", "unavailable"]
    model: str
    worker_alive: bool
    pending_jobs: int
