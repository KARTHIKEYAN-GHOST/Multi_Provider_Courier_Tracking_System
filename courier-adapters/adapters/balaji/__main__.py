"""Run the Balaji adapter worker: ``uv run python -m adapters.balaji``."""

from courier_adapter_sdk import run

from . import BalajiAdapter

if __name__ == "__main__":
    run(BalajiAdapter())
