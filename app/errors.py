"""Errors that are reported to the client.

Each error carries the HTTP status and a stable machine-readable code, so the
modules that detect a problem do not need to know anything about HTTP responses.
"""


class AppError(Exception):
    """Base class: an expected failure with a message that is safe to show to the user."""

    status_code = 500
    code = "internal_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class UnsupportedMediaTypeError(AppError):
    """The upload is not one of the supported audio or video file types."""

    status_code = 415
    code = "unsupported_media_type"


class UnprocessableMediaError(AppError):
    """The upload arrived, but it cannot be transcribed (unreadable, no audio, too long)."""

    status_code = 422
    code = "unprocessable_media"
