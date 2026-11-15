"""ASGI middleware: code that sees every request before the routes do."""

from fastapi.concurrency import run_in_threadpool
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.errors import AppError, FileTooLargeError, QueueFullError, error_response

# Room for the multipart framing and the other form fields around the file itself.
_MULTIPART_OVERHEAD_BYTES = 64 * 1024


class UploadGuard:
    """Refuses uploads the service cannot take, before their body is read.

    FastAPI reads a multipart body and writes the file to a temporary file before the
    endpoint runs. A check inside the endpoint would come after a 2 GB upload has
    already been received and stored, so these checks happen here:

    * the queue is full (503);
    * the declared Content-Length is over the limit (413);
    * the body turns out to be over the limit while it is arriving (413), which
      covers clients that send no Content-Length or a false one.
    """

    def __init__(
        self, app: ASGIApp, *, path: str, max_upload_mb: int, max_pending_jobs: int
    ) -> None:
        self._app = app
        self._path = path
        self._max_upload_mb = max_upload_mb
        self._max_body_bytes = max_upload_mb * 1024 * 1024 + _MULTIPART_OVERHEAD_BYTES
        self._max_pending_jobs = max_pending_jobs

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"] != self._path:
            await self._app(scope, receive, send)
            return

        rejection = await self._reason_to_reject(scope)
        if rejection is not None:
            await error_response(rejection)(scope, receive, send)
            return

        received_bytes = 0

        async def counting_receive() -> Message:
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > self._max_body_bytes:
                    raise FileTooLargeError(self._max_upload_mb)
            return message

        await self._app(scope, counting_receive, send)

    async def _reason_to_reject(self, scope: Scope) -> AppError | None:
        store = scope["app"].state.store
        if await run_in_threadpool(store.count_unfinished) >= self._max_pending_jobs:
            return QueueFullError()
        declared_length = Headers(scope=scope).get("content-length", "")
        if declared_length.isdigit() and int(declared_length) > self._max_body_bytes:
            return FileTooLargeError(self._max_upload_mb)
        return None


class ResponseHeaders:
    """Adds the headers every response should carry.

    * ``X-Content-Type-Options: nosniff``: the browser must trust the declared content
      type and not guess, so a response can never be reinterpreted as a script.
    * ``Cache-Control: no-store`` on /api/ responses: job status changes from one second
      to the next and transcripts are private, so browsers and proxies keep no copies.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        is_api_call = scope["path"].startswith("/api/")

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["X-Content-Type-Options"] = "nosniff"
                if is_api_call:
                    headers["Cache-Control"] = "no-store"
            await send(message)

        await self._app(scope, receive, send_with_headers)
