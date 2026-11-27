# Courier Tracking System — developer entry points.
# Targets are added phase by phase; every target listed here works today.

SHELL := /bin/bash
.DEFAULT_GOAL := help

# Override if your user cannot reach the Docker daemon directly: make up-deps DOCKER="sudo docker"
DOCKER  ?= docker
COMPOSE := $(DOCKER) compose --env-file .env -f infrastructure/docker-compose.yml -p courier

.PHONY: check-ai check-ai-push help env setup compose-config up-deps verify-deps down destroy ps logs migrate run-engine update-contract-fixtures test-contracts test-engine test-adapters run-worker test up up-dhl up-couriers smoke test-e2e master-key run-demo-site maintenance unmapped session-key test-human-interface run-human-interface test-ai-repair run-ai-repair repair-token repairs auto-update up-ai seed-test-data worker-tokens deploy

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "}; {printf "  %-16s %s\n", $$1, $$2}'

env: ## Create .env from .env.example if it does not exist
	@test -f .env || (cp .env.example .env && echo "created .env from .env.example")

setup: env ## Install project-local Python environments (uv-managed Python 3.13; system Python untouched)
	cd courier-contracts && uv sync
	cd courier-adapters && uv sync
	cd tests-e2e && uv sync
	cd courier-human-interface && uv sync
	cd courier-ai-repair && uv sync && uv run playwright install chromium-headless-shell

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

up-ai: env ## Additionally start the AI repair service with its agent in a container (see docs/phase-10.md)
	$(COMPOSE) --profile ai up -d --build --wait

check-ai: ## Check the running AI repair container without any model: sandbox and browser (needs make up-ai)
	tar --exclude=.venv --exclude=__pycache__ --exclude='.*_cache' -cf - courier-adapters courier-contracts \
		| $(DOCKER) exec -i courier-ai-repair-1 sh -c 'rm -rf /tmp/selfcheck && mkdir /tmp/selfcheck && tar -C /tmp/selfcheck -xf -'
	$(DOCKER) exec courier-ai-repair-1 /app/courier-ai-repair/.venv/bin/python -m ai_repair.selfcheck /tmp/selfcheck "http://demo-site:8090/track?number=DEMO-OK-1" $(SELFCHECK_FLAGS)

check-ai-push: ## Like check-ai, and also PUSHES one small test file to GitHub the way a finished repair does
	$(MAKE) check-ai SELFCHECK_FLAGS=--push

up-couriers: env ## Additionally start the workers for Delhivery, Safexpress, ABT, Balaji and The Professional Couriers (see docs/indian-couriers.md)
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

repair-token: ## Print a new random token for ENGINE_REPAIR_TOKEN (Engine and AI repair service share it)
	@openssl rand -hex 32

run-ai-repair: env ## Run the AI repair service (needs a running Engine and the settings in docs/phase-9.md)
	cd courier-ai-repair && AI_REPAIR_ADAPTERS_VENV=$(CURDIR)/courier-adapters/.venv uv run python -m ai_repair

repairs: env ## List the most recent AI repair tasks and their outcome
	cd courier-engine && cargo run --quiet -- admin list-repairs

test-ai-repair: ## AI repair service: ruff, mypy, pytest (the sandbox tests need bubblewrap and courier-adapters/.venv)
	cd courier-ai-repair && uv run ruff format --check .
	cd courier-ai-repair && uv run ruff check .
	cd courier-ai-repair && uv run mypy
	cd courier-ai-repair && uv run pytest

seed-test-data: env ## Put the test organizations, users and shipments of infrastructure/test-data.sql into the database and print their API keys
	set -a; . ./.env; set +a; \
	$(COMPOSE) exec -T postgres psql -q -v ON_ERROR_STOP=1 -U "$$POSTGRES_USER" -d "$$POSTGRES_DB" < infrastructure/test-data.sql
	@cd courier-engine && for email in asha@acme.example.com ravi@acme.example.com meena@globex.example.com arun@globex.example.com ops@platform.example.com; do \
	    printf '%-28s ' "$$email"; cargo run --quiet -- admin create-api-key --email "$$email" --name test-data | sed -n 's/^api_key: //p'; \
	done

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

test: test-contracts test-engine test-adapters test-human-interface test-ai-repair test-e2e ## Run every test suite

worker-tokens: env ## Give the workers of AI-written adapters their tokens (in infrastructure/worker-tokens/, never printed)
	COMPOSE="$(COMPOSE)" infrastructure/worker-tokens.sh

deploy: env ## Put everything into service: the stack, tokens for new workers, and every courier's worker (what the automatic update runs)
	$(MAKE) up
	$(MAKE) worker-tokens
	$(COMPOSE) --profile couriers up -d --build

auto-update: ## Follow GitHub: pull new commits (e.g. adapters pushed by the AI), check them and redeploy; runs until Ctrl+C
	infrastructure/auto-update.sh
