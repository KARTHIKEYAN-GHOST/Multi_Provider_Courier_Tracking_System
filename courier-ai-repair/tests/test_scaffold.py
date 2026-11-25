"""The files a new provider gets besides its adapter."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from ai_repair.scaffold import ProviderSpec, ScaffoldError, scaffold

REQUEST: dict[str, object] = {
    "code": "fastship", "name": "FastShip Express", "method": "api", "hosts": ["api.fastship.example"],
    "needs_credential": True, "notes": "GET /track?awb=", "sample": None,
}  # fmt: skip


def test_a_new_provider_gets_queues_database_rows_and_a_worker_service(source: Path) -> None:
    paths = scaffold(source, ProviderSpec.from_request(REQUEST))
    numbers = sorted(int(p.name[:4]) for p in (source / "courier-engine/migrations").glob("*.sql"))
    assert numbers == list(range(1, len(numbers) + 1)), "the new migration takes the next number"
    migration = f"courier-engine/migrations/{numbers[-1]:04d}_provider_fastship.sql"
    assert paths == ["courier-contracts/topology.yaml", "infrastructure/docker-compose.yml", migration]

    queues = {q["name"]: q for q in yaml.safe_load((source / paths[0]).read_text())["queues"]}
    job, dlq = queues["adapter.fastship.jobs"], queues["adapter.fastship.jobs.dlq"]
    assert job["bindings"] == [
        {"exchange": "courier.jobs", "routing_key": "job.track.v1.fastship"},
        {"exchange": "courier.jobs", "routing_key": "job.resume.v1.fastship"},
    ]
    assert (job["consumer"], job["durable"]) == ("adapter-worker", True)
    assert job["arguments"] == queues["adapter.balaji.jobs"]["arguments"] | {
        "x-dead-letter-routing-key": "adapter.fastship.jobs"
    }
    assert (dlq["consumer"], dlq["bindings"][0]["routing_key"]) == (
        "engine-dead-letter-consumer", "adapter.fastship.jobs",
    )  # fmt: skip

    services = yaml.safe_load((source / paths[1]).read_text())["services"]
    worker, model = services["adapter-fastship"], services["adapter-delhivery"]
    assert worker["command"] == ["-m", "adapters.fastship"]
    assert worker["profiles"] == ["couriers", "fastship"]
    assert worker["build"] == model["build"]
    assert set(worker["environment"]) == set(model["environment"]) - {"DELHIVERY_BASE_URL"}
    assert worker["environment"]["WORKER_TOKEN"] == "${FASTSHIP_WORKER_TOKEN:-}"

    sql = (source / migration).read_text()
    assert "'fastship', 'FastShip Express', 'api'" in sql
    assert "300000, true, 2000);" in sql, "an API provider with a credential starts unusable"
    assert "'fastship', 'api', 'adapter.fastship.jobs'" in sql
    assert sql.count("INSERT INTO") == 3
    assert sql.count("\n    ('") == 7, "the seven status codes of adapters/india.py"

    with pytest.raises(ScaffoldError, match="already"):
        scaffold(source, ProviderSpec.from_request(REQUEST))


def test_a_website_provider_gets_no_token_and_waits_for_permission(source: Path) -> None:
    spec = ProviderSpec.from_request({**REQUEST, "method": "scrape", "needs_credential": True})
    assert spec.needs_credential is False, "a website has no API credential"
    paths = scaffold(source, spec)
    worker = yaml.safe_load((source / paths[1]).read_text())["services"]["adapter-fastship"]
    assert "WORKER_TOKEN" not in worker["environment"]
    sql = (source / paths[2]).read_text()
    assert "300000, false, 2000);" in sql
    assert "scraping_authorized" not in sql, "left at its default: not permitted"
    assert "set-scraping-authorized" in sql


@pytest.mark.parametrize(
    "change",
    [
        {"code": "dhl; DROP"},
        {"code": "../x"},
        {"code": "Fast"},
        {"name": "x'); DELETE FROM users; --"},
        {"name": "a\nb"},
        {"name": ""},
        {"method": "browser"},
        {"hosts": []},
        {"hosts": "api.fastship.example"},
        {"hosts": ["https://api.fastship.example"]},
        {"hosts": ["api.fastship.example/x"]},
        {"hosts": ["localhost"]},
        {"hosts": ["a.example"] * 6},
    ],
)
def test_a_request_that_does_not_fit_the_templates_is_refused(change: dict[str, object]) -> None:
    with pytest.raises(ScaffoldError):
        ProviderSpec.from_request({**REQUEST, **change})


def test_an_existing_adapter_is_never_overwritten(source: Path) -> None:
    with pytest.raises(ScaffoldError, match="already exists"):
        scaffold(source, ProviderSpec.from_request({**REQUEST, "code": "dhl"}))
    assert "fastship" not in (source / "courier-contracts/topology.yaml").read_text()
