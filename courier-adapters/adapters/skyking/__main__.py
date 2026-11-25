"""Run the SkyKing adapter worker: ``uv run python -m adapters.skyking``."""

from courier_adapter_sdk import run

from . import SkykingAdapter

if __name__ == "__main__":
    run(SkykingAdapter())
