"""The AI model, reached through OpenRouter's chat-completions API.

What is sent: the service's own instructions, the adapter's source code and scrubbed
provider answers. Never a secret, never anything about the people using the system.
What comes back is text and is treated as untrusted (see ``guard``).
"""

from __future__ import annotations

import json
import re
import time
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
        retries: int = 3,
        retry_wait_s: float = 30.0,
    ) -> None:
        self.name = model
        self._max_tokens = max_tokens
        self._retries, self._retry_wait_s = retries, retry_wait_s
        # No referer or title header: nothing about this installation is sent along.
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout_s,
            transport=transport,
        )

    def _post(self, body: dict[str, Any]) -> httpx.Response:
        try:
            return self._client.post("/chat/completions", json=body)
        except httpx.HTTPError as error:
            raise ModelError(f"OpenRouter could not be reached ({type(error).__name__})") from None

    def complete(self, system: str, messages: list[dict[str, str]]) -> str:
        body: dict[str, Any] = {
            "model": self.name,
            "messages": [{"role": "system", "content": system}, *messages],
            "temperature": 0,
            "max_tokens": self._max_tokens,
            "response_format": {"type": "json_object"},
        }
        response = self._post(body)
        if response.status_code in (400, 404) and "response_format" in body:
            # Some models cannot be told to answer in JSON. The instructions ask for it anyway.
            del body["response_format"]
            response = self._post(body)
        for _ in range(self._retries):
            if response.status_code != 429:
                break
            time.sleep(self._retry_wait_s)
            response = self._post(body)
        if response.status_code in (401, 403):
            raise ModelError("OpenRouter refused the API key")
        if response.status_code == 402:
            raise ModelError("the OpenRouter account has no credit left")
        if response.status_code == 404:
            raise ModelError(
                f"OpenRouter has no usable provider for the model '{self.name}' (wrong name, or the "
                "account's privacy settings exclude it)"
            )
        if response.status_code == 429:
            raise ModelError("OpenRouter's rate limit was reached")
        if response.status_code != 200:
            raise ModelError(f"OpenRouter answered HTTP {response.status_code}")
        try:
            data: Any = response.json()
            content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError):
            # A failure of the model behind OpenRouter arrives as HTTP 200 with an error object.
            raise ModelError("OpenRouter's answer has no message (the model behind it failed)") from None
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
    # Weaker models wrap the object in a sentence or two; the object itself is what counts.
    start, end = text.find("{"), text.rfind("}")
    try:
        data = json.loads(text[start : end + 1]) if 0 <= start < end else None
    except ValueError:
        data = None
    if data is None:
        raise ModelError("the answer is not a JSON object") from None
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
