"""Run the DHL adapter worker: ``uv run python -m adapters.dhl``."""

from courier_adapter_sdk import run

from . import DhlAdapter

if __name__ == "__main__":
    run(DhlAdapter())
