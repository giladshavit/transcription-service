# Transcription Service

Upload an audio or video file in the browser and get a transcription with timestamps.

The speech-to-text model is [Whisper](https://huggingface.co/Systran/faster-whisper-small),
loaded from the Hugging Face Hub and run on CPU with
[faster-whisper](https://github.com/SYSTRAN/faster-whisper).

<!-- DRAFT: this README grows with the project. Sections still to come: Docker quick start,
     API reference, architecture diagram, validation rules, tests in Docker. -->

## Quick start (local)

Requirements: Python 3.12 or newer, [uv](https://docs.astral.sh/uv/), and `ffmpeg` on the `PATH`.

```bash
make install   # create .venv and install the locked dependencies
make run       # start the service on http://localhost:8000
```

Interactive API documentation is served at <http://localhost:8000/docs>.

```bash
make test      # fast test suite
make lint      # ruff: lint and formatting check
```

## Configuration

Settings are read from environment variables, or from a `.env` file in the project root
(see [.env.example](.env.example)). Invalid values stop the service at startup.

| Variable | Default | Meaning |
|---|---|---|
| `DATA_DIR` | `data` | Where the job database and in-flight uploads are kept |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |
| `LOG_FORMAT` | `text` | `text` for a terminal, `json` for one JSON object per line |

## Design decisions

### Uploads are accepted first and transcribed in the background

Transcription takes far longer than an HTTP request should stay open: on a laptop CPU, one
hour of audio takes several minutes. `POST /api/transcriptions` therefore validates and
stores the file, creates a job, and answers `202 Accepted` with the job id straight away.
The client then polls `GET /api/transcriptions/{id}` for the status, the progress and, at
the end, the transcript.

The job lives on the server, so closing the browser tab does not cancel it, and the page
can pick the job up again from its id.

Polling once a second is simple and is enough here. WebSockets or Server-Sent Events would
push updates instead, at the cost of one open connection per waiting client.

### One worker thread and a SQLite job table

A single background thread owns the model and takes jobs from an in-memory queue, one at a
time. One job already uses several CPU cores, so running jobs side by side on one machine
would only split the same cores between them. Inference runs in native code (CTranslate2),
which releases Python's GIL, so the API keeps answering while a job is running.

Job state is stored in SQLite, so jobs and finished transcripts survive a restart. Jobs
that were waiting or in progress when the service stopped are queued again on startup.

SQLite fits a service that runs on one machine: there is nothing to operate, the data is
on disk, and reads are fast. It stops fitting once several servers must write to the same
state. That is the point to move to a durable queue and a database server
(see [Limitations and next steps](#limitations-and-next-steps)).

### Model: Whisper `small`, run by faster-whisper

The assignment asks for an open-source model from Hugging Face. Whisper is released under
the MIT license, covers 99 languages with one model, and returns timestamps for each
segment, which the subtitle exports need.

faster-whisper runs the same Whisper weights on CTranslate2 instead of PyTorch. That keeps
the Docker image small and makes CPU inference fast, and 8-bit quantization (`int8`) cuts
memory use to about a quarter with little loss of accuracy.

The default model was picked by measuring two candidates on an Apple M4 CPU:

| Model (Hugging Face repository) | Download | English WER | Time for 8 minutes of audio | Hebrew WER |
|---|---|---|---|---|
| `Systran/faster-whisper-small` (default) | 0.5 GB | 4.3% | 62 s | 42.5% |
| `mobiuslabsgmbh/faster-whisper-large-v3-turbo` | 1.6 GB | 2.5% | 178 s | 33.2% |

- English: 78 consecutive LibriSpeech `test-clean` utterances by one reader, joined into a
  single 8-minute recording.
- Hebrew: 12 clips from the FLEURS test set.
- WER (word error rate) is computed after lower-casing and removing punctuation. The
  samples are small; the numbers guided a choice and are not a benchmark claim.

`small` is the default because the service is expected to run on CPU, where it is about
three times faster than `large-v3-turbo`. The model is a configuration value, so a
deployment with a GPU can switch to the more accurate model without a code change.

## Assumptions

- The service runs on a single machine, on CPU.
- English is the main language. Other languages go through the same multilingual model
  with lower accuracy (Hebrew is measured above).
- There is no authentication. A job id is a random UUID and acts as the key to its
  transcript.

## Limitations and next steps

- **Scaling out.** The queue is in memory and the database is a local file, so only one
  instance of the service can run. Production would use a durable queue (Redis or SQS),
  workers deployed separately from the API (on GPUs), a database server, and object
  storage for uploads.
- **Very long recordings.** A recording is transcribed by one worker from start to end.
  With more hardware, a long file could be cut at silences into chunks that are
  transcribed in parallel and stitched back together by their time offsets.
