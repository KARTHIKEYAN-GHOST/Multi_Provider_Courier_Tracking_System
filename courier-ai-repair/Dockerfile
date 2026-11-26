# AI repair service with its agent. Build from the repository root:
#   docker build -f courier-ai-repair/Dockerfile .
#
# The image holds three things: the service, a headless browser for the agent, and a Python
# environment of courier-adapters with the linter, the type checker and pytest, in which
# generated code is checked inside a bubblewrap sandbox. It holds no source of the
# adapters: every task works on a fresh clone from GitHub.

FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/browsers

RUN apt-get update \
    && apt-get install -y --no-install-recommends git bubblewrap ca-certificates libatomic1 \
    && rm -rf /var/lib/apt/lists/*

# (libatomic1: Playwright's driver needs it and the slim image lacks it.)

# The environment the checks run with (development tools included).
WORKDIR /app/courier-adapters
COPY courier-adapters/pyproject.toml courier-adapters/uv.lock courier-adapters/.python-version ./
RUN uv sync --frozen

WORKDIR /app/courier-ai-repair
COPY courier-ai-repair/pyproject.toml courier-ai-repair/uv.lock courier-ai-repair/.python-version ./
RUN uv sync --frozen --no-dev \
    && .venv/bin/playwright install --with-deps chromium-headless-shell \
    && rm -rf /var/lib/apt/lists/*
COPY courier-ai-repair/ai_repair ai_repair

RUN useradd --system --uid 10003 --create-home agent
USER agent
# No secret is baked in: the three tokens come from the environment.
ENV AI_REPAIR_ADAPTERS_VENV=/app/courier-adapters/.venv \
    AI_REPAIR_WORK_DIR=/tmp/courier-ai-repair
ENTRYPOINT ["/app/courier-ai-repair/.venv/bin/python", "-m", "ai_repair"]
