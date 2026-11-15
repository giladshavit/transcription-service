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
# Decoded audio is one channel of 32-bit float samples.
_BYTES_PER_SECOND = SAMPLE_RATE * 4

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
    report = _probe(path)
    audio = _first_audio_stream(report)
    container = report.get("format", {})
    duration = _parse_duration(container.get("duration"))
    if duration is not None and duration > max_duration_seconds:
        raise UnprocessableMediaError(
            f"The recording is {_clock(duration)} long, "
            f"which is over the limit of {_clock(max_duration_seconds)}."
        )
    return MediaInfo(
        container=container.get("format_name", "unknown"),
        audio_codec=audio.get("codec_name", "unknown"),
        duration_seconds=duration,
    )


def _probe(path: Path) -> dict:
    """Run ffprobe on a file and return its report; raise if it is not readable media."""
    command = [
        "ffprobe", "-v", "error", *_INPUT_GUARDS,
        "-select_streams", "a:0",  # only the first audio stream
        "-show_entries", "format=format_name,duration:stream=codec_name,sample_rate,channels",
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
    return json.loads(completed.stdout)


def _first_audio_stream(report: dict) -> dict:
    """The audio stream in an ffprobe report; raise if there is none, or it is not real."""
    audio_streams = report.get("streams", [])
    if not audio_streams:
        raise UnprocessableMediaError("The file has no audio track.")
    # ffprobe can take random bytes for an audio stream, but then the stream has no
    # sample rate or no channels. Catch that here, not later in the worker.
    audio = audio_streams[0]
    if not (_is_positive(audio.get("sample_rate")) and _is_positive(audio.get("channels"))):
        raise UnprocessableMediaError("The file is not a readable audio or video file.")
    return audio


def decode_audio(path: Path, *, max_duration_seconds: float) -> np.ndarray:
    """Decode the first audio track of a media file to 16 kHz mono float32 samples.

    ffmpeg writes raw samples to a temporary file next to the input, which is loaded
    and then removed. The output is cut off just past the limit in two ways: by time
    (-t), and by size (-fs), because a crafted file can lie about its timestamps. A
    small upload therefore cannot expand into an unbounded amount of audio.
    """
    samples_path = path.with_suffix(".pcm")
    max_bytes = int(max_duration_seconds * _BYTES_PER_SECOND)
    try:
        _decode_to_file(path, samples_path, max_duration_seconds, max_bytes)
        if samples_path.stat().st_size > max_bytes:  # checked before loading it into memory
            raise UnprocessableMediaError(
                f"The recording is longer than the limit of {_clock(max_duration_seconds)}."
            )
        samples = np.fromfile(samples_path, dtype=np.float32)
    finally:
        samples_path.unlink(missing_ok=True)

    if samples.size == 0:
        raise UnprocessableMediaError("The file contains no audio.")
    return samples


def _decode_to_file(
    source: Path, samples_path: Path, max_duration_seconds: float, max_bytes: int
) -> None:
    """Have ffmpeg write the first audio track of a file as raw 16 kHz mono samples."""
    command = [
        "ffmpeg", "-nostdin", "-v", "error", "-y", *_INPUT_GUARDS,
        "-i", str(source),
        "-map", "0:a:0",  # only the first audio stream
        "-ac", "1", "-ar", str(SAMPLE_RATE),
        "-t", str(max_duration_seconds + 1),
        "-fs", str(max_bytes + _BYTES_PER_SECOND),
        "-f", "f32le", str(samples_path),
    ]  # fmt: skip
    try:
        completed = _run(command, timeout=_DECODE_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as error:
        raise UnprocessableMediaError("Decoding the file took too long.") from error
    if completed.returncode != 0:
        reason = _last_line(completed.stderr)
        logger.info("ffmpeg could not decode an upload", extra={"reason": reason})
        raise UnprocessableMediaError("The audio track could not be decoded.")


def _run(command: list[str], *, timeout: float) -> subprocess.CompletedProcess[str]:
    """Run ffprobe or ffmpeg and capture its output. The child is killed on timeout."""
    return subprocess.run(
        command, capture_output=True, text=True, errors="replace", timeout=timeout
    )


def _is_positive(value: object) -> bool:
    """True for a positive whole number, whether ffprobe wrote it as 44100 or as "44100"."""
    digits = str(value)
    return digits.isdigit() and int(digits) > 0


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
