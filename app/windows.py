"""Split a long recording into windows that are transcribed one at a time.

faster-whisper prepares the features of everything it is given in a single pass,
which takes roughly 60 MB of memory per minute of audio: about 4 GB for an hour.
Transcribing window by window keeps the memory use flat, however long the recording.
"""

import numpy as np

# The loudness of the recording is compared in stretches of this length.
_FRAME_SECONDS = 0.2


def split_into_windows(
    audio: np.ndarray, sample_rate: int, window_seconds: float, search_seconds: float
) -> list[tuple[int, int]]:
    """Return (start, end) sample positions that cover the recording, in order.

    A window ends at the quietest moment in its last ``search_seconds``, so that a
    word is not cut in half. Windows are at most ``window_seconds`` long, except the
    last one: a remainder shorter than ``search_seconds`` is not worth a window of
    its own, so it stays attached and the last window can be that much longer.
    """
    window = int(window_seconds * sample_rate)
    search = int(search_seconds * sample_rate)
    windows = []
    start = 0
    while len(audio) - start > window + search:
        end = _quietest_point(audio, start + window - search, start + window, sample_rate)
        windows.append((start, end))
        start = end
    if start < len(audio):
        windows.append((start, len(audio)))
    return windows


def _quietest_point(audio: np.ndarray, low: int, high: int, sample_rate: int) -> int:
    """The middle of the quietest short stretch between two sample positions.

    When several stretches are equally quiet, the last one wins, which keeps the
    window as long as possible.
    """
    frame = max(1, int(_FRAME_SECONDS * sample_rate))
    frame_count = (high - low) // frame
    first_frame_start = high - frame_count * frame
    frames = audio[first_frame_start:high].reshape(frame_count, frame)
    loudness = np.abs(frames).mean(axis=1)
    quietest = frame_count - 1 - int(np.argmin(loudness[::-1]))
    return first_frame_start + quietest * frame + frame // 2
