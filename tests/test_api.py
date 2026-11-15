import json
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.fakes import FakeTranscriber

FINISHED = ("completed", "failed")
# Generous on purpose: the wait ends as soon as the job gets there.
PATIENCE_SECONDS = 30.0


def upload(
    client: TestClient,
    samples: Path,
    source: str = "tone.wav",
    *,
    filename: str | None = None,
    language: str | None = None,
) -> httpx.Response:
    with (samples / source).open("rb") as media:
        return client.post(
            "/api/transcriptions",
            files={"file": (filename or source, media)},
            data={} if language is None else {"language": language},
        )


def wait_for(client: TestClient, job_id: str, statuses: tuple[str, ...] = FINISHED) -> dict:
    deadline = time.monotonic() + PATIENCE_SECONDS
    while time.monotonic() < deadline:
        job = client.get(f"/api/transcriptions/{job_id}").json()
        if job["status"] in statuses:
            return job
        time.sleep(0.01)
    raise AssertionError(f"job did not reach {statuses} in time")


def stored_uploads(settings: Settings) -> list[Path]:
    return list(settings.uploads_dir.iterdir())


# --- Creating a transcription -------------------------------------------------------------


def test_an_upload_is_accepted_and_transcribed_in_the_background(
    client: TestClient, samples: Path
) -> None:
    response = upload(client, samples)

    assert response.status_code == 202
    accepted = response.json()
    assert accepted["status"] == "queued"
    assert accepted["filename"] == "tone.wav"
    assert accepted["size_bytes"] == (samples / "tone.wav").stat().st_size
    assert response.headers["location"] == f"/api/transcriptions/{accepted['id']}"

    job = wait_for(client, accepted["id"])
    assert job["status"] == "completed"
    assert job["progress"] == 1.0
    assert job["language"] == "en"
    assert job["duration_seconds"] == pytest.approx(2.0, abs=0.05)
    assert job["text"] == "Hello world.\nSecond line."
    assert job["segments"] == [
        {"start": 0.0, "end": 1.2, "text": "Hello world."},
        {"start": 1.2, "end": 2.0, "text": "Second line."},
    ]
    assert job["error"] is None


def test_video_files_are_accepted(client: TestClient, samples: Path) -> None:
    response = upload(client, samples, "video.mp4")

    assert response.status_code == 202
    assert wait_for(client, response.json()["id"])["status"] == "completed"


def test_the_language_field_is_passed_to_the_model(
    client: TestClient, samples: Path, transcriber: FakeTranscriber
) -> None:
    response = upload(client, samples, language="he")

    job = wait_for(client, response.json()["id"])
    assert job["language"] == "he"
    assert transcriber.calls[0]["language"] == "he"


@pytest.mark.parametrize("language", ["", "auto"])
def test_no_language_means_automatic_detection(
    client: TestClient, samples: Path, transcriber: FakeTranscriber, language: str
) -> None:
    response = upload(client, samples, language=language)

    wait_for(client, response.json()["id"])
    assert transcriber.calls[0]["language"] is None


def test_a_language_the_model_does_not_know_is_rejected(
    client: TestClient, samples: Path, settings: Settings
) -> None:
    response = upload(client, samples, language="xx")

    assert response.status_code == 422
    assert response.json()["code"] == "unsupported_language"
    assert stored_uploads(settings) == []


def test_an_unsupported_file_type_is_rejected(
    client: TestClient, samples: Path, settings: Settings
) -> None:
    response = upload(client, samples, filename="notes.pdf")

    assert response.status_code == 415
    assert response.json()["code"] == "unsupported_media_type"
    assert ".mp3" in response.json()["message"]
    assert stored_uploads(settings) == []


@pytest.mark.parametrize("source", ["not_media.mp3", "empty.mp3", "silent_video.mp4"])
def test_a_file_that_cannot_be_transcribed_is_rejected_and_not_kept(
    client: TestClient, samples: Path, settings: Settings, source: str
) -> None:
    response = upload(client, samples, source)

    assert response.status_code == 422
    assert response.json()["code"] == "unprocessable_media"
    assert stored_uploads(settings) == []


def test_a_request_without_a_file_is_rejected_in_the_common_error_format(
    client: TestClient,
) -> None:
    response = client.post("/api/transcriptions", data={"language": "en"})

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_request"
    assert "file" in response.json()["message"]


@pytest.mark.parametrize(
    ("sent_name", "shown_name"),
    [
        ("../../etc/passwd.wav", "passwd.wav"),
        ("C:\\Users\\me\\talk.wav", "talk.wav"),
        ("שיחה עם לקוח.wav", "שיחה עם לקוח.wav"),
    ],
)
def test_only_the_base_name_of_the_upload_is_kept_and_never_used_as_a_path(
    client: TestClient, samples: Path, settings: Settings, sent_name: str, shown_name: str
) -> None:
    response = upload(client, samples, filename=sent_name)

    assert response.status_code == 202
    assert response.json()["filename"] == shown_name
    job_id = response.json()["id"]
    assert all(path.name == f"{job_id}.wav" for path in stored_uploads(settings))


def test_an_upload_over_the_size_limit_is_rejected_before_it_is_read(
    make_client: Callable[..., TestClient], settings: Settings
) -> None:
    client = make_client(max_upload_mb=1)
    too_big = b"\0" * (2 * 1024 * 1024)

    response = client.post("/api/transcriptions", files={"file": ("big.wav", too_big)})

    assert response.status_code == 413
    assert response.json()["code"] == "file_too_large"
    assert stored_uploads(settings) == []


def test_the_size_limit_also_holds_when_the_client_does_not_declare_a_length(
    make_client: Callable[..., TestClient], settings: Settings
) -> None:
    client = make_client(max_upload_mb=1)
    boundary = "test-boundary"
    head = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="big.wav"\r\n'
        "Content-Type: audio/wav\r\n\r\n"
    ).encode()

    def body() -> Iterator[bytes]:  # a generator body is sent chunked, without Content-Length
        yield head
        for _ in range(4):
            yield b"\0" * (512 * 1024)
        yield f"\r\n--{boundary}--\r\n".encode()

    response = client.post(
        "/api/transcriptions",
        content=body(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )

    assert response.status_code == 413
    assert response.json()["code"] == "file_too_large"
    assert stored_uploads(settings) == []


def test_uploads_are_refused_while_too_many_jobs_are_pending(
    make_client: Callable[..., TestClient], samples: Path, transcriber: FakeTranscriber
) -> None:
    client = make_client(max_pending_jobs=1)
    transcriber.hold()
    first = upload(client, samples)

    refused = upload(client, samples)

    assert refused.status_code == 503
    assert refused.json()["code"] == "queue_full"
    assert int(refused.headers["retry-after"]) > 0

    transcriber.resume()
    wait_for(client, first.json()["id"])
    assert upload(client, samples).status_code == 202


# --- Reading a transcription ----------------------------------------------------------------


def test_an_unknown_job_id_gives_404(client: TestClient) -> None:
    response = client.get("/api/transcriptions/does-not-exist")

    assert response.status_code == 404
    assert response.json()["code"] == "job_not_found"


def test_a_waiting_job_reports_how_many_jobs_are_ahead_of_it(
    client: TestClient, samples: Path, transcriber: FakeTranscriber
) -> None:
    transcriber.hold()
    first = upload(client, samples).json()["id"]
    second = upload(client, samples).json()["id"]

    assert wait_for(client, first, ("processing",))["queue_position"] == 0
    assert client.get(f"/api/transcriptions/{second}").json()["queue_position"] == 1


def test_a_failed_job_reports_the_reason(
    client: TestClient, samples: Path, transcriber: FakeTranscriber
) -> None:
    transcriber.error = RuntimeError("boom")

    job = wait_for(client, upload(client, samples).json()["id"])

    assert job["status"] == "failed"
    assert job["error"] == "Transcription failed because of an internal error."
    assert job["segments"] is None


def test_api_responses_must_not_be_cached(client: TestClient) -> None:
    response = client.get("/api/transcriptions/does-not-exist")

    assert response.headers["cache-control"] == "no-store"


# --- Downloading ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("export_format", "media_type", "first_line"),
    [
        ("txt", "text/plain; charset=utf-8", "Hello world."),
        ("srt", "application/x-subrip; charset=utf-8", "1"),
        ("vtt", "text/vtt; charset=utf-8", "WEBVTT"),
        ("json", "application/json", "{"),
    ],
)
def test_a_finished_transcript_can_be_downloaded_in_each_format(
    client: TestClient, samples: Path, export_format: str, media_type: str, first_line: str
) -> None:
    job_id = upload(client, samples).json()["id"]
    wait_for(client, job_id)

    response = client.get(f"/api/transcriptions/{job_id}/download?format={export_format}")

    assert response.status_code == 200
    assert response.headers["content-type"] == media_type
    assert response.headers["content-disposition"].startswith(
        f'attachment; filename="tone.{export_format}"'
    )
    assert response.text.splitlines()[0] == first_line


def test_the_json_download_is_a_complete_document(client: TestClient, samples: Path) -> None:
    job_id = upload(client, samples).json()["id"]
    wait_for(client, job_id)

    document = json.loads(client.get(f"/api/transcriptions/{job_id}/download?format=json").text)

    assert document["language"] == "en"
    assert [segment["text"] for segment in document["segments"]] == ["Hello world.", "Second line."]


def test_the_download_name_keeps_a_non_ascii_file_name(client: TestClient, samples: Path) -> None:
    job_id = upload(client, samples, filename="שיחה.wav").json()["id"]
    wait_for(client, job_id)

    disposition = client.get(f"/api/transcriptions/{job_id}/download?format=srt").headers[
        "content-disposition"
    ]

    # An ASCII fallback for old clients, and the real name percent-encoded as UTF-8 (RFC 6266).
    assert 'filename="transcript.srt"' in disposition
    assert "filename*=UTF-8''%D7%A9%D7%99%D7%97%D7%94.srt" in disposition


def test_downloading_before_the_transcript_is_ready_gives_409(
    client: TestClient, samples: Path, transcriber: FakeTranscriber
) -> None:
    transcriber.hold()
    job_id = upload(client, samples).json()["id"]

    response = client.get(f"/api/transcriptions/{job_id}/download?format=txt")

    assert response.status_code == 409
    assert response.json()["code"] == "job_not_ready"


def test_downloading_an_unknown_job_gives_404(client: TestClient) -> None:
    response = client.get("/api/transcriptions/does-not-exist/download?format=txt")

    assert response.status_code == 404


def test_an_unknown_download_format_is_rejected(client: TestClient, samples: Path) -> None:
    job_id = upload(client, samples).json()["id"]
    wait_for(client, job_id)

    response = client.get(f"/api/transcriptions/{job_id}/download?format=docx")

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_request"


# --- Operations -------------------------------------------------------------------------------


def test_health_reports_the_model_and_the_worker(client: TestClient, settings: Settings) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "model": settings.whisper_model,
        "worker_alive": True,
        "pending_jobs": 0,
    }


def test_health_fails_when_the_worker_has_died(client: TestClient) -> None:
    client.app.state.worker.stop()

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"


def test_unknown_routes_use_the_common_error_format(client: TestClient) -> None:
    response = client.get("/api/nope")

    assert response.status_code == 404
    assert response.json() == {"code": "not_found", "message": "Not Found"}


def test_startup_fails_when_ffmpeg_is_missing(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.media.shutil.which", lambda name: None)
    app = create_app(settings, FakeTranscriber())

    with pytest.raises(RuntimeError, match="ffmpeg"), TestClient(app):
        pass
