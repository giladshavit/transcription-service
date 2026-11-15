"""Speech-to-text behind a small interface.

The worker depends on the ``Transcriber`` protocol and not on a specific model, so
the engine can be replaced, and tests can run with a fake that needs no model.
"""

from collections.abc import Callable
from typing import Protocol

import numpy as np
from faster_whisper import WhisperModel

from app.media import SAMPLE_RATE
from app.models import Segment, Transcript

# Called with the fraction of the recording processed so far (0.0 to 1.0).
ProgressCallback = Callable[[float], None]


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
        ``on_progress`` is called after each segment.
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
        # vad_filter drops the silent parts first. That saves time, and it stops Whisper
        # from inventing text for stretches that contain no speech.
        segments, info = self._loaded_model().transcribe(
            audio, language=language, beam_size=self._beam_size, vad_filter=True
        )
        duration = len(audio) / SAMPLE_RATE
        transcribed: list[Segment] = []
        # ``segments`` is a generator: the model runs while we iterate, one 30-second
        # window at a time, which is what lets us report progress.
        for segment in segments:
            text = segment.text.strip()
            if text:
                start, end = round(segment.start, 2), round(segment.end, 2)
                transcribed.append(Segment(start=start, end=end, text=text))
            on_progress(min(segment.end / duration, 1.0))
        return Transcript(language=info.language, segments=transcribed)

    def _loaded_model(self) -> WhisperModel:
        if self._model is None:
            raise RuntimeError("load() must be called before the transcriber is used")
        return self._model
