"""The AI model. Three suppliers, chosen with AI_REPAIR_MODEL_SUPPLIER:

- ``openrouter``: OpenRouter's chat-completions API (many models, some free).
- ``claude-code``: Claude through the Claude Code program, signed in with a setup token
  (``claude setup-token``, a Claude subscription). Meant for development on a laptop.
- ``anthropic``: Claude through Anthropic's API with an API key. Meant for the server.

What is sent: the service's own instructions, the adapter's source code and scrubbed
provider answers. Never a secret, never anything about the people using the system.
What comes back is text and is treated as untrusted (see ``guard``).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from typing import TYPE_CHECKING, Any, Protocol

import httpx

if TYPE_CHECKING:
    import anthropic

    from .config import Settings


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


# ----------------------------------------------------------------------------- Claude


def _transcript(messages: list[dict[str, str]]) -> str:
    """The conversation as one text, for a program that takes a single prompt."""
    if len(messages) == 1:
        return messages[0]["content"]
    parts = [
        "The conversation so far follows. Answer as the assistant to the last message, "
        "in the form your instructions require.\n"
    ]
    for message in messages:
        parts.append(f"<{message['role']}>\n{message['content']}\n</{message['role']}>")
    return "\n\n".join(parts)


class ClaudeCodeModel:
    """Claude, asked through the Claude Code program (``claude -p``) with a setup token.

    Claude Code is used as a plain model here: every tool is switched off, no MCP server
    is loaded, no setting, memory or project file is read (its configuration directory and
    working directory are fresh empty ones), and the program sees only the token and a
    minimal PATH; none of the service's other secrets reaches it.
    """

    def __init__(
        self,
        oauth_token: str,
        model: str,
        *,
        effort: str = "high",
        command: str = "claude",
        timeout_s: float = 240.0,
        retries: int = 3,
        retry_wait_s: float = 30.0,
    ) -> None:
        self.name = model
        self.last_model = model
        self._token, self._effort, self._command = oauth_token, effort, command
        self._timeout_s, self._retries, self._retry_wait_s = timeout_s, retries, retry_wait_s

    def _run(self, system: str, prompt: str) -> subprocess.CompletedProcess[str]:
        program = shutil.which(self._command)
        if program is None:
            raise ModelError(
                f"the Claude Code program '{self._command}' is not installed "
                "(https://code.claude.com/docs/en/setup)"
            )
        with tempfile.TemporaryDirectory(prefix="claude-model-") as home:
            environment = {
                "PATH": "/usr/local/bin:/usr/bin:/bin:" + os.path.dirname(program),
                "HOME": home,
                "CLAUDE_CONFIG_DIR": home,
                "CLAUDE_CODE_OAUTH_TOKEN": self._token,
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                "DISABLE_AUTOUPDATER": "1",
            }
            arguments = [
                program,
                "--print",
                "--output-format",
                "json",
                "--model",
                self.name,
                "--effort",
                self._effort,
                "--system-prompt",
                system,
                "--tools",
                "",
                "--strict-mcp-config",
                "--no-session-persistence",
            ]
            try:
                return subprocess.run(
                    arguments,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    env=environment,
                    cwd=home,
                    timeout=self._timeout_s,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                raise ModelError(
                    f"Claude did not answer within {self._timeout_s:.0f} s", retryable=True
                ) from None

    def _ask(self, system: str, prompt: str) -> str:
        done = self._run(system, prompt)
        try:
            data: Any = json.loads(done.stdout)
        except ValueError:
            data = None
        if not isinstance(data, dict):
            # The program's own error text can name the account; only its first line is kept.
            first = (done.stderr.strip().splitlines() or ["no output"])[0][:200]
            raise ModelError(f"Claude Code failed (exit {done.returncode}): {first}", retryable=True)
        text = str(data.get("result") or "")
        if data.get("is_error") or data.get("subtype") not in (None, "success"):
            lowered = text.lower()
            if any(word in lowered for word in ("rate limit", "usage limit", "overloaded", "529", "429")):
                raise ModelError(f"Claude's limit was reached: {text[:200]}", retryable=True)
            if any(word in lowered for word in ("auth", "token", "log in", "login", "401", "403")):
                raise ModelError(
                    "Claude Code refused the setup token (make a new one with: claude setup-token)"
                )
            raise ModelError(f"Claude Code reported an error: {text[:200]}", retryable=True)
        if not text.strip():
            raise ModelError("Claude answered with nothing", retryable=True)
        return text

    def complete(self, system: str, messages: list[dict[str, str]]) -> str:
        prompt = _transcript(messages)
        for attempt in range(self._retries + 1):
            try:
                return self._ask(system, prompt)
            except ModelError as error:
                if not error.retryable or attempt == self._retries:
                    raise
                time.sleep(self._retry_wait_s)
        raise AssertionError("unreachable")


class AnthropicModel:
    """Claude through Anthropic's Messages API with an API key (the official SDK).

    Thinking is adaptive (always on for Claude Opus 5.5); ``effort`` sets how deep. The
    answer is streamed, so a long one never runs into a request timeout.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        effort: str = "high",
        max_tokens: int = 32000,
        timeout_s: float = 240.0,
        retries: int = 3,
        client: anthropic.Anthropic | None = None,
    ) -> None:
        import anthropic

        self.name = model
        self.last_model = model
        self._effort, self._max_tokens = effort, max_tokens
        # The SDK waits and repeats by itself on 429, 5xx and broken connections.
        self._client = client or anthropic.Anthropic(api_key=api_key, timeout=timeout_s, max_retries=retries)

    def complete(self, system: str, messages: list[dict[str, str]]) -> str:
        import anthropic

        try:
            with self._client.messages.stream(
                model=self.name,
                max_tokens=self._max_tokens,
                system=system,
                thinking={"type": "adaptive"},
                output_config={"effort": self._effort},  # type: ignore[arg-type]
                messages=_alternating(messages),  # type: ignore[arg-type]
            ) as stream:
                message = stream.get_final_message()
        except anthropic.AuthenticationError:
            raise ModelError("Anthropic refused the API key") from None
        except anthropic.PermissionDeniedError:
            raise ModelError("the Anthropic API key may not use this model") from None
        except anthropic.NotFoundError:
            raise ModelError(f"Anthropic has no model named '{self.name}'") from None
        except anthropic.BadRequestError as error:
            raise ModelError(f"Anthropic refused the request: {str(error.message)[:200]}") from None
        except anthropic.RateLimitError:
            raise ModelError("Anthropic's rate limit was reached", retryable=True) from None
        except (anthropic.APIConnectionError, anthropic.APIStatusError) as error:
            raise ModelError(f"Anthropic could not answer ({type(error).__name__})", retryable=True) from None
        self.last_model = str(getattr(message, "model", "") or self.name)
        if message.stop_reason == "refusal":
            raise ModelError("Claude declined this request")
        if message.stop_reason == "max_tokens":
            raise ModelError(
                "Claude ran out of its output allowance before finishing (AI_REPAIR_MAX_OUTPUT_TOKENS)",
                retryable=True,
            )
        text = "".join(block.text for block in message.content if block.type == "text")
        if not text.strip():
            raise ModelError("Claude answered with nothing", retryable=True)
        return text

    def close(self) -> None:
        self._client.close()


def _alternating(messages: list[dict[str, str]]) -> list[dict[str, str]]:
    """User and assistant turns strictly alternating, starting with the user, as the API wants."""
    merged: list[dict[str, str]] = []
    for message in messages:
        if merged and merged[-1]["role"] == message["role"]:
            merged[-1] = {**merged[-1], "content": merged[-1]["content"] + "\n\n" + message["content"]}
        else:
            merged.append(dict(message))
    if merged and merged[0]["role"] != "user":
        merged.insert(0, {"role": "user", "content": "(continue)"})
    return merged


def build_model(
    settings: Settings,
    *,
    timeout_s: float | None = None,
    max_tokens: int | None = None,
    retries: int | None = None,
    retry_wait_s: float | None = None,
) -> Model:
    """The model of the configured supplier."""
    timeout = settings.ai_repair_llm_timeout_s if timeout_s is None else timeout_s
    tokens = settings.ai_repair_max_output_tokens if max_tokens is None else max_tokens
    tries = settings.ai_repair_llm_retries if retries is None else retries
    wait = settings.ai_repair_llm_retry_wait_s if retry_wait_s is None else retry_wait_s
    supplier = settings.ai_repair_model_supplier
    if supplier == "claude-code":
        return ClaudeCodeModel(
            settings.claude_code_oauth_token.get_secret_value(),
            settings.ai_repair_claude_model,
            effort=settings.ai_repair_claude_effort,
            command=settings.ai_repair_claude_command,
            timeout_s=timeout,
            retries=tries,
            retry_wait_s=wait,
        )
    if supplier == "anthropic":
        return AnthropicModel(
            settings.anthropic_api_key.get_secret_value(),
            settings.ai_repair_claude_model,
            effort=settings.ai_repair_claude_effort,
            max_tokens=tokens,
            timeout_s=timeout,
            retries=tries,
        )
    return OpenRouterModel(
        settings.openrouter_api_key.get_secret_value(),
        settings.openrouter_model,
        settings.openrouter_base_url,
        timeout,
        tokens,
        retries=tries,
        retry_wait_s=wait,
    )
