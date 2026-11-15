"""Test doubles."""

import threading

import numpy as np

from app.models import Segment, Transcript
from app.transcriber import ProgressCallback


class FakeTranscriber:
    """Stands in for the real model: no download, and it answers immediately.

    Call ``hold()`` to keep transcriptions "in progress" until ``resume()``.
    """

    supported_languages = frozenset({"en", "he"})

    def __init__(self) -> None:
        self.segments = [Segment(0.0, 1.2, "Hello world."), Segment(1.2, 2.0, "Second line.")]
        self.detected_language = "en"
        self.error: Exception | None = None
        self.loaded = False
        self.calls: list[dict] = []  # one entry per transcribe() call
        self.started = threading.Event()  # set when a transcription begins
        self._may_finish = threading.Event()
        self._may_finish.set()

    def hold(self) -> None:
        self._may_finish.clear()

    def resume(self) -> None:
        self._may_finish.set()

    def load(self) -> None:
        self.loaded = True

    def transcribe(
        self, audio: np.ndarray, *, language: str | None, on_progress: ProgressCallback
    ) -> Transcript:
        self.calls.append({"samples": len(audio), "language": language})
        self.started.set()
        if self.error is not None:
            raise self.error
        while not self._may_finish.wait(timeout=0.01):
            on_progress(0.5)  # like the real model, report progress while working
        on_progress(1.0)
        return Transcript(language=language or self.detected_language, segments=list(self.segments))
