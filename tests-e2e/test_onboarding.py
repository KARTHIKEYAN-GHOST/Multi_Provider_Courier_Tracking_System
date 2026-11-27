"""Phase 12: a courier added without an adapter, built by the AI, over the real Engine.

The AI itself is not run here (that needs a model and GitHub): the test plays the AI repair
service on the internal API, and applies the migration the service's own scaffold writes,
as the automatic update does once the AI has pushed. Everything else is the real Engine.
"""

from __future__ import annotations

import json
import secrets
import subprocess
from pathlib import Path
from typing import Any

import httpx
import psycopg

from harness import REPO, Stack, User, wait_until

PASSWORD = f"pw-{secrets.token_hex(8)}-MUST-NOT-LEAK"


def repair_api(stack: Stack, path: str) -> httpx.Response:
    return httpx.post(
        f"{stack.internal_url}/internal/v1/repairs{path}",
        headers={"Authorization": f"Bearer {stack.repair_token}"},
        timeout=10,
    )


def task_of(stack: Stack, code: str) -> dict[str, Any] | None:
    rows = stack.query(
        "SELECT id::text, status, request FROM repair_tasks WHERE adapter_key = %s ORDER BY created_at DESC", code
    )
    return rows[0] if rows else None


def scaffold_migration(tmp_path: Path, code: str, name: str, hosts: list[str]) -> str:
    """The migration the AI repair service's scaffold writes for this courier."""
    repo = tmp_path / "repo"
    (repo / "courier-engine").mkdir(parents=True)
    (repo / "courier-contracts").mkdir()
    (repo / "infrastructure").mkdir()
    (repo / "courier-adapters" / "adapters").mkdir(parents=True)
    subprocess.run(["cp", "-r", str(REPO / "courier-engine" / "migrations"), str(repo / "courier-engine")], check=True)
    for part in ("courier-contracts/topology.yaml", "infrastructure/docker-compose.yml"):
        (repo / part).write_text((REPO / part).read_text())
    script = (
        "import json, sys; from pathlib import Path; from ai_repair.scaffold import ProviderSpec, scaffold; "
        "print(scaffold(Path(sys.argv[1]), ProviderSpec.from_request(json.loads(sys.argv[2])))[2])"
    )
    spec = json.dumps({"code": code, "name": name, "method": "scrape", "hosts": hosts, "login": True, "notes": "x"})
    done = subprocess.run(
        ["uv", "run", "--offline", "--quiet", "python", "-c", script, str(repo), spec],
        cwd=REPO / "courier-ai-repair", capture_output=True, text=True, check=True,
    )  # fmt: skip
    return (repo / done.stdout.strip()).read_text()


def test_a_courier_added_by_a_user_is_built_and_its_waiting_lookups_start(
    stack: Stack, user: User, tmp_path: Path
) -> None:
    code = f"portal{secrets.token_hex(3)}"
    added = user.http.post(
        "/providers",
        json={
            "code": code,
            "name": "Portal Courier",
            "method": "scrape",
            "tracking_url": "https://track.portal.example/status",
            "notes": "Log in, enter the number, press Track.",
            "permitted": True,
            "login": {"email": "ops@acme.example", "password": PASSWORD},
        },
    )
    assert added.status_code == 202, added.text
    assert added.json() == {"code": code, "status": "building"}

    # The request is accepted and waits.
    request_id = user.submit("PORTAL123", provider=code)
    waiting = user.request(request_id)
    assert waiting["provider_status"] == "building"
    assert waiting["latest_job"] is None

    # The Engine asked for an adapter by itself; the AI repair service claims the task ...
    task = wait_until(lambda: task_of(stack, code), 10, "a new-adapter task")
    assert task["request"]["login"] is True
    assert PASSWORD not in str(task["request"])
    assert repair_api(stack, f"/{task['id']}/login").status_code == 204, "not before it is claimed"
    claimed = repair_api(stack, "/claim")
    assert claimed.status_code == 200, claimed.text
    assert claimed.json()["id"] == task["id"]
    assert PASSWORD not in claimed.text, "the task itself never carries the login"
    # ... and may fetch the login while it works on it.
    login = repair_api(stack, f"/{task['id']}/login")
    assert login.status_code == 200
    assert login.json()["login"]["password"] == PASSWORD
    assert login.headers["cache-control"] == "no-store"

    # The AI's adapter is published: its migration is applied (as by the automatic update).
    migration = scaffold_migration(tmp_path, code, "Portal Courier", ["track.portal.example"])
    with psycopg.connect(stack.database_url, autocommit=True) as connection:
        connection.execute(migration.encode())  # several statements: the simple protocol
    finished = httpx.post(
        f"{stack.internal_url}/internal/v1/repairs/{task['id']}/result",
        headers={"Authorization": f"Bearer {stack.repair_token}"},
        json={"status": "published", "commit_sha": "0" * 40, "summary": "new adapter"},
        timeout=10,
    )
    assert finished.status_code == 204, finished.text
    assert stack.query("SELECT status FROM providers WHERE code = %s", code)[0]["status"] == "live"
    # The user's own login and recorded permission were kept.
    kept = stack.query(
        "SELECT p.scraping_authorized, p.requires_credential, count(c.id) AS logins FROM providers p "
        "LEFT JOIN provider_credentials c ON c.provider_id = p.id AND c.revoked_at IS NULL "
        "WHERE p.code = %s GROUP BY p.id",
        code,
    )[0]
    assert (kept["scraping_authorized"], kept["requires_credential"], kept["logins"]) == (True, True, 1)

    # The waiting lookup starts by itself, with the organization's own login.
    stack.execute("UPDATE shipments SET next_poll_at = now() WHERE tracking_number = 'PORTAL123'")
    job = wait_until(lambda: user.request(request_id)["latest_job"], 15, "the waiting lookup to start")
    assert job["trigger"] == "poll"
    scope = stack.query("SELECT access_scope FROM shipments WHERE tracking_number = 'PORTAL123'")[0]
    assert scope["access_scope"].startswith("org:")

    # The login is nowhere in the Engine's logs.
    assert PASSWORD not in stack.engine.log_text()

    # No worker runs this made-up courier here: end the lookup, so later tests start quiet.
    assert user.http.delete(f"/tracking-requests/{request_id}").status_code in (200, 204)
    stack.execute("UPDATE providers SET max_attempts = 1 WHERE code = %s", code)
    stack.execute(
        "UPDATE tracking_jobs SET max_attempts = 1, queue_deadline_at = now() - interval '1 second' WHERE id = %s",
        job["id"],
    )
    wait_until(
        lambda: (
            not stack.query(
                "SELECT 1 FROM tracking_jobs WHERE id = %s AND status IN ('QUEUED','PROCESSING','RETRYING')", job["id"]
            )
        ),
        15,
        "the lookup to end",
    )


def test_a_courier_inserted_straight_into_the_database_gets_a_build_task(stack: Stack) -> None:
    code = f"direct{secrets.token_hex(3)}"
    stack.execute(
        "INSERT INTO providers (id, code, name, tracking_method, timeout_ms, queue_wait_max_ms, result_grace_ms,"
        " max_attempts, retry_base_ms, retry_max_ms, status, tracking_url)"
        " VALUES (gen_random_uuid(), %s, 'Direct Courier', 'api', 20000, 900000, 30000, 3, 5000, 300000,"
        " 'building', 'https://api.direct.example/track')",
        code,
    )
    task = wait_until(lambda: task_of(stack, code), 10, "the Engine to open a task by itself")
    assert task["request"]["hosts"] == ["api.direct.example"]
