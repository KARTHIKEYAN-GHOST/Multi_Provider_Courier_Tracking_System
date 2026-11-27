"""Run the Demo Express adapter worker: ``uv run python -m adapters.demoexpress``."""

from courier_adapter_sdk import run

from . import DemoExpressAdapter

if __name__ == "__main__":
    run(DemoExpressAdapter())
