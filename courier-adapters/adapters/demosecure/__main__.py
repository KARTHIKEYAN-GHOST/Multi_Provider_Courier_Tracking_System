"""Run the Demo Secure Logistics adapter worker: ``uv run python -m adapters.demosecure``."""

from courier_adapter_sdk import run

from . import DemoSecureAdapter

if __name__ == "__main__":
    run(DemoSecureAdapter())
