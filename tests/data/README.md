# Test data

`jfk.mp3` is an 11-second excerpt of President John F. Kennedy's inaugural address
(20 January 1961). As a work of the United States federal government it is in the
public domain. The recording is the sample used by the Whisper project
(`tests/jfk.flac` in <https://github.com/openai/whisper>), converted to 16 kHz mono MP3.

Every other media file the tests need is generated with ffmpeg when the tests run
(see `tests/conftest.py`).
