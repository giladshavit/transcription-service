"""Logging setup: one line per event, with context passed through ``extra=`` kept as fields.

Usage::

    logger.info("Transcription completed", extra={"job_id": job.id, "segments": 42})

Untrusted values (file names, for example) are passed as fields and never formatted
into the message, so they cannot forge extra log lines.
"""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

# Attributes that every LogRecord carries. Anything else on a record was passed by the
# caller through ``extra={...}`` and is treated as structured context.
# ``color_message`` is uvicorn's copy of the message with terminal colour codes.
_RESERVED_ATTRIBUTES = frozenset(vars(logging.makeLogRecord({}))) | {
    "message",
    "asctime",
    "color_message",
}

_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def _timestamp(record: logging.LogRecord) -> str:
    return datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds")


def _context(record: logging.LogRecord) -> dict[str, Any]:
    return {key: value for key, value in vars(record).items() if key not in _RESERVED_ATTRIBUTES}


def _render(value: object) -> str:
    """Render a field value for a text line; quote it if it could break the line apart."""
    text = str(value)
    if not text or any(char.isspace() for char in text):
        return json.dumps(text, ensure_ascii=False)  # escapes newlines and quotes
    return text


class TextFormatter(logging.Formatter):
    """Human-readable lines: ``<time> <level> <logger>: <message> key=value ...``."""

    def format(self, record: logging.LogRecord) -> str:
        line = f"{_timestamp(record)} {record.levelname:<7} {record.name}: {record.getMessage()}"
        for key, value in _context(record).items():
            line += f" {key}={_render(value)}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


class JsonFormatter(logging.Formatter):
    """One JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "timestamp": _timestamp(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            **_context(record),
        }
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


def configure_logging(level: str, log_format: str) -> None:
    """Send every logger, including uvicorn's, through a single stdout handler."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if log_format == "json" else TextFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

    # uvicorn installs its own handlers; remove them so its records use our format too.
    for name in _UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
