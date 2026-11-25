"""Run the ABT adapter worker: ``uv run python -m adapters.abt``."""

from courier_adapter_sdk import run

from . import AbtAdapter

if __name__ == "__main__":
    run(AbtAdapter())
