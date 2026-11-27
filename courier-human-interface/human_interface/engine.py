"""The Engine's public API, as far as this site uses it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


class NotSignedIn(Exception):
    """The Engine did not accept the API key."""


class NotAnAdmin(NotSignedIn):
    """The key belongs to an organization user: only admins answer verification requests."""


class EngineUnavailable(Exception):
    """The Engine could not be reached or failed."""


@dataclass(frozen=True)
class Submitted:
    #: accepted | invalid | already_answered | expired | not_found
    outcome: str


class EngineClient:
    def __init__(self, base_url: str, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=10.0, transport=transport)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, path: str, api_key: str, json: Any = None) -> httpx.Response:
        try:
            response = await self._client.request(
                method, path, json=json, headers={"Authorization": f"Bearer {api_key}"}
            )
        except httpx.HTTPError as error:
            raise EngineUnavailable(type(error).__name__) from None
        if response.status_code == 401:
            raise NotSignedIn
        if response.status_code == 403:
            raise NotAnAdmin
        if response.status_code >= 500:
            raise EngineUnavailable(f"HTTP {response.status_code}")
        return response

    async def pending(self, api_key: str) -> list[dict[str, Any]]:
        response = await self._call("GET", "/v1/interventions", api_key)
        if response.status_code != 200:
            raise EngineUnavailable(f"HTTP {response.status_code}")
        items = response.json().get("items", [])
        return [item for item in items if isinstance(item, dict)]

    async def one(self, api_key: str, intervention_id: str) -> dict[str, Any] | None:
        response = await self._call("GET", f"/v1/interventions/{intervention_id}", api_key)
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise EngineUnavailable(f"HTTP {response.status_code}")
        found: dict[str, Any] = response.json()
        return found

    async def answer(self, api_key: str, intervention_id: str, answer: str) -> Submitted:
        response = await self._call(
            "POST", f"/v1/interventions/{intervention_id}/response", api_key, {"response": answer}
        )
        outcomes = {
            202: "accepted",
            404: "not_found",
            409: "already_answered",
            410: "expired",
            422: "invalid",
        }
        if response.status_code not in outcomes:
            raise EngineUnavailable(f"HTTP {response.status_code}")
        return Submitted(outcomes[response.status_code])
