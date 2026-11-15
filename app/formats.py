"""Render a transcript in the download formats: plain text, SRT, WebVTT and JSON."""

import json
from collections.abc import Callable, Sequence
from enum import StrEnum

from app.models import Segment


class ExportFormat(StrEnum):
    TXT = "txt"
    SRT = "srt"
    VTT = "vtt"
    JSON = "json"


MEDIA_TYPES = {
    ExportFormat.TXT: "text/plain; charset=utf-8",
    ExportFormat.SRT: "application/x-subrip; charset=utf-8",
    ExportFormat.VTT: "text/vtt; charset=utf-8",
    ExportFormat.JSON: "application/json",
}


def render(export_format: ExportFormat, segments: Sequence[Segment], *, language: str) -> str:
    """Return the transcript as the text of a file in the given format."""
    return _RENDERERS[export_format](segments, language)


def plain_text(segments: Sequence[Segment]) -> str:
    """The whole transcript, one segment per line."""
    return "\n".join(segment.text for segment in segments)


def _txt(segments: Sequence[Segment], language: str) -> str:
    return plain_text(segments) + "\n" if segments else ""


def _srt(segments: Sequence[Segment], language: str) -> str:
    cues = [
        f"{number}\n"
        f"{_timestamp(segment.start, ',')} --> {_timestamp(segment.end, ',')}\n"
        f"{_single_line(segment.text)}\n"
        for number, segment in enumerate(segments, start=1)
    ]
    return "\n".join(cues)


def _vtt(segments: Sequence[Segment], language: str) -> str:
    cues = [
        f"{_timestamp(segment.start, '.')} --> {_timestamp(segment.end, '.')}\n"
        f"{_escape_vtt(_single_line(segment.text))}\n"
        for segment in segments
    ]
    return "\n".join(["WEBVTT\n", *cues])


def _json(segments: Sequence[Segment], language: str) -> str:
    document = {
        "language": language,
        "text": plain_text(segments),
        "segments": [
            {"start": segment.start, "end": segment.end, "text": segment.text}
            for segment in segments
        ],
    }
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


_RENDERERS: dict[ExportFormat, Callable[[Sequence[Segment], str], str]] = {
    ExportFormat.TXT: _txt,
    ExportFormat.SRT: _srt,
    ExportFormat.VTT: _vtt,
    ExportFormat.JSON: _json,
}


def _timestamp(seconds: float, millisecond_separator: str) -> str:
    """Format seconds as HH:MM:SS,mmm (SRT) or HH:MM:SS.mmm (WebVTT)."""
    total_milliseconds = round(seconds * 1000)
    hours, remainder = divmod(total_milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    whole_seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{whole_seconds:02d}{millisecond_separator}{milliseconds:03d}"


def _single_line(text: str) -> str:
    """Collapse line breaks: a blank line inside a cue would end it early."""
    return " ".join(text.split())


def _escape_vtt(text: str) -> str:
    """WebVTT reads &, < and > as markup; escaping > also defuses a literal "-->"."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
