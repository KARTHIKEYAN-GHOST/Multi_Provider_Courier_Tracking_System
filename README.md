# Courier Tracking System

A multi-provider courier tracking system: a Rust Engine that owns the database and business logic, Python adapter workers that talk to courier providers, and RabbitMQ between them.

- [docs/architecture.md](docs/architecture.md) — system-wide architecture
- [docs/milestone-1.md](docs/milestone-1.md) — Milestone 1 plan and acceptance criteria

## Status

| Phase | State |
|---|---|
| 1 — Foundation (layout, Compose, contracts) | done |
| 2 — Engine foundation (migrations, API keys, endpoints, transactional dispatch) | done |
| 2.1 — Hardening (database integrity constraints, cross-language contract test, refresh rules) | done |
| 3 — RabbitMQ integration | not started |
| 4 — Adapter SDK and dummy adapter | not started |
| 5 — First end-to-end integration | not started |

## Layout

| Path | Contents |
|---|---|
| `courier-engine/` | Rust Engine: HTTP API, database migrations, business logic |
| `courier-contracts/` | JSON Schemas, fixtures and RabbitMQ topology shared by Rust and Python |
| `infrastructure/` | Docker Compose, PostgreSQL and RabbitMQ configuration |
| `docs/` | Architecture and plans |

`courier-adapters/` and `tests-e2e/` are added in Phases 4 and 5.

## Requirements

- Docker with the Compose plugin, and permission to use the Docker daemon
- [uv](https://docs.astral.sh/uv/) — it downloads its own Python 3.13; the system Python is not used or changed
- Rust (from Phase 2)

## Quick start

```
make setup            # project-local Python environments
make test-contracts   # schemas, fixtures and topology
make up-deps          # PostgreSQL + RabbitMQ, waits until healthy
make verify-deps      # databases, vhost, users, permissions
make migrate          # apply database migrations
make test-engine      # Engine: fmt, clippy, unit + integration tests
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

Until Phase 3 connects RabbitMQ, a created job stays `QUEUED` and its `job.track` message waits in the `outbox_messages` table.

`make help` lists every target. Configuration lives in `.env`, created from `.env.example` on first use.

PostgreSQL is published on host port **55432** (not 5432, which a local PostgreSQL often occupies). The RabbitMQ management UI is at http://localhost:15672.

If your user cannot reach the Docker daemon, either add yourself to the `docker` group or run `make up-deps DOCKER="sudo docker"`.
