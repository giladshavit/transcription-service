"""Measure the service's transcriber on a real recording: accuracy, speed and memory.

The recording is built from consecutive LibriSpeech "test-clean" utterances read by
one person (about 8 minutes, CC BY 4.0, https://www.openslr.org/12). It goes through
the same code the service uses: ffmpeg decoding and windowed transcription.

Usage (from the project root):

    uv run python scripts/benchmark.py [model]

The model defaults to the service's default. The first run downloads about 10 MB of
audio into data/benchmark/ and, if needed, the model.
"""

import re
import resource
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # make "app" importable

from app.config import Settings
from app.media import SAMPLE_RATE, decode_audio
from app.transcriber import FasterWhisperTranscriber

ARCHIVE_URL = "https://www.openslr.org/resources/12/test-clean.tar.gz"
SAMPLE_DIR = Path("data/benchmark/librispeech")


def download_sample(directory: Path) -> None:
    """Fetch the utterances of the first reader in the archive, and their transcripts.

    The archive is read as a stream and the connection is closed as soon as the first
    reader's folder is complete, so only about 10 MB of the 346 MB are transferred.
    """
    directory.mkdir(parents=True, exist_ok=True)
    first_reader = None
    with (
        urllib.request.urlopen(ARCHIVE_URL, timeout=60) as response,
        tarfile.open(fileobj=response, mode="r|gz") as archive,
    ):
        for member in archive:
            # Paths look like LibriSpeech/test-clean/<reader>/<chapter>/<file>
            parts = Path(member.name).parts
            wanted = len(parts) == 5 and parts[4].endswith((".flac", ".trans.txt"))
            if not member.isfile() or not wanted:
                continue
            if first_reader is None:
                first_reader = parts[2]
            elif parts[2] != first_reader:
                break
            (directory / parts[4]).write_bytes(archive.extractfile(member).read())


def load_recording(directory: Path) -> tuple[np.ndarray, str]:
    """Join the utterances into one recording; return it with its reference text."""
    references = {}
    for transcript in directory.glob("*.trans.txt"):
        for line in transcript.read_text().splitlines():
            utterance_id, _, text = line.partition(" ")
            references[utterance_id] = text
    utterances = sorted(directory.glob("*.flac"))
    audio = np.concatenate([decode_audio(path, max_duration_seconds=3600) for path in utterances])
    return audio, " ".join(references[path.stem] for path in utterances)


def words(text: str) -> list[str]:
    """Lower-case and drop punctuation, so that only the words are compared."""
    return re.sub(r"[^\w\s]", " ", text.lower()).split()


def word_errors(reference: list[str], hypothesis: list[str]) -> int:
    """The edit distance between two word lists: substitutions, deletions and insertions."""
    previous = list(range(len(hypothesis) + 1))
    for row, expected in enumerate(reference, start=1):
        current = [row]
        for column, actual in enumerate(hypothesis, start=1):
            current.append(
                min(
                    previous[column] + 1,  # a word is missing from the transcript
                    current[column - 1] + 1,  # the transcript has an extra word
                    previous[column - 1] + (expected != actual),  # a different word
                )
            )
        previous = current
    return previous[-1]


def main() -> None:
    settings = Settings()
    model = sys.argv[1] if len(sys.argv) > 1 else settings.whisper_model

    if not any(SAMPLE_DIR.glob("*.flac")):
        print("Downloading the sample recording...")
        download_sample(SAMPLE_DIR)
    audio, reference_text = load_recording(SAMPLE_DIR)
    audio_seconds = len(audio) / SAMPLE_RATE

    transcriber = FasterWhisperTranscriber(
        model=model,
        device=settings.whisper_device,
        compute_type=settings.whisper_compute_type,
        cpu_threads=settings.whisper_cpu_threads,
        beam_size=settings.whisper_beam_size,
    )
    transcriber.load()

    started = time.perf_counter()
    transcript = transcriber.transcribe(audio, language=None, on_progress=lambda fraction: None)
    elapsed = time.perf_counter() - started

    reference = words(reference_text)
    hypothesis = words(" ".join(segment.text for segment in transcript.segments))
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    peak_megabytes = peak / 1024**2 if sys.platform == "darwin" else peak / 1024  # bytes vs KB

    print(f"model:            {model}")
    print(f"recording:        {audio_seconds / 60:.1f} minutes, {len(reference)} words")
    print(f"word error rate:  {100 * word_errors(reference, hypothesis) / len(reference):.1f}%")
    print(f"time:             {elapsed:.0f} s ({audio_seconds / elapsed:.1f} times real time)")
    print(f"segments:         {len(transcript.segments)}")
    print(f"peak memory:      {peak_megabytes:.0f} MB")


if __name__ == "__main__":
    main()
