"""The AI model, reached through OpenRouter's chat-completions API.

What is sent: the service's own instructions, the adapter's source code and scrubbed
provider answers. Never a secret, never anything about the people using the system.
What comes back is text and is treated as untrusted (see ``guard``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from .guard import ProposedFile


class ModelError(Exception):
    """The model could not be asked, or its answer is not in the agreed form."""


@dataclass(frozen=True)
class Proposal:
    summary: str
    files: list[ProposedFile]


class Model(Protocol):
    name: str

    def complete(self, system: str, messages: list[dict[str, str]]) -> str: ...


class OpenRouterModel:
    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = "https://openrouter.ai/api/v1",
        timeout_s: float = 240.0,
        max_tokens: int = 16000,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.name = model
        self._max_tokens = max_tokens
        # No referer or title header: nothing about this installation is sent along.
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_s,
            transport=transport,
        )

    def complete(self, system: str, messages: list[dict[str, str]]) -> str:
        body = {
            "model": self.name,
            "messages": [{"role": "system", "content": system}, *messages],
            "temperature": 0,
            "max_tokens": self._max_tokens,
            "response_format": {"type": "json_object"},
        }
        try:
            response = self._client.post("/chat/completions", json=body)
        except httpx.HTTPError as error:
            raise ModelError(f"OpenRouter could not be reached ({type(error).__name__})") from None
        if response.status_code in (401, 403):
            raise ModelError("OpenRouter refused the API key")
        if response.status_code == 402:
            raise ModelError("the OpenRouter account has no credit left")
        if response.status_code == 404:
            raise ModelError(f"OpenRouter does not know the model '{self.name}'")
        if response.status_code == 429:
            raise ModelError("OpenRouter's rate limit was reached")
        if response.status_code != 200:
            raise ModelError(f"OpenRouter answered HTTP {response.status_code}")
        try:
            data: Any = response.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise ModelError("OpenRouter's answer has no message") from None
        if not isinstance(content, str) or not content.strip():
            raise ModelError("the model answered with nothing")
        return content

    def close(self) -> None:
        self._client.close()


_FENCE = re.compile(r"^```[a-zA-Z]*\s*\n(.*)\n```\s*$", re.DOTALL)


def parse_proposal(text: str) -> Proposal:
    """Reads the model's answer: ``{"summary": "...", "files": [{"path": "...", "content": "..."}]}``."""
    text = text.strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1)
    try:
        data = json.loads(text)
    except ValueError:
        raise ModelError("the answer is not JSON") from None
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, list):
        raise ModelError('the answer has no "files" list')
    proposed = []
    for item in files:
        path, content = (item.get("path"), item.get("content")) if isinstance(item, dict) else (None, None)
        if not isinstance(path, str) or not isinstance(content, str):
            raise ModelError('every file needs a "path" and a "content" text')
        proposed.append(ProposedFile(path.strip(), content if content.endswith("\n") else content + "\n"))
    summary = data.get("summary")
    return Proposal(summary if isinstance(summary, str) else "", proposed)
