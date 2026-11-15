from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings


def test_environment_variables_override_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATA_DIR", "/tmp/somewhere")
    monkeypatch.setenv("LOG_FORMAT", "json")

    settings = Settings(_env_file=None)

    assert settings.data_dir == Path("/tmp/somewhere")
    assert settings.log_format == "json"


def test_invalid_value_is_rejected_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "LOUD")

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_log_settings_are_not_case_sensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    # LOG_LEVEL is a common variable name; a shell that already exports "info" must not
    # stop the service from starting.
    monkeypatch.setenv("LOG_LEVEL", "info")
    monkeypatch.setenv("LOG_FORMAT", "JSON")

    settings = Settings(_env_file=None)

    assert settings.log_level == "INFO"
    assert settings.log_format == "json"
