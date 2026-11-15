# syntax=docker/dockerfile:1
#
# Stages:
#   base     Python, ffmpeg and an unprivileged user
#   deps     the locked Python dependencies (installed with uv)
#   model    the speech-to-text model, downloaded from the Hugging Face Hub
#   test     dependencies + development tools + source: `docker compose run --rm tests`
#   runtime  what is shipped: dependencies + model + application code
#
# Layers are ordered from rarely changing to often changing, so a code change
# rebuilds in seconds and does not download the dependencies or the model again.

FROM python:3.13-slim-trixie AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HF_HOME=/opt/huggingface \
    PATH="/app/.venv/bin:$PATH"

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# The service does not need root, and it handles files from strangers.
RUN useradd --create-home --uid 1000 app

WORKDIR /app


FROM base AS deps

COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Only the dependency manifests are copied here, so this layer is rebuilt only when
# a dependency changes. --frozen installs exactly what uv.lock records.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


FROM deps AS model

# Baked into the image so that the service starts at once and works without network
# access. The price is a larger image. Build with another model:
#   docker compose build --build-arg WHISPER_MODEL=<Hugging Face repository>
ARG WHISPER_MODEL=Systran/faster-whisper-small
RUN HF_HUB_DISABLE_XET=1 python -c \
    "from faster_whisper import download_model; download_model('${WHISPER_MODEL}')"


FROM deps AS test

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen
COPY . .
CMD ["sh", "-c", "ruff check . && ruff format --check . && pytest"]


FROM base AS runtime

ARG WHISPER_MODEL=Systran/faster-whisper-small
ENV WHISPER_MODEL=${WHISPER_MODEL} \
    HF_HUB_OFFLINE=1 \
    DATA_DIR=/data

COPY --from=deps /app/.venv /app/.venv
# Owned by the service user: the Hugging Face library keeps small bookkeeping files here.
COPY --from=model --chown=app:app /opt/huggingface /opt/huggingface
COPY app ./app

# A new named volume takes its ownership from this directory, so it must already
# belong to the unprivileged user.
RUN mkdir /data && chown app:app /data
VOLUME /data
USER app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4)"]

# One process on purpose: the worker thread and the model live in it.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--timeout-graceful-shutdown", "10"]
