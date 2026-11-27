"""The files a new provider needs besides its adapter. Written by fixed rules, not by the model.

A provider is more than adapter code: a queue and a dead-letter queue in the topology, rows
in the Engine's database, a worker service. The model is not allowed near any of that. This
module derives them from the operator's request with templates, the same way the existing
providers were set up, so the result does not depend on what the model says.

A new provider starts switched off, like every other: an API provider needs its credential
stored, a website provider needs its owner's permission recorded.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path

import yaml

from .guard import valid_key

TOPOLOGY = "courier-contracts/topology.yaml"
MIGRATIONS = "courier-engine/migrations"
COMPOSE = "infrastructure/docker-compose.yml"
_HOST = re.compile(
    r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"
)
_NAME = re.compile(r"^[^\'\"\\\n\r]{1,100}$")

# The status codes of adapters/india.py: status_code(), which new adapters are told to use.
_MAPPINGS = (
    ("booked", "CREATED"),
    ("picked-up", "PICKED_UP"),
    ("in-transit", "IN_TRANSIT"),
    ("out-for-delivery", "OUT_FOR_DELIVERY"),
    ("delivered", "DELIVERED"),
    ("undelivered", "EXCEPTION"),
    ("returned", "RETURNED"),
)


class ScaffoldError(Exception):
    """The request or the repository is not what the templates expect."""


@dataclass(frozen=True)
class ProviderSpec:
    code: str
    name: str
    method: str  # "api" or "scrape"
    hosts: tuple[str, ...]
    needs_credential: bool
    notes: str
    sample: str | None = None
    tracking_url: str | None = None

    @classmethod
    def from_request(cls, request: dict[str, object]) -> ProviderSpec:
        """Checks the request again: it arrives over the network and ends up in SQL and YAML."""
        code, name, method = (str(request.get(k, "")) for k in ("code", "name", "method"))
        hosts_value = request.get("hosts")
        hosts = tuple(str(h) for h in hosts_value) if isinstance(hosts_value, list) else ()
        sample = request.get("sample")
        if not valid_key(code):
            raise ScaffoldError("the provider code is not valid")
        if not _NAME.match(name.strip()):
            raise ScaffoldError("the provider name is not valid")
        if method not in ("api", "scrape"):
            raise ScaffoldError("the method must be api or scrape")
        if not 1 <= len(hosts) <= 5 or not all(_HOST.match(h) for h in hosts):
            raise ScaffoldError("hosts must be one to five host names")
        return cls(
            code=code,
            name=name.strip(),
            method=method,
            hosts=hosts,
            # A website may need a login too (Phase 12): the worker then gets it from the Engine.
            needs_credential=bool(request.get("needs_credential")) or bool(request.get("login")),
            notes=str(request.get("notes", ""))[:4000],
            sample=sample[:100_000] if isinstance(sample, str) and sample else None,
            tracking_url=_tracking_url(request.get("tracking_url"), hosts),
        )


def _tracking_url(value: object, hosts: tuple[str, ...]) -> str | None:
    """The tracking address, if it is an http(s) address on one of the approved hosts."""
    if not isinstance(value, str) or len(value) > 500 or any(c in value for c in "\n\r\t "):
        return None
    match = re.match(r"https?://([^/?#:@]+)", value)
    return value if match and match.group(1).lower() in hosts else None


def _insert_before(text: str, anchor: str, block: str, path: str) -> str:
    if text.count(anchor) != 1:
        raise ScaffoldError(f"{path} does not have the expected structure")
    return text.replace(anchor, block + anchor)


def _topology(text: str, code: str) -> str:
    if f"adapter.{code}.jobs" in text:
        raise ScaffoldError(f"the topology already has queues for '{code}'")
    job = f"""  - name: adapter.{code}.jobs
    durable: true
    consumer: adapter-worker
    arguments:
      x-queue-type: quorum
      x-delivery-limit: 3
      x-dead-letter-exchange: courier.dlx
      x-dead-letter-routing-key: adapter.{code}.jobs
    bindings:
      - exchange: courier.jobs
        routing_key: job.track.v1.{code}
      # A lookup continued after a human answer; same queue, same delivery limit.
      - exchange: courier.jobs
        routing_key: job.resume.v1.{code}

"""
    dlq = f"""  - name: adapter.{code}.jobs.dlq
    durable: true
    consumer: engine-dead-letter-consumer
    arguments:
      x-queue-type: quorum
    bindings:
      - exchange: courier.dlx
        routing_key: adapter.{code}.jobs

"""
    text = _insert_before(text, "  - name: engine.events\n", job, TOPOLOGY)
    text = _insert_before(text, "  # Events the Engine itself rejected as malformed.", dlq, TOPOLOGY)
    names = [queue["name"] for queue in yaml.safe_load(text)["queues"]]
    if len(names) != len(set(names)):
        raise ScaffoldError("the topology would have a queue twice")
    return text


def _migration(spec: ProviderSpec) -> str:
    provider_id, adapter_id = uuid.uuid4(), uuid.uuid4()
    gate = (
        "its credential is stored (admin set-credential)"
        if spec.needs_credential
        else "its owner's permission is recorded (admin set-scraping-authorized)"
        if spec.method == "scrape"
        else "an operator has checked it"
    )
    rows = ",\n".join(f"    ('{uuid.uuid4()}', '{raw}', '{normalized}')" for raw, normalized in _MAPPINGS)
    return f"""-- Provider '{spec.code}' ({spec.name}), added by the AI repair service on request.
-- Generated from a template; the adapter code itself is in courier-adapters/adapters/{spec.code}/.
-- On a database where somebody added the courier already (Phase 12: status 'building'), its
-- row is kept as it is (with its login and recorded permission) and only the adapter is
-- added; elsewhere the row is created here. Either way the courier is live afterwards.
-- Elsewhere the provider cannot be used until {gate}.
INSERT INTO providers (id, code, name, tracking_method, timeout_ms, queue_wait_max_ms, result_grace_ms,
                       max_attempts, retry_base_ms, retry_max_ms, requires_credential,
                       min_dispatch_interval_ms)
VALUES ('{provider_id}', '{spec.code}', '{spec.name}', '{spec.method}',
        20000, 900000, 30000, 3, 5000, 300000, {"true" if spec.needs_credential else "false"}, 2000)
ON CONFLICT (code) DO NOTHING;

INSERT INTO adapters (id, provider_id, key, method, queue_name)
SELECT '{adapter_id}', id, '{spec.code}', '{spec.method}', 'adapter.{spec.code}.jobs'
  FROM providers WHERE code = '{spec.code}'
ON CONFLICT (key) DO NOTHING;

-- The status codes of courier-adapters/adapters/india.py. Wording the adapter does not
-- recognize arrives unchanged and appears in the list of unmapped statuses.
INSERT INTO provider_status_mappings (id, provider_id, raw_status, normalized_status)
SELECT m.id::uuid, p.id, m.raw_status, m.normalized_status::shipment_status
  FROM providers p, (VALUES
{rows}
  ) AS m (id, raw_status, normalized_status)
 WHERE p.code = '{spec.code}'
ON CONFLICT (provider_id, raw_status) DO NOTHING;

UPDATE providers SET status = 'live', build_failure = NULL, updated_at = now()
 WHERE code = '{spec.code}';
"""


def _compose(text: str, spec: ProviderSpec) -> str:
    if f"  adapter-{spec.code}:" in text:
        raise ScaffoldError(f"the Compose file already has a worker for '{spec.code}'")
    lines = [
        "      WORKER_AMQP_URL: amqp://${RABBITMQ_WORKER_USER}:${RABBITMQ_WORKER_PASSWORD}"
        "@rabbitmq:5672/${RABBITMQ_VHOST}",
        f"      WORKER_CONCURRENCY: ${{{spec.code.upper()}_WORKER_CONCURRENCY:-2}}",
    ]
    if spec.needs_credential:
        # The worker's token comes from infrastructure/worker-tokens/<code>.env, which
        # `make worker-tokens` writes (the automatic update runs it); it is never in Git.
        lines.append("      WORKER_ENGINE_INTERNAL_URL: http://engine:8081")
    lines.append("      LOG_FORMAT: json")
    block = (
        f"""
  # {spec.name} (adapter written by the AI repair service). Started only on request.
  adapter-{spec.code}:
    profiles: ["couriers", "{spec.code}"]
    build: *worker-build
    image: courier-adapter-worker:local
    command: ["-m", "adapters.{spec.code}"]
    restart: unless-stopped
    stop_grace_period: 40s
"""
        + (
            f"""    env_file:
      - path: ./worker-tokens/{spec.code}.env
        required: false
"""
            if spec.needs_credential
            else ""
        )
        + """    environment:
"""
        + "\n".join(lines)
        + """
    depends_on:
      engine:
        condition: service_healthy
"""
    )
    return _insert_before(text, "\nvolumes:\n", block, COMPOSE)


def scaffold(repo: Path, spec: ProviderSpec) -> list[str]:
    """Adds the provider's queues, database rows and worker service. Returns the changed paths."""
    if (repo / "courier-adapters" / "adapters" / spec.code).exists():
        raise ScaffoldError(f"an adapter named '{spec.code}' already exists")
    numbers = [int(p.name[:4]) for p in (repo / MIGRATIONS).glob("[0-9][0-9][0-9][0-9]_*.sql")]
    if not numbers:
        raise ScaffoldError("the Engine's migrations were not found")
    migration = f"{MIGRATIONS}/{max(numbers) + 1:04d}_provider_{spec.code}.sql"

    topology = _topology((repo / TOPOLOGY).read_text(), spec.code)
    compose = _compose((repo / COMPOSE).read_text(), spec)
    (repo / TOPOLOGY).write_text(topology)
    (repo / COMPOSE).write_text(compose)
    (repo / migration).write_text(_migration(spec))
    return [TOPOLOGY, COMPOSE, migration]
