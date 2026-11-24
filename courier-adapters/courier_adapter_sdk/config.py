"""Worker configuration, read from environment variables (and a ``.env`` file if present)."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# courier-adapters/courier_adapter_sdk/config.py → repository root
_REPO_ROOT = Path(__file__).resolve().parents[2]


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(_REPO_ROOT / ".env", ".env"), extra="ignore")

    #: Connection string of the restricted adapter-worker account. A secret: never logged.
    worker_amqp_url: SecretStr
    #: Base URL of the Engine's internal API, e.g. http://engine:8081. Needed only by adapters
    #: that use credentials.
    worker_engine_internal_url: str = ""
    #: This worker's token for the internal API. A secret: never logged.
    worker_token: SecretStr | None = None
    #: How many jobs run at the same time in this process.
    worker_concurrency: int = Field(default=4, ge=1, le=256)
    #: Unacknowledged messages RabbitMQ may hand this worker. Defaults to the concurrency.
    worker_prefetch: int | None = Field(default=None, ge=1, le=1024)
    #: On SIGTERM: how long in-flight jobs may finish before they are cancelled.
    worker_shutdown_grace_s: float = Field(default=30.0, ge=0)
    #: Upper bound for one job's resource cleanup.
    worker_cleanup_timeout_s: float = Field(default=5.0, gt=0)
    #: Tolerated clock difference when deciding that a first delivery has expired.
    worker_expiry_skew_s: float = Field(default=5.0, ge=0)
    #: Stable name for this process in logs and messages. Generated if empty.
    worker_id: str = ""
    #: Lets an adapter simulate a crash, a hang or a duplicated result. Test environments only.
    worker_allow_fault_injection: bool = False
    #: Prefix of exchange and queue names. Empty outside integration tests.
    worker_topology_prefix: str = ""
    topology_path: Path = _REPO_ROOT / "courier-contracts" / "topology.yaml"
    log_format: str = "json"

    @model_validator(mode="after")
    def _defaults(self) -> WorkerSettings:
        if self.worker_prefetch is None:
            self.worker_prefetch = self.worker_concurrency
        return self

    @property
    def prefetch(self) -> int:
        return self.worker_prefetch or self.worker_concurrency
