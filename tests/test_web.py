from fastapi.testclient import TestClient

from app.config import Settings


def test_the_page_is_served_at_the_root(client: TestClient) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<title>Transcription Service</title>" in response.text


def test_the_page_may_only_load_scripts_and_styles_from_this_server(client: TestClient) -> None:
    policy = client.get("/").headers["content-security-policy"]

    assert "default-src 'none'" in policy
    assert "script-src 'self'" in policy
    assert "style-src 'self'" in policy
    assert "unsafe-inline" not in policy


def test_every_file_the_page_refers_to_exists(client: TestClient) -> None:
    for path in ("/static/styles.css", "/static/app.js", "/static/favicon.svg"):
        assert path in client.get("/").text
        assert client.get(path).status_code == 200


def test_static_files_are_revalidated_so_a_new_version_shows_up_at_once(client: TestClient) -> None:
    response = client.get("/static/app.js")

    assert response.headers["cache-control"] == "no-cache"
    assert "etag" in response.headers


def test_responses_tell_the_browser_not_to_guess_content_types(client: TestClient) -> None:
    for path in ("/", "/static/app.js", "/health", "/api/config"):
        assert client.get(path).headers["x-content-type-options"] == "nosniff"


def test_the_page_can_ask_the_server_for_its_limits(client: TestClient, settings: Settings) -> None:
    config = client.get("/api/config").json()

    assert config["max_upload_bytes"] == settings.max_upload_bytes
    assert config["max_duration_seconds"] == settings.max_duration_seconds
    assert ".mp3" in config["extensions"]
    assert config["extensions"] == sorted(config["extensions"])
    assert config["languages"] == ["en", "he"]  # what the (fake) model supports
    assert config["model"] == settings.whisper_model
