"""Fault injection for test adapters.

A test adapter (the dummy adapter) raises these to make the worker itself misbehave, so the
Engine's recovery paths can be exercised end to end. They are honoured only when
``WORKER_ALLOW_FAULT_INJECTION`` is true; otherwise they are reported as an ordinary
``INTERNAL`` failure, so a production worker can never be told to crash or hang.
"""

from __future__ import annotations

from .base import TrackingResult


class FaultInjection(Exception):
    """Base class for simulated worker faults."""


class SimulateCrash(FaultInjection):
    """The worker process dies now: no result, no acknowledgement."""


class SimulateHang(FaultInjection):
    """The worker stops responding for this job without exiting: started, but never a result."""


class SimulateDuplicateResult(FaultInjection):
    """The worker publishes the identical result message twice."""

    def __init__(self, result: TrackingResult) -> None:
        super().__init__("duplicate result")
        self.result = result
