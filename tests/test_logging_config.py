import json
import logging
import sys

import pytest

from app.logging_config import JsonFormatter, RoutineRequestFilter, TextFormatter


def make_record(message: str = "Job finished", **extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="app.worker",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=None,
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)  # what ``logger.info(..., extra={...})`` does
    return record


def test_text_line_contains_level_logger_message_and_fields() -> None:
    line = TextFormatter().format(make_record(job_id="abc123", segments=42))

    assert "INFO" in line
    assert "app.worker: Job finished" in line
    assert line.endswith("job_id=abc123 segments=42")


def test_uvicorn_colour_copy_of_the_message_is_not_a_field() -> None:
    line = TextFormatter().format(make_record(color_message="\x1b[36mJob finished\x1b[0m"))

    assert line.endswith("app.worker: Job finished")


def test_text_fields_cannot_break_the_line() -> None:
    line = TextFormatter().format(make_record(file_name="a b.mp3\n2026 ERROR forged"))

    assert "\n" not in line
    assert 'file_name="a b.mp3\\n2026 ERROR forged"' in line


def test_json_line_is_one_valid_object_with_fields() -> None:
    line = JsonFormatter().format(make_record(job_id="abc123", file_name="שלום.mp3"))

    entry = json.loads(line)
    assert "\n" not in line
    assert entry["level"] == "INFO"
    assert entry["logger"] == "app.worker"
    assert entry["message"] == "Job finished"
    assert entry["job_id"] == "abc123"
    assert entry["file_name"] == "שלום.mp3"
    assert entry["timestamp"].endswith("+00:00")


def test_exceptions_are_included() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = make_record()
        record.exc_info = sys.exc_info()

    assert "ValueError: boom" in TextFormatter().format(record)
    assert "ValueError: boom" in json.loads(JsonFormatter().format(record))["exception"]


def access_record(method: str, path: str, status: int) -> logging.LogRecord:
    """A record shaped like the ones uvicorn's access logger emits."""
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("127.0.0.1:50000", method, path, "1.1", status),
        exc_info=None,
    )


@pytest.mark.parametrize(
    ("method", "path", "status", "kept"),
    [
        ("GET", "/health", 200, False),  # the container health check, every 30 seconds
        ("GET", "/api/transcriptions/3f2a9c1e", 200, False),  # the page polls every second
        ("GET", "/api/transcriptions/3f2a9c1e", 404, True),  # failures stay visible
        ("GET", "/api/transcriptions/3f2a9c1e/download?format=srt", 200, True),
        ("POST", "/api/transcriptions", 202, True),
        ("GET", "/", 200, True),
    ],
)
def test_only_routine_successful_requests_are_left_out_of_the_access_log(
    method: str, path: str, status: int, kept: bool
) -> None:
    assert RoutineRequestFilter().filter(access_record(method, path, status)) is kept
