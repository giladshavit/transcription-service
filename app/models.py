"""Domain objects shared by the store, the worker and the API."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class JobStatus(StrEnum):
    """A job only moves forward: queued -> processing -> completed or failed."""

    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class Segment:
    """A stretch of speech: start and end in seconds from the beginning of the recording."""

    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Transcript:
    """What a transcriber returns for one recording."""

    language: str
    segments: list[Segment]


@dataclass(frozen=True)
class Job:
    """One uploaded file and the state of its transcription."""

    id: str
    status: JobStatus
    filename: str  # the name the user uploaded; display only, never used as a path
    extension: str  # validated extension; the upload is stored as <id><extension>
    size_bytes: int
    duration_seconds: float | None
    requested_language: str | None  # None means "detect it"
    language: str | None
    progress: float  # 0.0 to 1.0
    attempts: int
    error: str | None
    segments: list[Segment] | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
