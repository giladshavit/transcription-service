import threading
from pathlib import Path

import pytest

from app.models import Job, JobStatus, Segment, Transcript
from app.store import JobStore


@pytest.fixture
def store(tmp_path: Path) -> JobStore:
    return JobStore(tmp_path / "jobs.sqlite3")


def add_job(store: JobStore, job_id: str, **overrides: object) -> Job:
    fields: dict = {
        "filename": f"{job_id}.mp3",
        "extension": ".mp3",
        "size_bytes": 1234,
        "duration_seconds": 60.0,
        "requested_language": None,
    }
    fields.update(overrides)
    return store.create(job_id=job_id, **fields)


def test_a_new_job_is_queued_and_can_be_read_back(store: JobStore) -> None:
    created = add_job(store, "a", filename="שיחה.mp3", requested_language="he")

    job = store.get("a")

    assert job == created
    assert job.status is JobStatus.QUEUED
    assert job.filename == "שיחה.mp3"
    assert job.requested_language == "he"
    assert job.progress == 0.0
    assert job.attempts == 0
    assert job.segments is None
    assert job.created_at.tzinfo is not None


def test_get_returns_none_for_an_unknown_id(store: JobStore) -> None:
    assert store.get("missing") is None


def test_jobs_survive_a_restart(tmp_path: Path) -> None:
    add_job(JobStore(tmp_path / "jobs.sqlite3"), "a")

    reopened = JobStore(tmp_path / "jobs.sqlite3")

    assert reopened.get("a").status is JobStatus.QUEUED


def test_claim_next_hands_out_jobs_in_arrival_order(store: JobStore) -> None:
    add_job(store, "first")
    add_job(store, "second")

    claimed = store.claim_next()

    assert claimed.id == "first"
    assert claimed.status is JobStatus.PROCESSING
    assert claimed.attempts == 1
    assert claimed.started_at is not None
    assert store.claim_next().id == "second"
    assert store.claim_next() is None


def test_a_job_is_never_claimed_twice_by_concurrent_workers(store: JobStore) -> None:
    for number in range(20):
        add_job(store, f"job-{number}")
    claimed: list[str] = []

    def claim_until_empty() -> None:
        while (job := store.claim_next()) is not None:
            claimed.append(job.id)

    workers = [threading.Thread(target=claim_until_empty) for _ in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    assert sorted(claimed) == sorted(f"job-{number}" for number in range(20))


def test_progress_updates_are_visible(store: JobStore) -> None:
    add_job(store, "a")
    store.claim_next()

    store.update_progress("a", 0.4)

    assert store.get("a").progress == 0.4


def test_complete_stores_the_transcript(store: JobStore) -> None:
    add_job(store, "a", duration_seconds=None)
    store.claim_next()
    transcript = Transcript(
        language="he",
        segments=[Segment(0.0, 1.5, "שלום לכולם"), Segment(1.5, 3.0, 'He said "hi"')],
    )

    store.complete("a", transcript, duration_seconds=3.0)

    job = store.get("a")
    assert job.status is JobStatus.COMPLETED
    assert job.progress == 1.0
    assert job.language == "he"
    assert job.duration_seconds == 3.0
    assert job.segments == transcript.segments
    assert job.finished_at is not None


def test_fail_stores_the_reason(store: JobStore) -> None:
    add_job(store, "a")
    store.claim_next()

    store.fail("a", "The file has no audio track.")

    job = store.get("a")
    assert job.status is JobStatus.FAILED
    assert job.error == "The file has no audio track."
    assert job.finished_at is not None


def test_release_returns_a_job_to_the_front_of_the_queue_without_using_an_attempt(
    store: JobStore,
) -> None:
    add_job(store, "first")
    add_job(store, "second")
    store.claim_next()
    store.update_progress("first", 0.7)

    store.release("first")

    job = store.get("first")
    assert job.status is JobStatus.QUEUED
    assert job.attempts == 0
    assert job.progress == 0.0
    assert store.claim_next().id == "first"


def test_recover_interrupted_requeues_jobs_that_were_processing(store: JobStore) -> None:
    add_job(store, "a")
    store.claim_next()  # the service "crashes" here

    store.recover_interrupted(max_attempts=2)

    assert store.get("a").status is JobStatus.QUEUED


def test_recover_interrupted_gives_up_on_a_job_that_keeps_crashing_the_service(
    store: JobStore,
) -> None:
    add_job(store, "poison")
    store.claim_next()
    store.recover_interrupted(max_attempts=2)
    store.claim_next()  # second attempt, and the service "crashes" again

    store.recover_interrupted(max_attempts=2)

    job = store.get("poison")
    assert job.status is JobStatus.FAILED
    assert job.error is not None


def test_queue_position_counts_the_unfinished_jobs_ahead(store: JobStore) -> None:
    add_job(store, "a")
    add_job(store, "b")
    add_job(store, "c")
    store.claim_next()  # "a" is now processing

    assert store.queue_position("a") == 0
    assert store.queue_position("b") == 1
    assert store.queue_position("c") == 2

    store.fail("a", "broken")

    assert store.queue_position("c") == 1


def test_count_unfinished_ignores_completed_and_failed_jobs(store: JobStore) -> None:
    add_job(store, "a")
    add_job(store, "b")
    add_job(store, "c")
    store.claim_next()
    store.fail("a", "broken")

    assert store.count_unfinished() == 2


def test_unfinished_jobs_lists_what_still_needs_its_upload(store: JobStore) -> None:
    add_job(store, "a")
    add_job(store, "b", extension=".mp4")
    store.claim_next()
    store.complete("a", Transcript(language="en", segments=[]), duration_seconds=1.0)

    assert [(job.id, job.extension) for job in store.unfinished_jobs()] == [("b", ".mp4")]
