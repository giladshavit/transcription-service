"""Application settings, read from environment variables and an optional .env file."""

from pathlib import Path
from typing import Literal

from pydantic import Field
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

    # --- Speech-to-text model ---
    # A Whisper model in CTranslate2 format: a Hugging Face repository id or a local path.
    whisper_model: str = "Systran/faster-whisper-small"
    whisper_device: Literal["cpu", "cuda"] = "cpu"
    # int8 quantization: about a quarter of the memory of float32, and faster on CPU.
    whisper_compute_type: str = "int8"
    # 0 lets CTranslate2 choose (4 threads).
    whisper_cpu_threads: int = Field(default=0, ge=0)
    whisper_beam_size: int = Field(default=5, ge=1)

    # --- Limits that protect the machine ---
    max_upload_mb: int = Field(default=2048, gt=0)
    # A recording is decoded into memory as a whole, so its length must be bounded.
    max_duration_minutes: int = Field(default=240, gt=0)
    # New uploads are refused (503) while this many jobs are waiting or running.
    max_pending_jobs: int = Field(default=10, gt=0)
    # A job that was interrupted by a crash this many times is marked as failed.
    max_job_attempts: int = Field(default=2, gt=0)

    # --- Logging ---
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # "text" is easy to read in a terminal; "json" is one object per line for log aggregators.
    log_format: Literal["text", "json"] = "text"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def database_path(self) -> Path:
        return self.data_dir / "jobs.sqlite3"

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def max_duration_seconds(self) -> int:
        return self.max_duration_minutes * 60
