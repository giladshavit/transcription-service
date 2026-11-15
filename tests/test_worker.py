import shutil
import sqlite3
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from app.models import Job, JobStatus
from app.store import JobStore
from app.worker import TranscriptionWorker
from tests.fakes import FakeTranscriber

# Waits return as soon as their condition holds, so a generous limit costs nothing on a
# fast machine and keeps the tests reliable on a busy one.
PATIENCE_SECONDS = 30.0


def wait_until(condition: Callable[[], bool], timeout: float = PATIENCE_SECONDS) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition was not met in time")
        time.sleep(0.01)


@pytest.fixture
def store(tmp_path: Path) -> JobStore:
    return JobStore(tmp_path / "jobs.sqlite3")


@pytest.fixture
def uploads(tmp_path: Path) -> Path:
    directory = tmp_path / "uploads"
    directory.mkdir()
    return directory


@pytest.fixture
def worker(
    store: JobStore, uploads: Path, transcriber: FakeTranscriber
) -> Iterator[TranscriptionWorker]:
    worker = make_worker(store, uploads, transcriber)
    yield worker
    transcriber.resume()
    worker.stop()


def make_worker(
    store: JobStore, uploads: Path, transcriber: FakeTranscriber, poll_seconds: float = 0.05
) -> TranscriptionWorker:
    return TranscriptionWorker(
        store=store,
        transcriber=transcriber,
        uploads_dir=uploads,
        max_duration_seconds=60,
        max_attempts=2,
        poll_seconds=poll_seconds,
    )


def add_upload(
    store: JobStore,
    uploads: Path,
    samples: Path,
    job_id: str,
    source: str = "tone.wav",
    language: str | None = None,
) -> Job:
    """Do what the API does: store the file under the job id, then create the job."""
    extension = Path(source).suffix
    shutil.copy(samples / source, uploads / f"{job_id}{extension}")
    return store.create(
        job_id=job_id,
        filename=source,
        extension=extension,
        size_bytes=(samples / source).stat().st_size,
        duration_seconds=None,
        requested_language=language,
    )


def finished(store: JobStore, job_id: str) -> Callable[[], bool]:
    return lambda: store.get(job_id).status in (JobStatus.COMPLETED, JobStatus.FAILED)


def test_a_queued_job_is_transcribed(
    worker: TranscriptionWorker,
    store: JobStore,
    uploads: Path,
    samples: Path,
    transcriber: FakeTranscriber,
) -> None:
    worker.start()
    add_upload(store, uploads, samples, "a" * 32)
    worker.notify()

    wait_until(finished(store, "a" * 32))

    job = store.get("a" * 32)
    assert job.status is JobStatus.COMPLETED
    assert job.segments == transcriber.segments
    assert job.language == "en"
    assert job.progress == 1.0
    assert job.duration_seconds == pytest.approx(2.0, abs=0.05)
    assert transcriber.calls[0]["samples"] == pytest.approx(32_000, abs=800)


def test_the_upload_is_deleted_once_the_job_is_finished(
    worker: TranscriptionWorker, store: JobStore, uploads: Path, samples: Path
) -> None:
    worker.start()
    add_upload(store, uploads, samples, "a" * 32)
    worker.notify()

    wait_until(finished(store, "a" * 32))

    wait_until(lambda: list(uploads.iterdir()) == [])


def test_the_requested_language_reaches_the_transcriber(
    worker: TranscriptionWorker,
    store: JobStore,
    uploads: Path,
    samples: Path,
    transcriber: FakeTranscriber,
) -> None:
    worker.start()
    add_upload(store, uploads, samples, "a" * 32, language="he")
    worker.notify()

    wait_until(finished(store, "a" * 32))

    assert transcriber.calls[0]["language"] == "he"
    assert store.get("a" * 32).language == "he"


def test_a_job_queued_while_the_service_was_down_is_picked_up(
    worker: TranscriptionWorker, store: JobStore, uploads: Path, samples: Path
) -> None:
    add_upload(store, uploads, samples, "a" * 32)

    worker.start()  # no notify(): the worker finds the job in the table

    wait_until(finished(store, "a" * 32))
    assert store.get("a" * 32).status is JobStatus.COMPLETED


def test_notify_wakes_an_idle_worker_without_waiting_for_the_poll_interval(
    store: JobStore, uploads: Path, samples: Path, transcriber: FakeTranscriber
) -> None:
    worker = make_worker(store, uploads, transcriber, poll_seconds=60)
    worker.start()
    time.sleep(0.1)  # let the worker find the queue empty and go to sleep
    add_upload(store, uploads, samples, "a" * 32)

    worker.notify()

    wait_until(finished(store, "a" * 32))  # far sooner than the 60-second poll interval
    worker.stop()


def test_a_file_that_cannot_be_decoded_fails_with_a_message_for_the_user(
    worker: TranscriptionWorker, store: JobStore, uploads: Path, samples: Path
) -> None:
    worker.start()
    add_upload(store, uploads, samples, "a" * 32, source="not_media.mp3")
    worker.notify()

    wait_until(finished(store, "a" * 32))

    job = store.get("a" * 32)
    assert job.status is JobStatus.FAILED
    assert job.error == "The audio track could not be decoded."
    wait_until(lambda: list(uploads.iterdir()) == [])


def test_an_unexpected_error_fails_the_job_without_leaking_details_and_the_worker_carries_on(
    worker: TranscriptionWorker,
    store: JobStore,
    uploads: Path,
    samples: Path,
    transcriber: FakeTranscriber,
) -> None:
    transcriber.error = RuntimeError("stack details that users must not see")
    worker.start()
    add_upload(store, uploads, samples, "a" * 32)
    worker.notify()
    wait_until(finished(store, "a" * 32))

    transcriber.error = None
    add_upload(store, uploads, samples, "b" * 32)
    worker.notify()
    wait_until(finished(store, "b" * 32))

    failed = store.get("a" * 32)
    assert failed.status is JobStatus.FAILED
    assert "stack details" not in failed.error
    assert store.get("b" * 32).status is JobStatus.COMPLETED


def test_stopping_mid_job_puts_the_job_back_in_the_queue_for_the_next_start(
    store: JobStore, uploads: Path, samples: Path, transcriber: FakeTranscriber
) -> None:
    first = make_worker(store, uploads, transcriber)
    first.start()
    transcriber.hold()
    add_upload(store, uploads, samples, "a" * 32)
    first.notify()
    assert transcriber.started.wait(timeout=PATIENCE_SECONDS)

    first.stop()

    job = store.get("a" * 32)
    assert job.status is JobStatus.QUEUED
    assert job.attempts == 0  # a shutdown is not the job's fault
    assert (uploads / f"{'a' * 32}.wav").exists()

    transcriber.resume()
    second = make_worker(store, uploads, transcriber)
    second.start()
    wait_until(finished(store, "a" * 32))
    second.stop()
    assert store.get("a" * 32).status is JobStatus.COMPLETED


def test_start_retries_a_job_that_was_interrupted_by_a_crash(
    worker: TranscriptionWorker, store: JobStore, uploads: Path, samples: Path
) -> None:
    add_upload(store, uploads, samples, "a" * 32)
    store.claim_next()  # a previous run claimed the job and then died

    worker.start()

    wait_until(finished(store, "a" * 32))
    job = store.get("a" * 32)
    assert job.status is JobStatus.COMPLETED
    assert job.attempts == 2


def test_start_removes_uploads_that_no_unfinished_job_needs(
    worker: TranscriptionWorker,
    store: JobStore,
    uploads: Path,
    samples: Path,
    transcriber: FakeTranscriber,
) -> None:
    transcriber.hold()  # keep the queued job from finishing during the test
    add_upload(store, uploads, samples, "a" * 32)
    orphan = uploads / f"{'b' * 32}.mp4"
    leftover_samples = uploads / f"{'c' * 32}.pcm"
    unrelated = uploads / "notes.txt"
    for path in (orphan, leftover_samples, unrelated):
        path.write_bytes(b"x")

    worker.start()

    assert not orphan.exists()
    assert not leftover_samples.exists()
    assert unrelated.exists()  # only files that follow the upload naming scheme are touched
    assert (uploads / f"{'a' * 32}.wav").exists()


def test_a_database_error_does_not_kill_the_worker(
    tmp_path: Path, uploads: Path, samples: Path, transcriber: FakeTranscriber
) -> None:
    class FlakyStore(JobStore):
        failures_left = 2

        def claim_next(self) -> Job | None:
            if self.failures_left > 0:
                self.failures_left -= 1
                raise sqlite3.OperationalError("database is locked")
            return super().claim_next()

    store = FlakyStore(tmp_path / "jobs.sqlite3")
    add_upload(store, uploads, samples, "a" * 32)
    worker = make_worker(store, uploads, transcriber)

    worker.start()

    wait_until(finished(store, "a" * 32))
    assert worker.is_alive
    worker.stop()
    assert not worker.is_alive
