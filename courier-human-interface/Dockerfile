# Human interface image. Build from the repository root:
#   docker build -f courier-human-interface/Dockerfile .

FROM python:3.13-slim
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PYTHONUNBUFFERED=1

WORKDIR /app/courier-human-interface
COPY courier-human-interface/pyproject.toml courier-human-interface/uv.lock courier-human-interface/.python-version ./
RUN uv sync --frozen --no-dev
COPY courier-human-interface/human_interface human_interface

RUN useradd --system --uid 10002 --no-create-home web
USER web
# No secrets are baked in or configured: users sign in with their own API key.
ENV HUMAN_INTERFACE_HOST=0.0.0.0
EXPOSE 8088
ENTRYPOINT ["/app/courier-human-interface/.venv/bin/python", "-m", "human_interface"]
