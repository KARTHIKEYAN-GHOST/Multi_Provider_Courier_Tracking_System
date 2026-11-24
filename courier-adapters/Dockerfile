# Adapter worker image. Build from the repository root:
#   docker build -f courier-adapters/Dockerfile .
# (the worker reads courier-contracts/topology.yaml at start-up)

FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PYTHONUNBUFFERED=1

WORKDIR /app/courier-adapters
COPY courier-adapters/pyproject.toml courier-adapters/uv.lock courier-adapters/.python-version ./
RUN uv sync --frozen --no-dev
COPY courier-contracts/topology.yaml /app/courier-contracts/topology.yaml
COPY courier-adapters/courier_adapter_sdk courier_adapter_sdk
COPY courier-adapters/adapters adapters
COPY courier-adapters/demo_site demo_site

RUN useradd --system --uid 10001 --no-create-home worker
USER worker
# No secrets are baked in: WORKER_AMQP_URL and WORKER_TOKEN come from the environment.
# Which adapter runs is chosen by the command, e.g. "-m adapters.dummy".
ENTRYPOINT ["/app/courier-adapters/.venv/bin/python"]
CMD ["-m", "adapters.dummy"]
