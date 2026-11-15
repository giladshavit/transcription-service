import shutil
from pathlib import Path

import numpy as np
import pytest

from app.errors import UnprocessableMediaError, UnsupportedMediaTypeError
from app.media import decode_audio, ensure_ffmpeg_available, inspect_media, validate_extension


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("lecture.mp3", ".mp3"),
        ("Interview.MP4", ".mp4"),
        ("voice note 12.opus", ".opus"),
        ("archive.tar.wav", ".wav"),
        ("שיחה.m4a", ".m4a"),
    ],
)
def test_validate_extension_returns_the_normalised_extension(filename: str, expected: str) -> None:
    assert validate_extension(filename) == expected


@pytest.mark.parametrize(
    "filename",
    ["report.pdf", "song.mp3.exe", "playlist.m3u8", "no-extension", ".mp3", ""],
)
def test_validate_extension_rejects_unsupported_types(filename: str) -> None:
    with pytest.raises(UnsupportedMediaTypeError) as raised:
        validate_extension(filename)

    assert raised.value.status_code == 415


def test_inspect_media_reports_what_the_file_contains(samples: Path) -> None:
    info = inspect_media(samples / "tone.wav", max_duration_seconds=60)

    assert info.container == "wav"
    assert info.audio_codec == "pcm_s16le"
    assert info.duration_seconds == pytest.approx(2.0, abs=0.05)


def test_inspect_media_accepts_video_with_an_audio_track(samples: Path) -> None:
    info = inspect_media(samples / "video.mp4", max_duration_seconds=60)

    assert info.audio_codec == "aac"
    assert info.duration_seconds == pytest.approx(2.0, abs=0.2)


def test_inspect_media_accepts_a_file_that_does_not_declare_its_duration(samples: Path) -> None:
    info = inspect_media(samples / "no_duration.mkv", max_duration_seconds=60)

    assert info.duration_seconds is None


@pytest.mark.parametrize("name", ["not_media.mp3", "empty.mp3", "noise.aac", "noise.flac"])
def test_inspect_media_rejects_files_that_are_not_media(samples: Path, name: str) -> None:
    with pytest.raises(UnprocessableMediaError) as raised:
        inspect_media(samples / name, max_duration_seconds=60)

    assert raised.value.status_code == 422


def test_inspect_media_rejects_video_without_an_audio_track(samples: Path) -> None:
    with pytest.raises(UnprocessableMediaError, match="no audio"):
        inspect_media(samples / "silent_video.mp4", max_duration_seconds=60)


def test_inspect_media_rejects_recordings_over_the_duration_limit(samples: Path) -> None:
    with pytest.raises(UnprocessableMediaError, match="limit"):
        inspect_media(samples / "tone.wav", max_duration_seconds=1)


def test_inspect_media_does_not_follow_references_to_other_files(samples: Path) -> None:
    with pytest.raises(UnprocessableMediaError):
        inspect_media(samples / "concat_script.mp3", max_duration_seconds=60)


def test_decode_audio_returns_mono_16khz_float_samples(samples: Path) -> None:
    audio = decode_audio(samples / "tone.wav", max_duration_seconds=60)

    assert audio.dtype == np.float32
    assert audio.ndim == 1  # the stereo source was mixed down to one channel
    assert len(audio) == pytest.approx(32_000, abs=800)  # 2 seconds at 16 kHz
    assert 0.05 < np.abs(audio).max() <= 1.0  # an audible tone, not silence


def test_decode_audio_extracts_the_audio_track_of_a_video(samples: Path) -> None:
    audio = decode_audio(samples / "video.mp4", max_duration_seconds=60)

    assert len(audio) == pytest.approx(32_000, abs=3_200)


def test_decode_audio_enforces_the_limit_when_the_file_hides_its_duration(samples: Path) -> None:
    with pytest.raises(UnprocessableMediaError, match="limit"):
        decode_audio(samples / "no_duration.mkv", max_duration_seconds=1)


def test_decode_audio_enforces_the_limit_when_the_timestamps_lie(samples: Path) -> None:
    # The file declares 0.1 seconds and holds 10, so neither its declared duration nor
    # its timestamps can be trusted: what counts is how much audio comes out.
    with pytest.raises(UnprocessableMediaError, match="limit"):
        decode_audio(samples / "compressed_timestamps.mkv", max_duration_seconds=2)


def test_decode_audio_rejects_a_file_that_cannot_be_decoded(samples: Path) -> None:
    with pytest.raises(UnprocessableMediaError):
        decode_audio(samples / "not_media.mp3", max_duration_seconds=60)


def test_decode_audio_leaves_no_temporary_files_after_success(
    samples: Path, tmp_path: Path
) -> None:
    upload = shutil.copy(samples / "tone.wav", tmp_path / "upload.wav")

    decode_audio(upload, max_duration_seconds=60)

    assert [path.name for path in tmp_path.iterdir()] == ["upload.wav"]


def test_decode_audio_leaves_no_temporary_files_after_failure(
    samples: Path, tmp_path: Path
) -> None:
    upload = shutil.copy(samples / "no_duration.mkv", tmp_path / "upload.mkv")

    with pytest.raises(UnprocessableMediaError):
        decode_audio(upload, max_duration_seconds=1)

    assert [path.name for path in tmp_path.iterdir()] == ["upload.mkv"]


def test_ensure_ffmpeg_available_fails_when_the_tools_are_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.media.shutil.which", lambda name: None)

    with pytest.raises(RuntimeError, match="ffmpeg"):
        ensure_ffmpeg_available()
