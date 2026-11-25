"""Settings, read from environment variables (and a ``.env`` file if present)."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HUMAN_INTERFACE_", env_file=(_REPO_ROOT / ".env", ".env"), extra="ignore"
    )

    #: The Engine's public API.
    engine_url: str = "http://127.0.0.1:8080"
    host: str = "127.0.0.1"
    port: int = Field(default=8088, ge=1, le=65535)
    #: Send the sign-in cookie over HTTPS only. Turn on wherever the site is served with TLS.
    cookie_secure: bool = False
    #: How long a sign-in lasts.
    session_ttl_s: int = Field(default=8 * 3600, ge=60, le=7 * 86_400)
    #: Seconds between automatic reloads of the list of open challenges.
    refresh_s: int = Field(default=15, ge=5, le=600)
