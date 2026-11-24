"""Run the dummy adapter worker: ``uv run python -m adapters.dummy``."""

from courier_adapter_sdk import run

from . import DummyAdapter

if __name__ == "__main__":
    run(DummyAdapter())
