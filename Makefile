# Courier Tracking System — developer entry points.
# Targets are added phase by phase; every target listed here works today.

SHELL := /bin/bash
.DEFAULT_GOAL := help

# Override if your user cannot reach the Docker daemon directly: make up-deps DOCKER="sudo docker"
DOCKER  ?= docker
COMPOSE := $(DOCKER) compose --env-file .env -f infrastructure/docker-compose.yml -p courier

.PHONY: help env setup compose-config up-deps verify-deps down destroy ps logs migrate run-engine update-contract-fixtures test-contracts test-engine test-adapters run-worker test up up-dhl up-couriers smoke test-e2e master-key run-demo-site maintenance unmapped session-key test-human-interface run-human-interface

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-16s %s\n", $$1, $$2}'

env: ## Create .env from .env.example if it does not exist
	@test -f .env || (cp .env.example .env && echo "created .env from .env.example")

setup: env ## Install project-local Python environments (uv-managed Python 3.13; system Python untouched)
	cd courier-contracts && uv sync
	cd courier-adapters && uv sync
	cd tests-e2e && uv sync
	cd courier-human-interface && uv sync

compose-config: env ## Validate the Compose file (does not need a running Docker daemon)
	$(COMPOSE) config --quiet && echo "compose file OK"

up-deps: env ## Start PostgreSQL, RabbitMQ and Redis and wait until they are healthy
	$(COMPOSE) up -d --wait postgres rabbitmq redis

verify-deps: env ## Check databases, RabbitMQ vhost, users and permissions
	bash infrastructure/verify-deps.sh "$(DOCKER)"

up: env ## Build and start the stack: PostgreSQL, RabbitMQ, Redis, Engine, dummy + demo-site workers, demo courier website, human interface
	$(COMPOSE) up -d --build --wait

up-dhl: env ## Additionally start the DHL worker (needs CREDENTIAL_MASTER_KEY and DHL_WORKER_TOKEN in .env; see docs/phase-6.md)
	$(COMPOSE) --profile dhl up -d --build --wait

up-couriers: env ## Additionally start the workers for Delhivery, Safexpress, SkyKing, ABT and Balaji (see docs/indian-couriers.md)
	$(COMPOSE) --profile couriers up -d --build --wait

master-key: ## Print a new random master key for CREDENTIAL_MASTER_KEY (shown once; nothing is stored)
	@cd courier-engine && cargo run --quiet -- admin generate-master-key

session-key: ## Print a new random key for WORKER_SESSION_KEY (shown once; nothing is stored)
	@cd courier-adapters && uv run python -c "import secrets; print(secrets.token_hex(32))"

run-human-interface: env ## Run the human interface on http://127.0.0.1:8088 (needs a running Engine)
	cd courier-human-interface && uv run python -m human_interface

test-human-interface: ## Human interface: ruff, mypy, pytest (needs nothing running)
	cd courier-human-interface && uv run ruff format --check .
	cd courier-human-interface && uv run ruff check .
	cd courier-human-interface && uv run mypy
	cd courier-human-interface && uv run pytest

maintenance: env ## Run archiving and retention once, now (the Engine also does this periodically)
	cd courier-engine && cargo run --quiet -- admin run-maintenance

unmapped: env ## List provider statuses that have no mapping yet
	cd courier-engine && cargo run --quiet -- admin list-unmapped-statuses

run-demo-site: ## Run the demo courier website on http://127.0.0.1:8090 (target of the demo-site adapter)
	cd courier-adapters && uv run python -m demo_site

smoke: env ## Check the running full stack (make up) with one real tracking request
	bash infrastructure/smoke.sh "$(DOCKER)"

down: ## Stop containers (data volumes are kept)
	$(COMPOSE) down

destroy: ## Stop containers and delete data volumes
	$(COMPOSE) down --volumes

ps: ## Show container status
	$(COMPOSE) ps

logs: ## Follow container logs
	$(COMPOSE) logs -f --tail=100

test-contracts: ## Validate schemas, fixtures and topology
	cd courier-contracts && uv run pytest

migrate: env ## Apply database migrations (needs PostgreSQL: make up-deps)
	cd courier-engine && cargo run --quiet -- migrate

run-engine: env ## Run the Engine (API, outbox relay, consumers, scheduler, reaper) on ENGINE_HTTP_ADDR
	cd courier-engine && cargo run -- serve

test-engine: env ## Engine: format check, lints as errors, unit + integration tests (needs PostgreSQL and RabbitMQ: make up-deps)
	cd courier-engine && cargo fmt --check
	cd courier-engine && cargo clippy --all-targets -- -D warnings
	cd courier-engine && set -a && . ../.env && set +a && cargo test

update-contract-fixtures: ## Regenerate the Engine-produced contract fixtures (developer action; review and commit the diff)
	@test -z "$$CI" || (echo "refusing to regenerate contract fixtures in CI" && exit 1)
	cd courier-engine && UPDATE_CONTRACT_FIXTURES=1 cargo test --quiet --test contracts the_engines_job_track_message
	$(MAKE) test-contracts

test-adapters: env ## Adapter SDK and all adapters: ruff, mypy, pytest (needs RabbitMQ: make up-deps; the Redis test is skipped without Redis)
	cd courier-adapters && uv run ruff format --check .
	cd courier-adapters && uv run ruff check .
	cd courier-adapters && uv run mypy
	cd courier-adapters && uv run pytest

run-worker: env ## Run the dummy adapter worker (needs RabbitMQ and a running Engine, which declares the queues)
	cd courier-adapters && uv run python -m adapters.dummy

test-e2e: env ## End-to-end: real Engine + worker processes on a private database and queues (needs make up-deps)
	cd courier-engine && cargo build --quiet
	cd courier-adapters && uv sync --quiet
	cd courier-human-interface && uv sync --quiet
	cd tests-e2e && uv run ruff format --check . && uv run ruff check . && uv run pytest

test: test-contracts test-engine test-adapters test-human-interface test-e2e ## Run every test suite
