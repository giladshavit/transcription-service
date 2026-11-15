"""Application settings, read from environment variables and an optional .env file."""

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Service configuration.

    Each field can be overridden by an environment variable with the same name in
    upper case (for example ``LOG_LEVEL=DEBUG``). Invalid values fail at startup,
    not in the middle of a request.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Where the service keeps its state (job database, uploads being processed).
    data_dir: Path = Path("data")

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # "text" is easy to read in a terminal; "json" is one object per line for log aggregators.
    log_format: Literal["text", "json"] = "text"
