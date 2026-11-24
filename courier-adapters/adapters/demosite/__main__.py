"""Run the demo-site adapter worker: ``uv run python -m adapters.demosite``."""

from courier_adapter_sdk import run

from . import DemoSiteAdapter

if __name__ == "__main__":
    run(DemoSiteAdapter())
