"""Process-level configuration, read from environment variables.

These are deployment facts (paths, ports) that are fixed for the life of the
process. User-editable settings live in the database; see ``app_settings.py``.
"""

from __future__ import annotations

from functools import cached_property
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Env(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RM_", extra="ignore")

    config_dir: Path = Path("/config")
    data_dir: Path = Path("/data")
    import_dir: Path = Path("/import")
    # Optional read-only ROM library, scanned for inputs (see scan.py)
    roms_dir: Path = Path("/roms")
    # Built frontend. When unset or missing, the API runs without the web UI.
    static_dir: Path | None = None
    docker_socket: Path = Path("/var/run/docker.sock")
    # Host-side path of /data. Sibling build containers are started through the
    # host's Docker daemon, so their bind mounts must use host paths, not ours.
    host_data_path: str | None = Field(
        default=None, validation_alias=AliasChoices("HOST_DATA_PATH", "RM_HOST_DATA_PATH")
    )
    host: str = "0.0.0.0"
    port: int = 8080
    log_level: str = "INFO"
    job_workers: int = 2
    scheduler: bool = True  # periodic update checks; off in tests

    @cached_property
    def database_path(self) -> Path:
        return self.config_dir / "recompmanager.db"

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.database_path.as_posix()}"

    @property
    def bootstrap_token_file(self) -> Path:
        return self.config_dir / "admin-token.txt"

    @property
    def recipes_dir(self) -> Path:
        return self.config_dir / "recipes"

    @property
    def logs_dir(self) -> Path:
        return self.config_dir / "logs"

    def ensure_dirs(self) -> None:
        for d in (self.config_dir, self.recipes_dir, self.logs_dir, self.data_dir):
            d.mkdir(parents=True, exist_ok=True)
        for sub in ("library", "inputs", "builds"):
            (self.data_dir / sub).mkdir(parents=True, exist_ok=True)

    @property
    def docker_available(self) -> bool:
        return self.docker_socket.exists()
