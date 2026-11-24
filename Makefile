# Courier Tracking System — developer entry points.
# Targets are added phase by phase; every target listed here works today.

SHELL := /bin/bash
.DEFAULT_GOAL := help

# Override if your user cannot reach the Docker daemon directly: make up-deps DOCKER="sudo docker"
DOCKER  ?= docker
COMPOSE := $(DOCKER) compose --env-file .env -f infrastructure/docker-compose.yml -p courier

.PHONY: help env setup compose-config up-deps verify-deps down destroy ps logs migrate run-engine update-contract-fixtures test-contracts test-engine test-adapters run-worker test up up-dhl smoke test-e2e master-key run-demo-site

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-16s %s\n", $$1, $$2}'

env: ## Create .env from .env.example if it does not exist
	@test -f .env || (cp .env.example .env && echo "created .env from .env.example")

setup: env ## Install project-local Python environments (uv-managed Python 3.13; system Python untouched)
	cd courier-contracts && uv sync
	cd courier-adapters && uv sync
	cd tests-e2e && uv sync

compose-config: env ## Validate the Compose file (does not need a running Docker daemon)
	$(COMPOSE) config --quiet && echo "compose file OK"

up-deps: env ## Start PostgreSQL and RabbitMQ and wait until both are healthy
	$(COMPOSE) up -d --wait postgres rabbitmq

verify-deps: env ## Check databases, RabbitMQ vhost, users and permissions
	bash infrastructure/verify-deps.sh "$(DOCKER)"

up: env ## Build and start the stack: PostgreSQL, RabbitMQ, Engine, dummy + demo-site workers, demo courier website
	$(COMPOSE) up -d --build --wait

up-dhl: env ## Additionally start the DHL worker (needs CREDENTIAL_MASTER_KEY and DHL_WORKER_TOKEN in .env; see docs/phase-6.md)
	$(COMPOSE) --profile dhl up -d --build --wait

master-key: ## Print a new random master key for CREDENTIAL_MASTER_KEY (shown once; nothing is stored)
	@cd courier-engine && cargo run --quiet -- admin generate-master-key

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

test-adapters: env ## Adapter SDK and adapters (dummy, dhl, demosite): ruff, mypy, pytest (needs RabbitMQ: make up-deps)
	cd courier-adapters && uv run ruff format --check .
	cd courier-adapters && uv run ruff check .
	cd courier-adapters && uv run mypy
	cd courier-adapters && uv run pytest

run-worker: env ## Run the dummy adapter worker (needs RabbitMQ and a running Engine, which declares the queues)
	cd courier-adapters && uv run python -m adapters.dummy

test-e2e: env ## End-to-end: real Engine + worker processes on a private database and queues (needs make up-deps)
	cd courier-engine && cargo build --quiet
	cd courier-adapters && uv sync --quiet
	cd tests-e2e && uv run ruff format --check . && uv run ruff check . && uv run pytest

test: test-contracts test-engine test-adapters test-e2e ## Run every test suite
