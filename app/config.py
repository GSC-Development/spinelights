"""Runtime configuration loaded from environment / .env file."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # TPC
    tpc_base_url: str = Field(default="https://192.168.54.227")
    tpc_username: str = Field(default="scheduler")
    tpc_password: str = Field(default="")
    tpc_verify_ssl: bool = Field(default=False)
    tpc_request_timeout_seconds: float = Field(default=5.0)

    # App
    app_host: str = Field(default="0.0.0.0")
    app_port: int = Field(default=8000)
    app_secret_key: str = Field(default="dev-only-change-me")
    app_timezone: str = Field(default="Europe/London")
    # If set, the app skips the login screen and auto-authenticates every
    # request as this username. Use only on a trusted LAN where Caddy's
    # @not_lan block enforces who can reach the box.
    app_auto_login_as: str = Field(default="")
    # The .pd2 project's daily schedule runs between these times. Overrides
    # whose start_at falls inside this window are rejected — anything in
    # [start, release] would be wiped out when the release trigger fires.
    daily_start_hour: int = Field(default=6)
    daily_start_minute: int = Field(default=0)
    daily_release_hour: int = Field(default=18)
    daily_release_minute: int = Field(default=0)

    # Storage
    db_path: Path = Field(default=Path("./data/pharos.sqlite"))

    # Logging
    log_level: str = Field(default="INFO")

    @property
    def sqlalchemy_url(self) -> str:
        return f"sqlite:///{self.db_path}"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.db_path.parent.mkdir(parents=True, exist_ok=True)
    return s
