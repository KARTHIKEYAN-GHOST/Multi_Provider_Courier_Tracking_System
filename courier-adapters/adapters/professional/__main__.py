"""Run the Professional adapter worker: ``uv run python -m adapters.professional``."""

from courier_adapter_sdk import run

from . import ProfessionalAdapter

if __name__ == "__main__":
    run(ProfessionalAdapter())
