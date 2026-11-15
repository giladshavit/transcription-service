from itertools import pairwise

import numpy as np
import pytest

from app.windows import split_into_windows

RATE = 100  # samples per second; small numbers keep the arithmetic readable


def speech_with_pauses(seconds: int, pause_starts: list[int]) -> np.ndarray:
    """Constant "speech" with one second of silence starting at each given second."""
    audio = np.full(seconds * RATE, 0.5, dtype=np.float32)
    for start in pause_starts:
        audio[start * RATE : (start + 1) * RATE] = 0.0
    return audio


def test_a_short_recording_is_a_single_window() -> None:
    audio = speech_with_pauses(50, [])

    assert split_into_windows(audio, RATE, window_seconds=60, search_seconds=10) == [(0, 5000)]


def test_windows_cover_the_whole_recording_without_gaps_or_overlap() -> None:
    audio = speech_with_pauses(250, [])

    windows = split_into_windows(audio, RATE, window_seconds=60, search_seconds=10)

    assert windows[0][0] == 0
    assert windows[-1][1] == len(audio)
    assert all(end == next_start for (_, end), (next_start, _) in pairwise(windows))


def test_a_window_ends_in_the_pause_nearest_its_end_so_no_word_is_cut() -> None:
    audio = speech_with_pauses(150, pause_starts=[20, 55, 112])

    windows = split_into_windows(audio, RATE, window_seconds=60, search_seconds=10)

    first_end, second_end = windows[0][1], windows[1][1]
    assert 55 * RATE <= first_end <= 56 * RATE  # inside the pause at 55-56 s, not at 60 s
    assert 112 * RATE <= second_end <= 113 * RATE  # inside the pause at 112-113 s


def test_window_length_is_bounded() -> None:
    audio = speech_with_pauses(1000, pause_starts=[57, 110, 171, 222])

    windows = split_into_windows(audio, RATE, window_seconds=60, search_seconds=10)

    lengths = [end - start for start, end in windows]
    assert max(lengths[:-1]) <= 60 * RATE
    assert lengths[-1] <= (60 + 10) * RATE  # the last window may keep a short remainder


def test_a_short_tail_is_merged_into_the_last_window() -> None:
    # 65 seconds with 60-second windows: a 5-second window of its own would give the
    # model almost no context, so the recording stays in one piece.
    audio = speech_with_pauses(65, [])

    assert split_into_windows(audio, RATE, window_seconds=60, search_seconds=10) == [(0, 6500)]


def test_an_empty_recording_has_no_windows() -> None:
    assert split_into_windows(np.zeros(0, dtype=np.float32), RATE, 60, 10) == []


@pytest.mark.parametrize("seconds", [61, 119, 120, 121, 600])
def test_every_sample_belongs_to_exactly_one_window(seconds: int) -> None:
    audio = speech_with_pauses(seconds, [])

    windows = split_into_windows(audio, RATE, window_seconds=60, search_seconds=10)

    assert sum(end - start for start, end in windows) == len(audio)
