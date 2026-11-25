# Courier Tracking System

A multi-provider courier tracking system: a Rust Engine that owns the database and business logic, Python adapter workers that talk to courier providers, and RabbitMQ between them.

- [docs/architecture.md](docs/architecture.md) — system-wide architecture
- [docs/contracts.md](docs/contracts.md) — the messages between Engine and workers
- [docs/milestone-1.md](docs/milestone-1.md) — Milestone 1 plan and acceptance criteria
- [docs/phase-6.md](docs/phase-6.md) — credentials, the DHL and demo-site adapters, rate limiting
- [docs/phase-7.md](docs/phase-7.md) — automatic re-polling, status-mapping management, archiving and retention

## Status

| Phase | State |
|---|---|
| 1 — Foundation (layout, Compose, contracts) | done |
| 2 — Engine foundation (migrations, API keys, endpoints, transactional dispatch) | done |
| 2.1 — Hardening (database integrity constraints, cross-language contract test, refresh rules) | done |
| 3 — RabbitMQ integration (outbox relay, event and dead-letter consumers, retry scheduler, reaper) | done |
| 4 — Adapter SDK and dummy adapter (worker runtime, concurrency, fault injection) | done |
| 5 — First end-to-end integration (containers, end-to-end suite) | done |
| 6 — Encrypted credentials and resolve API, DHL adapter, scraping adapter (demo site), provider rate limiting | done |
| 7 — Automatic re-polling, status-mapping management, archiving and retention | done |

## Layout

| Path | Contents |
|---|---|
| `courier-engine/` | Rust Engine: HTTP API, database migrations, business logic |
| `courier-contracts/` | JSON Schemas, fixtures and RabbitMQ topology shared by Rust and Python |
| `courier-adapters/` | Python Adapter SDK (worker runtime, message contracts, credentials, HTTP), the dummy, DHL and demo-site adapters, and the demo courier website |
| `infrastructure/` | Docker Compose, PostgreSQL and RabbitMQ configuration |
| `tests-e2e/` | End-to-end tests: real Engine and worker processes on a private database and queues |
| `docs/` | Architecture, contracts and plans |

## Requirements

- Docker with the Compose plugin, and permission to use the Docker daemon
- [uv](https://docs.astral.sh/uv/) — it downloads its own Python 3.13; the system Python is not used or changed
- Rust

## Quick start

```
make setup            # project-local Python environments
make test-contracts   # schemas, fixtures and topology
make up-deps          # PostgreSQL + RabbitMQ, waits until healthy
make verify-deps      # databases, vhost, users, permissions
make migrate          # apply database migrations
make test-engine      # Engine: fmt, clippy, unit + integration tests
make test-adapters    # Adapter SDK + adapters: ruff, mypy, pytest
make test-e2e         # Engine + workers end to end (about 3 min)
make up               # full stack in containers: + Engine, dummy and demo-site workers, demo website
make master-key       # a new CREDENTIAL_MASTER_KEY (needed for DHL; see docs/phase-6.md)
make smoke            # one tracking request through the containers
make update-contract-fixtures   # only after intentionally changing a message the Engine sends
make down             # stop (data kept); make destroy also deletes the volumes
```

### Trying the API

```
cd courier-engine
cargo run -- admin create-user --email you@example.com    # prints an API key once
cargo run -- serve                                         # http://127.0.0.1:8080

curl -H "Authorization: Bearer <key>" -H 'content-type: application/json' \
     -d '{"provider_code":"dummy","tracking_number":"DUMMY-OK-1"}' \
     http://127.0.0.1:8080/v1/tracking-requests
```

The Engine publishes each job to `adapter.dummy.jobs`. Start the dummy worker in a second terminal to process it:

```
make run-worker       # WORKER_CONCURRENCY=4 by default
```

The tracking-number prefix selects the dummy adapter's behaviour: `DUMMY-OK-`, `DUMMY-TRANSIT-`, `DUMMY-ODD-`, `DUMMY-NOTFOUND-`, `DUMMY-FAIL-`, `DUMMY-FATAL-`, `DUMMY-FLAKY-`, `DUMMY-SLOW-`, `DUMMY-DELAY-<ms>-`. The fault prefixes `DUMMY-HANG-`, `DUMMY-CRASHONCE-`, `DUMMY-CRASH-` and `DUMMY-DUP-` only act when the worker runs with `WORKER_ALLOW_FAULT_INJECTION=true`; otherwise they are reported as an `INTERNAL` failure. Without a running worker a job stays `QUEUED` until its queue-wait deadline (15 minutes), is retried, and finally ends `FAILED` with `QUEUE_WAIT_EXCEEDED`.

`make help` lists every target. Configuration lives in `.env`, created from `.env.example` on first use.

PostgreSQL is published on host port **55432** (not 5432, which a local PostgreSQL often occupies). The RabbitMQ management UI is at http://localhost:15672.

If your user cannot reach the Docker daemon, either add yourself to the `docker` group or run `make up-deps DOCKER="sudo docker"`.
