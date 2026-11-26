"""The AI model, reached through OpenRouter's chat-completions API.

What is sent: the service's own instructions, the adapter's source code and scrubbed
provider answers. Never a secret, never anything about the people using the system.
What comes back is text and is treated as untrusted (see ``guard``).
"""

from __future__ import annotations

import time
from typing import Any, Protocol

import httpx


class ModelError(Exception):
    """The model could not be asked, or its answer is not in the agreed form.

    ``retryable``: asking again may help (a busy or weak model), unlike a refused key.
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


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
        #: First choice, then the ones to fall back on (OpenRouter accepts at most three).
        self._models = [name.strip() for name in model.split(",") if name.strip()][:3] or [model]
        self.name = self._models[0]
        #: The model that really answered last: a fallback, or a router's pick, can differ.
        self.last_model = self.name
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
        # A connection that fails or breaks is tried again: networks hiccup, answers take minutes.
        for attempt in range(self._retries + 1):
            try:
                return self._client.post("/chat/completions", json=body)
            except httpx.HTTPError as error:
                kind = type(error).__name__
                if attempt < self._retries:
                    time.sleep(min(self._retry_wait_s, 10.0))
        raise ModelError(f"OpenRouter could not be reached ({kind})", retryable=True)

    def complete(self, system: str, messages: list[dict[str, str]]) -> str:
        body: dict[str, Any] = {
            **({"models": self._models} if len(self._models) > 1 else {"model": self.name}),
            "messages": [{"role": "system", "content": system}, *messages],
            "temperature": 0,
            "max_tokens": self._max_tokens,
            "response_format": {"type": "json_object"},
            # Models that "think" first spend the same budget on thinking; keep that short,
            # or nothing is left for the answer.
            "reasoning": {"effort": "low"},
        }
        response = self._post(body)
        if response.status_code in (400, 404):
            # Some models cannot be told to answer in JSON or how much to think. The
            # instructions ask for JSON anyway.
            del body["response_format"], body["reasoning"]
            response = self._post(body)
        if response.status_code in (400, 404) and "models" in body:
            # One name in the list is refused outright when OpenRouter no longer has that
            # model (free models come and go). The last entry is the safest one left.
            del body["models"]
            body["model"] = self._models[-1]
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
            raise ModelError("OpenRouter's rate limit was reached", retryable=True)
        if response.status_code != 200:
            raise ModelError(f"OpenRouter answered HTTP {response.status_code}", retryable=True)
        try:
            data: Any = response.json()
            used = data.get("model")
            self.last_model = used if isinstance(used, str) and used else self.name
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            # A failure of the model behind OpenRouter arrives as HTTP 200 with an error object.
            raise ModelError(
                "OpenRouter's answer has no message (the model behind it failed)", retryable=True
            ) from None
        if choice.get("finish_reason") == "length":
            raise ModelError(
                f"the model {self.last_model} ran out of its output allowance before finishing "
                "(it spent it on thinking, or the answer is too long)",
                retryable=True,
            )
        if not isinstance(content, str) or not content.strip():
            raise ModelError(f"the model {self.last_model} answered with nothing", retryable=True)
        return content

    def close(self) -> None:
        self._client.close()
