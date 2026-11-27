"""Settings of the repair service, read from the environment (and ``.env``)."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """The secrets live here and nowhere else: the Engine's repair token, the model supplier's
    key (OpenRouter key, Claude setup token or Anthropic API key) and the GitHub token. None
    of them is ever given to the model, written into the repository or passed into the
    sandbox."""

    # An empty value counts as "not set": Compose passes every listed setting, also the
    # ones the operator left out, and those must fall back to the defaults here.
    model_config = SettingsConfigDict(env_file=(".env", "../.env"), extra="ignore", env_ignore_empty=True)

    #: The Engine's internal API, e.g. http://127.0.0.1:8081
    ai_repair_engine_url: str = "http://127.0.0.1:8081"
    engine_repair_token: SecretStr = SecretStr("")

    #: Who supplies the model: openrouter, claude-code (Claude with a setup token from
    #: "claude setup-token", for a laptop) or anthropic (Claude with an API key, for a server).
    ai_repair_model_supplier: Literal["openrouter", "claude-code", "anthropic"] = "openrouter"

    #: claude-code: the token "claude setup-token" prints (valid one year).
    claude_code_oauth_token: SecretStr = SecretStr("")
    #: anthropic: an API key from platform.claude.com.
    anthropic_api_key: SecretStr = SecretStr("")
    #: The Claude model for both Claude suppliers, and how hard it thinks
    #: (low, medium, high, xhigh, max).
    ai_repair_claude_model: str = "claude-opus-5-5"
    ai_repair_claude_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    #: The Claude Code program (claude-code supplier).
    ai_repair_claude_command: str = "claude"

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

    #: How many actions the agent may take on one task, and how many requests it may make
    #: to the courier's site while doing so.
    ai_repair_agent_max_steps: int = Field(default=40, ge=5, le=200)
    ai_repair_agent_max_requests: int = Field(default=60, ge=0, le=500)
    #: false = the agent works without a browser (plain HTTP only).
    ai_repair_agent_browser: bool = True

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

    def model_key(self) -> tuple[str, str]:
        """The name and value of the setting that holds the chosen supplier's secret."""
        if self.ai_repair_model_supplier == "claude-code":
            return "CLAUDE_CODE_OAUTH_TOKEN", self.claude_code_oauth_token.get_secret_value()
        if self.ai_repair_model_supplier == "anthropic":
            return "ANTHROPIC_API_KEY", self.anthropic_api_key.get_secret_value()
        return "OPENROUTER_API_KEY", self.openrouter_api_key.get_secret_value()

    def model_name(self) -> str:
        if self.ai_repair_model_supplier == "openrouter":
            return self.openrouter_model
        return self.ai_repair_claude_model

    def missing(self) -> list[str]:
        """Names of required settings that are not set."""
        key_name, key = self.model_key()
        needed = {
            "ENGINE_REPAIR_TOKEN": self.engine_repair_token.get_secret_value(),
            key_name: key,
            "AI_REPAIR_GITHUB_REPO": self.ai_repair_github_repo or self.ai_repair_repo_url,
        }
        if not self.ai_repair_repo_url and not self.ai_repair_dry_run:
            needed["AI_REPAIR_GITHUB_TOKEN"] = self.ai_repair_github_token.get_secret_value()
        return [name for name, value in needed.items() if not value]
