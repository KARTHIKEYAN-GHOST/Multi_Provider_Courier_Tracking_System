"""Runs a private copy of the system for the end-to-end tests.

The harness starts the real Engine binary and the real dummy worker as local processes. They
use the PostgreSQL and RabbitMQ containers of the development stack, but nothing that
belongs to development:

* a database of their own (``courier_e2e``), dropped and recreated at the start of every run;
* exchanges and queues under a random name prefix, deleted at the end of the run.

Because prefixed queues do not match the worker account's permissions, the worker runs with
the Engine account here. The worker account itself is checked by the adapter tests.
"""

from __future__ import annotations

import os
import secrets
import signal
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
import psycopg
from psycopg.rows import dict_row

REPO = Path(__file__).resolve().parents[1]
ENGINE_DIR = REPO / "courier-engine"
ADAPTERS_DIR = REPO / "courier-adapters"
E2E_DATABASE = "courier_e2e"

# Timings of the dummy provider during the tests: short, so failures play out in seconds.
PROVIDER_DEFAULTS = {
    "timeout_ms": 2000,
    "queue_wait_max_ms": 8000,
    "result_grace_ms": 1000,
    "max_attempts": 3,
    "retry_base_ms": 300,
    "retry_max_ms": 3000,
    "min_dispatch_interval_ms": 0,
}
PROVIDERS = ("dummy", "dhl", "demosite")
TERMINAL = ("COMPLETED", "FAILED")


def dotenv() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in (REPO / ".env").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return {**values, **{k: v for k, v in os.environ.items() if k in values}}


def wait_until[T](probe: Callable[[], T], timeout_s: float, what: str, interval_s: float = 0.05) -> T:
    """Polls until ``probe`` returns something truthy."""
    deadline = time.monotonic() + timeout_s
    while True:
        value = probe()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout_s}s waiting for {what}")
        time.sleep(interval_s)


class Supervised:
    """A child process whose output goes to a log file.

    With ``restart=True`` it behaves like a container with ``restart: unless-stopped``: when
    the process dies on its own it is started again.
    """

    def __init__(self, name: str, argv: list[str], cwd: Path, env: dict[str, str], log: Path, restart: bool) -> None:
        self.name, self.argv, self.cwd, self.env, self.log, self.restart = name, argv, cwd, env, log, restart
        self.process: subprocess.Popen[bytes] | None = None
        self.exit_codes: list[int] = []
        self._wanted = False
        self._lock = threading.Lock()
        self._watcher: threading.Thread | None = None

    def _spawn(self) -> None:
        with self.log.open("ab") as output:
            self.process = subprocess.Popen(self.argv, cwd=self.cwd, env=self.env, stdout=output, stderr=output)

    def _watch(self) -> None:
        while True:
            process = self.process
            assert process is not None
            code = process.wait()
            with self._lock:
                self.exit_codes.append(code)
                if not (self._wanted and self.restart):
                    return
                time.sleep(0.2)
                self._spawn()

    def start(self, **env: str) -> None:
        with self._lock:
            if self._wanted:
                return
            self.env = {**self.env, **env}
            self._wanted = True
            self._spawn()
        self._watcher = threading.Thread(target=self._watch, daemon=True, name=f"watch-{self.name}")
        self._watcher.start()

    def _end(self, signum: int, timeout_s: float) -> None:
        with self._lock:
            if not self._wanted:
                return
            self._wanted = False
            process = self.process
        assert process is not None
        assert self._watcher is not None
        if process.poll() is None:
            process.send_signal(signum)
        try:
            process.wait(timeout_s)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(5)
        self._watcher.join(5)

    def stop(self, timeout_s: float = 20) -> None:
        """Graceful: SIGTERM, then wait."""
        self._end(signal.SIGTERM, timeout_s)

    def kill(self) -> None:
        self._end(signal.SIGKILL, 5)

    @property
    def running(self) -> bool:
        return self._wanted and self.process is not None and self.process.poll() is None

    def log_text(self) -> str:
        return self.log.read_text(encoding="utf-8", errors="replace") if self.log.exists() else ""


class Stack:
    """The system under test plus the means to drive and inspect it."""

    def __init__(self, log_dir: Path) -> None:
        self.settings = dotenv()
        self.prefix = f"e2e-{secrets.token_hex(5)}."
        self.log_dir = log_dir
        self.database_url = self._database_url(E2E_DATABASE)
        self.port, self.internal_port, self.site_port = free_port(), free_port(), free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.internal_url = f"http://127.0.0.1:{self.internal_port}"
        self.site_url = f"http://127.0.0.1:{self.site_port}"
        # A master key that exists only for this run.
        self.master_key = secrets.token_hex(32)
        engine_env = {
            **os.environ,
            "DATABASE_URL": self.database_url,
            "ENGINE_AMQP_URL": self.settings["ENGINE_AMQP_URL"],
            "ENGINE_HTTP_ADDR": f"127.0.0.1:{self.port}",
            "ENGINE_TOPOLOGY_PREFIX": self.prefix,
            "ENGINE_INTERNAL_ADDR": f"127.0.0.1:{self.internal_port}",
            "CREDENTIAL_MASTER_KEY": self.master_key,
            "ENABLE_DUMMY_PROVIDER": "true",
            "LOG_FORMAT": "json",
            "OUTBOX_POLL_MS": "50",
            "RETRY_SCHEDULER_INTERVAL_MS": "100",
            "REAPER_INTERVAL_MS": "200",
        }
        self._engine_env = engine_env
        self.engine_binary = str(ENGINE_DIR / "target" / "debug" / "courier-engine")
        self.engine = Supervised(
            "engine",
            [self.engine_binary, "serve"],
            ENGINE_DIR,
            engine_env,
            log_dir / "engine.log",
            restart=False,
        )
        worker_env = {
            **os.environ,
            # The Engine account: see the module docstring.
            "WORKER_AMQP_URL": self.settings["ENGINE_AMQP_URL"],
            "WORKER_TOPOLOGY_PREFIX": self.prefix,
            "WORKER_ALLOW_FAULT_INJECTION": "true",
            "WORKER_CONCURRENCY": "4",
            "WORKER_SHUTDOWN_GRACE_S": "5",
            "WORKER_ID": "e2e-worker",
            "LOG_FORMAT": "json",
        }
        python = str(ADAPTERS_DIR / ".venv" / "bin" / "python")

        def worker(adapter: str, **extra: str) -> Supervised:
            env = {**worker_env, "WORKER_ID": f"e2e-{adapter}", **extra}
            return Supervised(
                adapter,
                [python, "-m", f"adapters.{adapter}"],
                ADAPTERS_DIR,
                env,
                log_dir / f"worker-{adapter}.log",
                True,
            )

        self.workers = {
            "dummy": worker("dummy", WORKER_ID="e2e-worker"),
            # The scraping adapter is pointed at the demo courier website started below.
            "demosite": worker("demosite", DEMOSITE_BASE_URL=self.site_url),
            # The DHL adapter is pointed at the test's fake DHL API; its token is set per test.
            "dhl": worker("dhl", WORKER_ENGINE_INTERNAL_URL=self.internal_url),
        }
        self.worker = self.workers["dummy"]
        self.site = Supervised(
            "demo-site",
            [python, "-m", "demo_site"],
            ADAPTERS_DIR,
            {**os.environ, "DEMO_SITE_PORT": str(self.site_port)},
            log_dir / "demo-site.log",
            restart=False,
        )
        self._management = httpx.Client(
            base_url=f"http://127.0.0.1:{self.settings['RABBITMQ_MGMT_HOST_PORT']}/api",
            auth=(self.settings["RABBITMQ_ADMIN_USER"], self.settings["RABBITMQ_ADMIN_PASSWORD"]),
            timeout=10,
        )
        self._vhost = quote(self.settings["RABBITMQ_VHOST"], safe="")
        self._users = 0

    # ------------------------------------------------------------------ database

    def _database_url(self, database: str) -> str:
        parts = urlsplit(self.settings["DATABASE_URL"])
        return urlunsplit(parts._replace(path=f"/{database}"))

    def create_database(self) -> None:
        """A fresh database for this run. Only ``courier_e2e`` is ever dropped."""
        with psycopg.connect(self.settings["DATABASE_URL"], autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{E2E_DATABASE}" WITH (FORCE)')
            admin.execute(f'CREATE DATABASE "{E2E_DATABASE}"')
        subprocess.run(
            [self.engine_binary, "migrate"],
            cwd=ENGINE_DIR,
            env=self._engine_env,
            check=True,
            capture_output=True,
        )
        for code in PROVIDERS:
            self.set_provider(code, **PROVIDER_DEFAULTS)

    def query(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        """Read-only look into the Engine's database, for assertions."""
        with psycopg.connect(self.database_url, row_factory=dict_row) as connection:
            return connection.execute(sql, params).fetchall()

    def set_provider(self, code: str = "dummy", **timings: int) -> None:
        assert code in PROVIDERS
        assert set(timings) <= set(PROVIDER_DEFAULTS)
        assignments = ", ".join(f"{column} = %s" for column in timings)
        with psycopg.connect(self.database_url, autocommit=True) as connection:
            connection.execute(
                f"UPDATE providers SET {assignments}, next_dispatch_at = NULL WHERE code = %s",
                [*timings.values(), code],
            )

    @contextmanager
    def provider(self, code: str = "dummy", **timings: int) -> Iterator[None]:
        """Temporarily different timings for a provider."""
        self.set_provider(code, **timings)
        try:
            yield
        finally:
            self.set_provider(code, **PROVIDER_DEFAULTS)

    def admin(self, *arguments: str, stdin: str | None = None) -> str:
        """Runs an Engine admin command and returns what it printed."""
        done = subprocess.run(
            [self.engine_binary, "admin", *arguments],
            cwd=ENGINE_DIR,
            env=self._engine_env,
            input=stdin,
            capture_output=True,
            text=True,
        )
        assert done.returncode == 0, done.stderr
        return done.stdout

    # ------------------------------------------------------------------ processes

    def start_engine(self) -> None:
        self.engine.start()
        wait_until(self._ready, 30, "the Engine to become ready")

    def _ready(self) -> bool:
        try:
            return httpx.get(f"{self.base_url}/readyz", timeout=2).status_code == 200
        except httpx.HTTPError:
            return False

    def start_worker(self, concurrency: int = 4, adapter: str = "dummy", **env: str) -> None:
        self.workers[adapter].start(WORKER_CONCURRENCY=str(concurrency), **env)
        wait_until(lambda: self.consumers(f"adapter.{adapter}.jobs") > 0, 30, f"the {adapter} worker to consume")

    def stop_worker(self, adapter: str) -> None:
        self.workers[adapter].stop()
        wait_until(lambda: self.consumers(f"adapter.{adapter}.jobs") == 0, 10, f"the {adapter} worker to disconnect")

    def start_site(self) -> None:
        self.site.start()
        wait_until(lambda: self._up(f"{self.site_url}/healthz"), 15, "the demo courier website")

    @staticmethod
    def _up(url: str) -> bool:
        try:
            return httpx.get(url, timeout=2).status_code == 200
        except httpx.HTTPError:
            return False

    def log_files(self) -> dict[str, str]:
        """Every log the run has written so far, by file name."""
        return {path.name: path.read_text(encoding="utf-8", errors="replace") for path in self.log_dir.glob("*.log")}

    def restart_worker(self, concurrency: int = 4, purge: bool = False) -> None:
        self.worker.stop()
        if purge:
            wait_until(lambda: self.consumers("adapter.dummy.jobs") == 0, 10, "the worker to disconnect")
            self.purge("adapter.dummy.jobs")
        self.start_worker(concurrency)

    # ------------------------------------------------------------------ RabbitMQ (management API)

    def _queue(self, logical: str) -> str:
        return f"/queues/{self._vhost}/{quote(self.prefix + logical, safe='')}"

    def consumers(self, queue: str) -> int:
        response = self._management.get(self._queue(queue))
        return int(response.json().get("consumers", 0)) if response.status_code == 200 else 0

    def purge(self, queue: str) -> None:
        self._management.delete(f"{self._queue(queue)}/contents").raise_for_status()

    def take(self, queue: str, count: int = 50) -> list[dict[str, Any]]:
        """Removes and returns up to ``count`` messages from a queue."""
        response = self._management.post(
            f"{self._queue(queue)}/get",
            json={"count": count, "ackmode": "ack_requeue_false", "encoding": "auto"},
        )
        response.raise_for_status()
        return list(response.json())

    def publish(self, exchange: str, routing_key: str, body: str) -> None:
        response = self._management.post(
            f"/exchanges/{self._vhost}/{quote(self.prefix + exchange, safe='')}/publish",
            json={
                "properties": {"delivery_mode": 2, "content_type": "application/json"},
                "routing_key": routing_key,
                "payload": body,
                "payload_encoding": "string",
            },
        )
        response.raise_for_status()
        assert response.json()["routed"] is True

    def delete_topology(self) -> None:
        for kind in ("queues", "exchanges"):
            for item in self._management.get(f"/{kind}/{self._vhost}", params={"columns": "name"}).json():
                if item["name"].startswith(self.prefix):
                    self._management.delete(f"/{kind}/{self._vhost}/{quote(item['name'], safe='')}")

    # ------------------------------------------------------------------ API

    def new_user(self) -> User:
        """Creates a user with the Engine's admin command and returns an API client for them."""
        self._users += 1
        email = f"user{self._users}-{secrets.token_hex(3)}@e2e.example"
        output = subprocess.run(
            [self.engine_binary, "admin", "create-user", "--email", email],
            cwd=ENGINE_DIR,
            env=self._engine_env,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        key = next(line.split(": ", 1)[1] for line in output.splitlines() if line.startswith("api_key: "))
        return User(self, email, key)

    def shutdown(self) -> None:
        for worker in self.workers.values():
            worker.stop()
        self.site.stop()
        self.engine.stop()
        self.delete_topology()
        self._management.close()


class User:
    """One API user."""

    def __init__(self, stack: Stack, email: str, api_key: str) -> None:
        self.stack, self.email, self.api_key = stack, email, api_key
        self.http = httpx.Client(
            base_url=f"{stack.base_url}/v1", headers={"Authorization": f"Bearer {api_key}"}, timeout=15
        )

    def track(self, tracking_number: str, provider: str = "dummy") -> httpx.Response:
        return self.http.post(
            "/tracking-requests", json={"provider_code": provider, "tracking_number": tracking_number}
        )

    def submit(self, tracking_number: str, provider: str = "dummy") -> str:
        """Creates a tracking request and returns its ID."""
        response = self.track(tracking_number, provider)
        assert response.status_code in (200, 201), response.text
        return str(response.json()["id"])

    def request(self, request_id: str) -> dict[str, Any]:
        response = self.http.get(f"/tracking-requests/{request_id}")
        assert response.status_code == 200, response.text
        return dict(response.json())

    def job(self, request_id: str) -> dict[str, Any]:
        return dict(self.request(request_id)["latest_job"])

    def events(self, request_id: str) -> list[dict[str, Any]]:
        return list(self.http.get(f"/tracking-requests/{request_id}/events").json()["items"])

    def wait_for(self, request_id: str, *statuses: str, timeout_s: float = 20) -> dict[str, Any]:
        """Waits until the request's latest job has one of the statuses; returns the request."""
        wanted = statuses or TERMINAL

        def probe() -> dict[str, Any] | None:
            view = self.request(request_id)
            return view if view["latest_job"]["status"] in wanted else None

        return wait_until(probe, timeout_s, f"job of {request_id} to be {' or '.join(wanted)}")


def number(kind: str, family: str = "DUMMY") -> str:
    """A unique tracking number with the given behaviour, e.g. ``DUMMY-OK-3F9A1C02``."""
    return f"{family}-{kind}-{secrets.token_hex(4).upper()}"


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])  # the Engine stores numbers in upper case
