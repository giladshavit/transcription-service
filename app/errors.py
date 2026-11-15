"""Errors that are reported to the client, and how they become HTTP responses.

Every error response has the same JSON shape: ``{"code": "...", "message": "..."}``.
``code`` is stable and meant for programs; ``message`` is meant for people.
"""

from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException


class AppError(HTTPException):
    """An expected failure, with a message that is safe to show to the user.

    It extends Starlette's HTTPException for one reason: an error raised while the
    request body is still being read (the upload size limit) then reaches the client
    with its own status. FastAPI turns any other exception at that point into a 400.
    """

    status_code = 500
    code = "internal_error"

    def __init__(self, message: str, *, headers: dict[str, str] | None = None) -> None:
        super().__init__(status_code=self.status_code, detail=message, headers=headers)
        self.message = message


class JobNotFoundError(AppError):
    status_code = 404
    code = "job_not_found"

    def __init__(self) -> None:
        super().__init__("There is no transcription with this id.")


class JobNotReadyError(AppError):
    """The request is valid, but the job is not in a state that allows it."""

    status_code = 409
    code = "job_not_ready"


class FileTooLargeError(AppError):
    status_code = 413
    code = "file_too_large"

    def __init__(self, limit_mb: int) -> None:
        super().__init__(f"The file is larger than the {limit_mb} MB limit.")


class UnsupportedMediaTypeError(AppError):
    """The upload is not one of the supported audio or video file types."""

    status_code = 415
    code = "unsupported_media_type"


class UnprocessableMediaError(AppError):
    """The upload arrived, but it cannot be transcribed (unreadable, no audio, too long)."""

    status_code = 422
    code = "unprocessable_media"


class UnsupportedLanguageError(AppError):
    status_code = 422
    code = "unsupported_language"


class QueueFullError(AppError):
    status_code = 503
    code = "queue_full"

    def __init__(self) -> None:
        super().__init__(
            "Too many transcriptions are waiting. Try again in a minute.",
            headers={"Retry-After": "60"},
        )


def error_response(error: AppError) -> JSONResponse:
    return JSONResponse(
        {"code": error.code, "message": error.message},
        status_code=error.status_code,
        headers=error.headers,
    )


def register_error_handlers(app: FastAPI) -> None:
    """Make every error the app can produce use the common JSON shape."""

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, error: AppError) -> JSONResponse:
        return error_response(error)

    @app.exception_handler(RequestValidationError)
    async def handle_invalid_request(
        request: Request, error: RequestValidationError
    ) -> JSONResponse:
        # Each problem has a location such as ("body", "file") and a message.
        problems = [f"{problem['loc'][-1]}: {problem['msg']}" for problem in error.errors()]
        return JSONResponse(
            {"code": "invalid_request", "message": "; ".join(problems)}, status_code=422
        )

    @app.exception_handler(HTTPException)
    async def handle_http_error(request: Request, error: HTTPException) -> JSONResponse:
        # Raised by the framework itself: unknown route, method not allowed.
        code = HTTPStatus(error.status_code).phrase.lower().replace(" ", "_")
        return JSONResponse(
            {"code": code, "message": str(error.detail)},
            status_code=error.status_code,
            headers=error.headers,
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, error: Exception) -> JSONResponse:
        # A bug. The server logs the traceback; the client learns nothing internal.
        return JSONResponse(
            {"code": "internal_error", "message": "Internal server error."}, status_code=500
        )
