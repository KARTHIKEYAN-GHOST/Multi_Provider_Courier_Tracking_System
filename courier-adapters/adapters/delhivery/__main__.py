"""Run the Delhivery adapter worker: ``uv run python -m adapters.delhivery``."""

from courier_adapter_sdk import run

from . import DelhiveryAdapter

if __name__ == "__main__":
    run(DelhiveryAdapter())
