"""Run the Safexpress adapter worker: ``uv run python -m adapters.safexpress``."""

from courier_adapter_sdk import run

from . import SafexpressAdapter

if __name__ == "__main__":
    run(SafexpressAdapter())
