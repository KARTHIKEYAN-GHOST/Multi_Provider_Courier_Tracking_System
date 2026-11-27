"""The repair service's only link to the Engine: claim a task, report its outcome."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx


class EngineUnavailable(Exception):
    """The Engine could not be reached or refused the token."""


@dataclass(frozen=True)
class Sample:
    """A provider answer the adapter could not read. Untrusted content."""

    id: str
    adapter_version: str
    error_class: str
    error_message: str
    tracking_number: str
    content_type: str
    body: Any


@dataclass(frozen=True)
class Task:
    id: str
    kind: str  # "repair" or "new_provider"
    adapter_key: str
    adapter_version: str | None
    attempts: int
    request: dict[str, Any]
    samples: list[Sample] = field(default_factory=list)


class EngineClient:
    def __init__(self, base_url: str, token: str, transport: httpx.BaseTransport | None = None) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=20.0,
            transport=transport,
        )

    def _post(self, path: str, body: dict[str, Any] | None = None) -> httpx.Response:
        try:
            response = self._client.post(path, json=body)
        except httpx.HTTPError as error:
            # Only the kind of failure: the exception text can contain the address.
            raise EngineUnavailable(f"the Engine could not be reached ({type(error).__name__})") from None
        if response.status_code in (401, 403):
            raise EngineUnavailable("the Engine refused the repair token")
        if response.status_code >= 500:
            raise EngineUnavailable(f"the Engine answered HTTP {response.status_code}")
        return response

    def claim(self) -> Task | None:
        """The next task, or ``None`` when there is nothing to do."""
        response = self._post("/internal/v1/repairs/claim")
        if response.status_code == 204:
            return None
        if response.status_code != 200:
            raise EngineUnavailable(f"unexpected answer to a claim (HTTP {response.status_code})")
        data = response.json()
        samples = [
            Sample(
                id=str(s["id"]),
                adapter_version=str(s["adapter_version"]),
                error_class=str(s["error_class"]),
                error_message=str(s["error_message"]),
                tracking_number=str(s["tracking_number"]),
                content_type=str(s["content_type"]),
                body=s["body"],
            )
            for s in data.get("samples", [])
        ]
        return Task(
            id=str(data["id"]),
            kind=str(data["kind"]),
            adapter_key=str(data["adapter_key"]),
            adapter_version=data.get("adapter_version"),
            attempts=int(data.get("attempts", 1)),
            request=dict(data.get("request") or {}),
            samples=samples,
        )

    def login(self, task_id: str) -> dict[str, str] | None:
        """The courier's login for a task being worked on, or ``None``. Kept only in memory."""
        response = self._post(f"/internal/v1/repairs/{task_id}/login")
        if response.status_code in (204, 404):
            return None
        if response.status_code != 200:
            raise EngineUnavailable(f"unexpected answer to a login request (HTTP {response.status_code})")
        login = response.json().get("login")
        if not isinstance(login, dict):
            return None
        return {str(k): str(v) for k, v in login.items() if isinstance(v, (str, int))}

    def published(self, task_id: str, commit_sha: str, summary: str) -> None:
        self._finish(task_id, {"status": "published", "commit_sha": commit_sha, "summary": summary})

    def failed(self, task_id: str, reason: str) -> None:
        self._finish(task_id, {"status": "failed", "reason": reason[:2000]})

    def _finish(self, task_id: str, body: dict[str, Any]) -> None:
        response = self._post(f"/internal/v1/repairs/{task_id}/result", body)
        if response.status_code not in (204, 404):
            raise EngineUnavailable(f"unexpected answer to a result (HTTP {response.status_code})")

    def close(self) -> None:
        self._client.close()
