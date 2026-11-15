"""Speech-to-text behind a small interface.

The worker depends on the ``Transcriber`` protocol and not on a specific model, so
the engine can be replaced, and tests can run with a fake that needs no model.
"""

from collections.abc import Callable, Iterable
from typing import Protocol

import numpy as np
from faster_whisper import WhisperModel

from app.media import SAMPLE_RATE
from app.models import Segment, Transcript
from app.windows import split_into_windows

# A recording is transcribed in windows of this length, so that memory use does not
# grow with the length of the recording (see app/windows.py).
WINDOW_SECONDS = 300
# A window may end anywhere in its last 30 seconds: at the quietest moment.
BOUNDARY_SEARCH_SECONDS = 30

# Called with the fraction of the recording processed so far (0.0 to 1.0).
ProgressCallback = Callable[[float], None]


class SpokenSegment(Protocol):
    """A segment as the speech-to-text engine reports it: times relative to its window."""

    start: float
    end: float
    text: str


# Transcribes one window. Takes the samples and a language code (None to detect it);
# returns the segments, lazily if it likes, and the language it used or detected.
WindowTranscriber = Callable[[np.ndarray, str | None], tuple[Iterable[SpokenSegment], str]]


class Transcriber(Protocol):
    @property
    def supported_languages(self) -> frozenset[str]:
        """Language codes the loaded model can transcribe, for example "en"."""

    def load(self) -> None:
        """Load the model into memory. Called once, before the first transcription."""

    def transcribe(
        self, audio: np.ndarray, *, language: str | None, on_progress: ProgressCallback
    ) -> Transcript:
        """Transcribe 16 kHz mono float32 samples.

        ``language`` is a language code, or None to detect the language.
        ``on_progress`` is called as the transcription advances.
        """


class FasterWhisperTranscriber:
    """Whisper weights from the Hugging Face Hub, run on CTranslate2 by faster-whisper."""

    def __init__(
        self, *, model: str, device: str, compute_type: str, cpu_threads: int, beam_size: int
    ) -> None:
        self._model_name = model
        self._device = device
        self._compute_type = compute_type
        self._cpu_threads = cpu_threads
        self._beam_size = beam_size
        self._model: WhisperModel | None = None

    @property
    def supported_languages(self) -> frozenset[str]:
        return frozenset(self._loaded_model().supported_languages)

    def load(self) -> None:
        # The first call downloads the model from the Hugging Face Hub into the local
        # cache; later calls read it from there.
        self._model = WhisperModel(
            self._model_name,
            device=self._device,
            compute_type=self._compute_type,
            cpu_threads=self._cpu_threads,
        )

    def transcribe(
        self, audio: np.ndarray, *, language: str | None, on_progress: ProgressCallback
    ) -> Transcript:
        return transcribe_in_windows(
            audio, language=language, on_progress=on_progress, transcribe_window=self._window
        )

    def _window(
        self, audio: np.ndarray, language: str | None
    ) -> tuple[Iterable[SpokenSegment], str]:
        # vad_filter drops the silent parts first. That saves time, and it stops Whisper
        # from inventing text for stretches that contain no speech.
        # The segments come back as a generator: the model runs while it is consumed.
        segments, info = self._loaded_model().transcribe(
            audio, language=language, beam_size=self._beam_size, vad_filter=True
        )
        return segments, info.language

    def _loaded_model(self) -> WhisperModel:
        if self._model is None:
            raise RuntimeError("load() must be called before the transcriber is used")
        return self._model


def transcribe_in_windows(
    audio: np.ndarray,
    *,
    language: str | None,
    on_progress: ProgressCallback,
    transcribe_window: WindowTranscriber,
    window_seconds: float = WINDOW_SECONDS,
    search_seconds: float = BOUNDARY_SEARCH_SECONDS,
) -> Transcript:
    """Transcribe a recording one window at a time and join the results.

    Segment times are shifted by the start of their window. When no language is
    requested, it is detected on the first window that contains speech and then kept,
    so a later window cannot be transcribed as a different language by mistake.
    """
    transcribed: list[Segment] = []
    detected: str | None = None

    for start, end in split_into_windows(audio, SAMPLE_RATE, window_seconds, search_seconds):
        offset_seconds = start / SAMPLE_RATE
        segments, window_language = transcribe_window(audio[start:end], language or detected)

        window_had_speech = False
        for segment in segments:
            text = segment.text.strip()
            if text:
                window_had_speech = True
                transcribed.append(
                    Segment(
                        start=round(offset_seconds + segment.start, 2),
                        end=round(offset_seconds + segment.end, 2),
                        text=text,
                    )
                )
            reached = start + segment.end * SAMPLE_RATE
            on_progress(min(reached / len(audio), end / len(audio)))

        if window_had_speech and detected is None:
            detected = window_language
        on_progress(end / len(audio))  # also moves on through a window without speech

    return Transcript(language=language or detected or "", segments=transcribed)
