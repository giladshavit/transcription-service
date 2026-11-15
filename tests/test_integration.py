"""The whole service with the real speech-to-text model.

Slow, and the first run downloads the model, so these tests are opt-in:
``make test-integration`` (or ``pytest -m integration``).
"""

import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

pytestmark = pytest.mark.integration

SAMPLE = Path(__file__).parent / "data" / "jfk.mp3"


@pytest.fixture(scope="module")
def client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    settings = Settings(_env_file=None, data_dir=tmp_path_factory.mktemp("data"))
    with TestClient(create_app(settings)) as real_client:  # no fake: this loads the model
        yield real_client


def transcribe(client: TestClient, timeout_seconds: float = 300) -> dict:
    with SAMPLE.open("rb") as media:
        accepted = client.post("/api/transcriptions", files={"file": ("jfk.mp3", media)})
    assert accepted.status_code == 202
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        job = client.get(accepted.headers["location"]).json()
        if job["status"] in ("completed", "failed"):
            return job
        time.sleep(0.5)
    raise AssertionError("the transcription did not finish in time")


def test_a_real_recording_is_transcribed(client: TestClient) -> None:
    job = transcribe(client)

    assert job["status"] == "completed", job["error"]
    assert job["language"] == "en"
    assert job["duration_seconds"] == pytest.approx(11.0, abs=0.5)
    # The exact wording can differ between model versions and machines, so check for
    # the phrases that any reasonable transcript of this speech contains.
    text = job["text"].lower()
    for phrase in ("my fellow americans", "ask not what your country can do for you"):
        assert phrase in text
    assert all(0 <= segment["start"] < segment["end"] <= 11.5 for segment in job["segments"])


def test_the_subtitle_export_of_a_real_transcript_is_well_formed(client: TestClient) -> None:
    job = transcribe(client)

    srt = client.get(f"/api/transcriptions/{job['id']}/download?format=srt").text

    first_cue = srt.split("\n\n")[0].splitlines()
    assert first_cue[0] == "1"
    assert " --> " in first_cue[1]
    assert first_cue[2].strip()
