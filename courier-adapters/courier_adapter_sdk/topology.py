"""The worker's view of the RabbitMQ topology in ``courier-contracts/topology.yaml``.

Workers never create exchanges or queues: the Engine declares them, and the worker account
has no ``configure`` permission. A worker only needs to know which queue it reads and where
it publishes its events.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from .contracts import JOB_RESULT, JOB_STARTED, JOB_TRACK


@dataclass(frozen=True)
class WorkerRoutes:
    #: Queue this adapter's jobs arrive on.
    job_queue: str
    #: Exchange that worker events are published to.
    events_exchange: str
    started_routing_key: str
    result_routing_key: str
    max_message_bytes: int


def load_raw(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a topology file")
    return data


def routes_for(adapter_key: str, path: Path, prefix: str = "") -> WorkerRoutes:
    """Finds the adapter's job queue through the routing key its jobs are published with."""
    topology = load_raw(path)
    routing = topology["routing"]
    job_key = routing[JOB_TRACK]["routing_key"].format(adapter_key=adapter_key)
    job_exchange = routing[JOB_TRACK]["exchange"]

    queues = [
        queue["name"]
        for queue in topology["queues"]
        for binding in queue.get("bindings", [])
        if binding["exchange"] == job_exchange and binding["routing_key"] == job_key
    ]
    if len(queues) != 1:
        raise ValueError(
            f"topology has {len(queues)} queues bound for {job_key!r}; "
            f"adapter {adapter_key!r} needs exactly one"
        )
    if routing[JOB_STARTED]["exchange"] != routing[JOB_RESULT]["exchange"]:
        raise ValueError("job.started and job.result must share one exchange")

    return WorkerRoutes(
        job_queue=prefix + queues[0],
        events_exchange=prefix + routing[JOB_RESULT]["exchange"],
        started_routing_key=routing[JOB_STARTED]["routing_key"],
        result_routing_key=routing[JOB_RESULT]["routing_key"],
        max_message_bytes=int(topology["limits"]["max_message_bytes"]),
    )
