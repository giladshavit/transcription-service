import json

import pytest

from app.formats import ExportFormat, render
from app.models import Segment

SEGMENTS = [
    Segment(0.0, 1.4, "Good afternoon everyone."),
    Segment(1.4, 4.76, "שלום לכולם"),
]


def test_plain_text_has_one_segment_per_line() -> None:
    assert render(ExportFormat.TXT, SEGMENTS, language="en") == (
        "Good afternoon everyone.\nשלום לכולם\n"
    )


def test_srt_numbers_the_cues_and_uses_a_comma_before_the_milliseconds() -> None:
    assert render(ExportFormat.SRT, SEGMENTS, language="en") == (
        "1\n"
        "00:00:00,000 --> 00:00:01,400\n"
        "Good afternoon everyone.\n"
        "\n"
        "2\n"
        "00:00:01,400 --> 00:00:04,760\n"
        "שלום לכולם\n"
    )


def test_vtt_starts_with_the_header_and_uses_a_dot_before_the_milliseconds() -> None:
    assert render(ExportFormat.VTT, SEGMENTS, language="en") == (
        "WEBVTT\n"
        "\n"
        "00:00:00.000 --> 00:00:01.400\n"
        "Good afternoon everyone.\n"
        "\n"
        "00:00:01.400 --> 00:00:04.760\n"
        "שלום לכולם\n"
    )


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0.0, "00:00:00,000"),
        (59.9994, "00:00:59,999"),
        (59.9996, "00:01:00,000"),  # rounding carries into the next unit
        (3661.5, "01:01:01,500"),
        (360_000.0, "100:00:00,000"),  # over 99 hours
    ],
)
def test_timestamps(seconds: float, expected: str) -> None:
    srt = render(ExportFormat.SRT, [Segment(seconds, seconds, "x")], language="en")

    assert srt.splitlines()[1] == f"{expected} --> {expected}"


def test_vtt_escapes_characters_that_would_be_read_as_markup_or_as_a_cue_arrow() -> None:
    vtt = render(ExportFormat.VTT, [Segment(0.0, 1.0, "if a < b & b --> c")], language="en")

    assert vtt.splitlines()[3] == "if a &lt; b &amp; b --&gt; c"


@pytest.mark.parametrize("export_format", [ExportFormat.SRT, ExportFormat.VTT])
def test_a_line_break_inside_a_segment_cannot_split_the_cue(export_format: ExportFormat) -> None:
    text = render(export_format, [Segment(0.0, 1.0, "first\n\nsecond")], language="en")

    assert "first second" in text


def test_json_contains_the_language_the_text_and_the_segments() -> None:
    document = json.loads(render(ExportFormat.JSON, SEGMENTS, language="he"))

    assert document == {
        "language": "he",
        "text": "Good afternoon everyone.\nשלום לכולם",
        "segments": [
            {"start": 0.0, "end": 1.4, "text": "Good afternoon everyone."},
            {"start": 1.4, "end": 4.76, "text": "שלום לכולם"},
        ],
    }


@pytest.mark.parametrize(
    ("export_format", "expected"),
    [(ExportFormat.TXT, ""), (ExportFormat.SRT, ""), (ExportFormat.VTT, "WEBVTT\n")],
)
def test_a_recording_without_speech_gives_a_valid_empty_file(
    export_format: ExportFormat, expected: str
) -> None:
    assert render(export_format, [], language="en") == expected
