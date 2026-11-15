import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.fakes import FakeTranscriber


def test_health(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_startup_fails_when_ffmpeg_is_missing(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.media.shutil.which", lambda name: None)

    app = create_app(settings, FakeTranscriber())

    with pytest.raises(RuntimeError, match="ffmpeg"), TestClient(app):
        pass
