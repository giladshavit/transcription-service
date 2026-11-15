"""Validation and decoding of uploaded media files.

Uploads are untrusted input. They are inspected and decoded by ffprobe and ffmpeg
in separate processes, with timeouts and with allow-lists for protocols and
container formats. A hostile or broken file therefore cannot hang or crash the
service, and cannot make ffmpeg open other files or network addresses.
"""

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import numpy as np

from app.errors import UnprocessableMediaError, UnsupportedMediaTypeError

logger = logging.getLogger(__name__)

# Whisper models expect mono audio sampled at 16 kHz.
SAMPLE_RATE = 16_000

# File types accepted at upload time.
ALLOWED_EXTENSIONS = frozenset(
    {
        # audio
        ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma",
        # video
        ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".wmv",
    }
)  # fmt: skip

# The ffmpeg demuxers behind those extensions. ffmpeg is told to refuse every other input
# format, which includes "playlist" formats (HLS, ffconcat) that make it open other files.
_ALLOWED_DEMUXERS = "aac,asf,avi,flac,matroska,mov,mp3,ogg,wav"

# Placed before the input of every ffprobe and ffmpeg call.
_INPUT_GUARDS = ("-protocol_whitelist", "file", "-format_whitelist", _ALLOWED_DEMUXERS)

_PROBE_TIMEOUT_SECONDS = 30
_DECODE_TIMEOUT_SECONDS = 600


@dataclass(frozen=True)
class MediaInfo:
    """What ffprobe found in an upload."""

    container: str
    audio_codec: str
    # None when the container does not declare a duration (browser recordings, for example).
    duration_seconds: float | None


def ensure_ffmpeg_available() -> None:
    """Fail at startup, with a clear message, if ffmpeg or ffprobe is not installed."""
    missing = [tool for tool in ("ffmpeg", "ffprobe") if shutil.which(tool) is None]
    if missing:
        raise RuntimeError(
            f"Required tools are not on the PATH: {', '.join(missing)}. Install ffmpeg."
        )


def validate_extension(filename: str) -> str:
    """Return the file's extension in lower case, or raise if the type is not supported.

    This is only a cheap first filter: the file's content is checked by ``inspect_media``.
    """
    extension = Path(filename).suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        supported = ", ".join(sorted(ALLOWED_EXTENSIONS))
        raise UnsupportedMediaTypeError(
            f"Unsupported file type {extension or '(no extension)'}. Supported types: {supported}."
        )
    return extension


def inspect_media(path: Path, *, max_duration_seconds: float) -> MediaInfo:
    """Check that a stored upload can be transcribed, and return what it contains.

    Raises UnprocessableMediaError if the file is not readable media, has no audio
    track, or declares a duration over the limit.
    """
    command = [
        "ffprobe", "-v", "error", *_INPUT_GUARDS,
        "-select_streams", "a:0",  # only the first audio stream
        "-show_entries", "format=format_name,duration:stream=codec_name",
        "-of", "json",
        str(path),
    ]  # fmt: skip
    try:
        completed = _run(command, timeout=_PROBE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as error:
        raise UnprocessableMediaError("The file could not be analysed in time.") from error
    if completed.returncode != 0:
        logger.info("ffprobe rejected an upload", extra={"reason": _last_line(completed.stderr)})
        raise UnprocessableMediaError("The file is not a readable audio or video file.")

    report = json.loads(completed.stdout)
    audio_streams = report.get("streams", [])
    if not audio_streams:
        raise UnprocessableMediaError("The file has no audio track.")

    container = report.get("format", {})
    duration = _parse_duration(container.get("duration"))
    if duration is not None and duration > max_duration_seconds:
        raise UnprocessableMediaError(
            f"The recording is {_clock(duration)} long, "
            f"which is over the limit of {_clock(max_duration_seconds)}."
        )
    return MediaInfo(
        container=container.get("format_name", "unknown"),
        audio_codec=audio_streams[0].get("codec_name", "unknown"),
        duration_seconds=duration,
    )


def decode_audio(path: Path, *, max_duration_seconds: float) -> np.ndarray:
    """Decode the first audio track of a media file to 16 kHz mono float32 samples.

    ffmpeg writes raw samples to a temporary file next to the input, which is loaded
    and then removed. Decoding stops just past the limit even if the file's metadata
    claims it is short, so a small file cannot expand into an unbounded amount of audio.
    """
    samples_path = path.with_suffix(".pcm")
    command = [
        "ffmpeg", "-nostdin", "-v", "error", "-y", *_INPUT_GUARDS,
        "-i", str(path),
        "-map", "0:a:0",  # only the first audio stream
        "-ac", "1", "-ar", str(SAMPLE_RATE),
        "-t", str(max_duration_seconds + 1),
        "-f", "f32le", str(samples_path),
    ]  # fmt: skip
    try:
        completed = _run(command, timeout=_DECODE_TIMEOUT_SECONDS)
        if completed.returncode != 0:
            reason = _last_line(completed.stderr)
            logger.info("ffmpeg could not decode an upload", extra={"reason": reason})
            raise UnprocessableMediaError("The audio track could not be decoded.")
        samples = np.fromfile(samples_path, dtype=np.float32)
    except subprocess.TimeoutExpired as error:
        raise UnprocessableMediaError("Decoding the file took too long.") from error
    finally:
        samples_path.unlink(missing_ok=True)

    if samples.size == 0:
        raise UnprocessableMediaError("The file contains no audio.")
    if samples.size > max_duration_seconds * SAMPLE_RATE:
        raise UnprocessableMediaError(
            f"The recording is longer than the limit of {_clock(max_duration_seconds)}."
        )
    return samples


def _run(command: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    """Run ffprobe or ffmpeg and capture its output. The child is killed on timeout."""
    return subprocess.run(
        command, capture_output=True, text=True, errors="replace", timeout=timeout
    )


def _parse_duration(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:  # ffprobe prints "N/A" when it cannot tell
        return None


def _clock(seconds: float) -> str:
    """Format a duration as H:MM:SS."""
    return str(timedelta(seconds=round(seconds)))


def _last_line(text: str) -> str:
    lines = text.strip().splitlines()
    return lines[-1] if lines else ""
