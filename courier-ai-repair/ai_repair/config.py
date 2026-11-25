"""Settings of the repair service, read from the environment (and ``.env``)."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Three secrets live here and nowhere else: the Engine's repair token, the OpenRouter key
    and the GitHub token. None of them is ever given to the model, written into the
    repository or passed into the sandbox."""

    model_config = SettingsConfigDict(env_file=(".env", "../.env"), extra="ignore")

    #: The Engine's internal API, e.g. http://127.0.0.1:8081
    ai_repair_engine_url: str = "http://127.0.0.1:8081"
    engine_repair_token: SecretStr = SecretStr("")

    openrouter_api_key: SecretStr = SecretStr("")
    #: The model that writes the code: a name from openrouter.ai/models, or several separated
    #: by commas. The first is used; the next ones only when it is unavailable or rate-limited.
    #: The default is free: the two free models that did best when all free models were tried
    #: on this service's own tasks (docs/phase-9.md), then OpenRouter's pick among free models.
    openrouter_model: str = "baidu/ernie-5.5-flash-preview:free,particle-ai/particle-1:free,openrouter/free"
    #: How often a call is repeated when OpenRouter says "too many requests" (free models
    #: say it often), and how long to wait in between.
    ai_repair_llm_retries: int = Field(default=3, ge=0, le=10)
    ai_repair_llm_retry_wait_s: float = Field(default=30.0, ge=0, le=600)
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    ai_repair_llm_timeout_s: float = Field(default=240.0, ge=10, le=900)
    #: Room for the answer. Models that think before answering use part of it for that.
    ai_repair_max_output_tokens: int = Field(default=32000, ge=1000, le=128000)

    #: "owner/name" of the GitHub repository the service clones and pushes to.
    ai_repair_github_repo: str = ""
    ai_repair_github_token: SecretStr = SecretStr("")
    #: Overrides the address built from the repository name (used by the tests: a local path).
    ai_repair_repo_url: str = ""
    #: The branch the finished work is pushed to.
    ai_repair_push_branch: str = "main"
    ai_repair_git_name: str = "courier-ai-repair"
    ai_repair_git_email: str = "courier-ai-repair@users.noreply.github.com"
    #: true = do everything except the push (to look at what it would publish).
    ai_repair_dry_run: bool = True

    #: How often the model may try again after its proposal was rejected or its tests failed.
    ai_repair_max_rounds: int = Field(default=3, ge=1, le=6)
    ai_repair_poll_s: float = Field(default=60.0, ge=1, le=3600)
    ai_repair_sandbox_timeout_s: float = Field(default=300.0, ge=10, le=1800)
    #: Where clones are made. Each task gets its own directory, removed afterwards.
    ai_repair_work_dir: Path = Path("/tmp/courier-ai-repair")
    #: A ready Python environment of courier-adapters to run the checks with. Empty: one is
    #: created in every clone with "uv sync --frozen" (needs network or uv's cache).
    ai_repair_adapters_venv: str = ""

    def repo_url(self) -> str:
        if self.ai_repair_repo_url:
            return self.ai_repair_repo_url
        return f"https://github.com/{self.ai_repair_github_repo}.git"

    def missing(self) -> list[str]:
        """Names of required settings that are not set."""
        needed = {
            "ENGINE_REPAIR_TOKEN": self.engine_repair_token.get_secret_value(),
            "OPENROUTER_API_KEY": self.openrouter_api_key.get_secret_value(),
            "AI_REPAIR_GITHUB_REPO": self.ai_repair_github_repo or self.ai_repair_repo_url,
        }
        if not self.ai_repair_repo_url and not self.ai_repair_dry_run:
            needed["AI_REPAIR_GITHUB_TOKEN"] = self.ai_repair_github_token.get_secret_value()
        return [name for name, value in needed.items() if not value]
