import numpy as np
import pytest

from app.media import SAMPLE_RATE
from app.models import Segment
from app.transcriber import transcribe_in_windows


def recording(seconds: float) -> np.ndarray:
    return np.full(int(seconds * SAMPLE_RATE), 0.5, dtype=np.float32)


class ScriptedModel:
    """Answers each window with scripted segments, and remembers what it was asked."""

    def __init__(self, *windows: tuple[str, list[Segment]]) -> None:
        self._answers = list(windows)
        self.requested_languages: list[str | None] = []
        self.window_seconds: list[float] = []

    def __call__(self, audio: np.ndarray, language: str | None) -> tuple[list[Segment], str]:
        self.requested_languages.append(language)
        self.window_seconds.append(len(audio) / SAMPLE_RATE)
        detected, segments = self._answers.pop(0)
        return segments, detected


def run(model: ScriptedModel, seconds: float, language: str | None = None) -> tuple:
    progress: list[float] = []
    transcript = transcribe_in_windows(
        recording(seconds),
        language=language,
        on_progress=progress.append,
        transcribe_window=model,
        window_seconds=60,
        search_seconds=10,
    )
    return transcript, progress


def test_segment_times_are_shifted_by_the_start_of_their_window() -> None:
    model = ScriptedModel(
        ("en", [Segment(0.0, 2.0, "first window")]),
        ("en", [Segment(1.5, 3.0, "second window")]),
    )

    transcript, _ = run(model, seconds=100)

    second = transcript.segments[1]
    window_start = model.window_seconds[0]  # the first window ended where the second began
    assert second.start == pytest.approx(window_start + 1.5, abs=0.01)
    assert second.end == pytest.approx(window_start + 3.0, abs=0.01)
    assert [segment.text for segment in transcript.segments] == ["first window", "second window"]


def test_the_language_is_detected_once_and_kept_for_the_rest_of_the_recording() -> None:
    model = ScriptedModel(
        ("he", [Segment(0.0, 2.0, "שלום")]),
        ("en", [Segment(0.0, 2.0, "a window that would be mis-detected on its own")]),
    )

    transcript, _ = run(model, seconds=100)

    assert model.requested_languages == [None, "he"]
    assert transcript.language == "he"


def test_a_window_without_speech_does_not_decide_the_language() -> None:
    model = ScriptedModel(
        ("en", []),  # silence or music: whatever was "detected" here means nothing
        ("he", [Segment(0.0, 2.0, "שלום")]),
    )

    transcript, _ = run(model, seconds=100)

    assert model.requested_languages == [None, None]
    assert transcript.language == "he"


def test_a_requested_language_is_used_for_every_window() -> None:
    model = ScriptedModel(("en", [Segment(0.0, 1.0, "x")]), ("en", [Segment(0.0, 1.0, "y")]))

    transcript, _ = run(model, seconds=100, language="he")

    assert model.requested_languages == ["he", "he"]
    assert transcript.language == "he"


def test_progress_rises_to_one_even_when_the_end_of_the_recording_is_silent() -> None:
    model = ScriptedModel(("en", [Segment(0.0, 30.0, "speech")]), ("en", []))

    _, progress = run(model, seconds=100)

    assert progress == sorted(progress)
    assert 0 < progress[0] < 1
    assert progress[-1] == 1.0


def test_surrounding_whitespace_is_removed_and_empty_segments_are_dropped() -> None:
    model = ScriptedModel(("en", [Segment(0.0, 1.0, "  Hello. "), Segment(1.0, 2.0, "   ")]))

    transcript, _ = run(model, seconds=30)

    assert transcript.segments == [Segment(0.0, 1.0, "Hello.")]


def test_no_window_is_longer_than_the_limit_that_keeps_memory_flat() -> None:
    model = ScriptedModel(*[("en", [])] * 20)

    run(model, seconds=600)

    assert len(model.window_seconds) > 1
    assert max(model.window_seconds) <= 60 + 10
