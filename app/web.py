"""Serves the single-page user interface: plain HTML, CSS and JavaScript, no build step."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

_STATIC_DIR = Path(__file__).parent / "static"

# The page only needs its own script and stylesheet, API calls to this server, and
# to play the user's local file (a blob: URL). Everything else is forbidden, so even
# if text from an upload were ever inserted as HTML, it could not load or run code.
_CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self'",
        "img-src 'self'",
        "connect-src 'self'",
        "media-src blob:",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    ]
)

_PAGE_HEADERS = {
    "Content-Security-Policy": _CONTENT_SECURITY_POLICY,
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-cache",
}


class _RevalidatedStaticFiles(StaticFiles):
    """Static files that the browser must revalidate before using a cached copy.

    The check is a cheap conditional request (ETag), and it means a new version of
    the page's script or stylesheet is picked up immediately.
    """

    def file_response(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


def mount_ui(app: FastAPI) -> None:
    app.mount("/static", _RevalidatedStaticFiles(directory=_STATIC_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(_STATIC_DIR / "index.html", headers=_PAGE_HEADERS)
