"""Shared test fixtures."""

import random
import shutil
import subprocess
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.fakes import FakeTranscriber


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings that keep all state in a per-test temp dir and ignore a local .env file."""
    return Settings(_env_file=None, data_dir=tmp_path / "data")


@pytest.fixture
def transcriber() -> FakeTranscriber:
    return FakeTranscriber()


@pytest.fixture
def make_client(
    settings: Settings, transcriber: FakeTranscriber
) -> Iterator[Callable[..., TestClient]]:
    """Start the whole application with a fake model; keyword arguments override settings."""
    with ExitStack() as running_apps:

        def make(**overrides: object) -> TestClient:
            custom_settings = settings.model_copy(update=overrides)
            return running_apps.enter_context(TestClient(create_app(custom_settings, transcriber)))

        yield make
    transcriber.resume()


@pytest.fixture
def client(make_client: Callable[..., TestClient]) -> TestClient:
    return make_client()


def _ffmpeg(*arguments: str, stdout: int | None = None) -> None:
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-y", *arguments], check=True, stdout=stdout
    )


@pytest.fixture(scope="session")
def samples(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A directory of small media files, generated once per test run with ffmpeg.

    Every recording is two seconds long.
    """
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed")

    root = tmp_path_factory.mktemp("samples")
    tone = ("-f", "lavfi", "-i", "sine=frequency=440:duration=2")
    picture = ("-f", "lavfi", "-i", "testsrc=duration=2:size=160x120:rate=10")

    _ffmpeg(*tone, "-ar", "44100", "-ac", "2", str(root / "tone.wav"))
    _ffmpeg(*picture, *tone, "-c:v", "mpeg4", "-c:a", "aac", "-shortest", str(root / "video.mp4"))
    _ffmpeg(*picture, "-c:v", "mpeg4", "-an", str(root / "silent_video.mp4"))

    # Written to a pipe, so the container cannot go back and record its duration.
    # Recordings made in a browser look like this.
    with (root / "no_duration.mkv").open("wb") as output:
        _ffmpeg(*tone, "-c:a", "flac", "-f", "matroska", "-", stdout=output.fileno())

    # Ten seconds of audio whose timestamps claim a tenth of a second: a file that
    # lies about its length to slip past a duration limit.
    _ffmpeg(
        "-f", "lavfi", "-i", "sine=frequency=440:duration=10", "-af", "asetpts=PTS/100",
        "-c:a", "flac", "-f", "matroska", str(root / "compressed_timestamps.mkv"),
    )  # fmt: skip

    # Random bytes: ffprobe "recognises" these as AAC and FLAC streams with no sample rate.
    noise = random.Random(0).randbytes(20_000)
    (root / "noise.aac").write_bytes(noise)
    (root / "noise.flac").write_bytes(noise)

    (root / "not_media.mp3").write_text("this is plain text, not audio\n")
    (root / "empty.mp3").write_bytes(b"")
    # An ffconcat script is a playlist: it tells ffmpeg to open the files it lists.
    (root / "concat_script.mp3").write_text("ffconcat version 1.0\nfile 'tone.wav'\n")
    return root
