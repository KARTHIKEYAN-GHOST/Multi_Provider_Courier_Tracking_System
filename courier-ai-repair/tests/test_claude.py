"""The two Claude suppliers, without asking Claude.

``claude-code`` runs a stand-in for the Claude Code program that records how it was started.
``anthropic`` gets a stand-in for the SDK's client. Neither test spends a subscription or
an API key.
"""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ai_repair.config import Settings
from ai_repair.llm import AnthropicModel, ClaudeCodeModel, ModelError, OpenRouterModel, build_model

TOKEN = "sk-ant-oat01-not-a-real-token"


def fake_claude(tmp_path: Path, reply: dict[str, Any] | str, exit_code: int = 0) -> tuple[str, Path]:
    """A program named like Claude Code that saves its arguments, environment, input and
    working directory, and prints ``reply``."""
    record = tmp_path / "record.json"
    out = reply if isinstance(reply, str) else json.dumps(reply)
    program = tmp_path / "bin" / "claude"
    program.parent.mkdir()
    program.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"json.dump({{'argv': sys.argv[1:], 'env': dict(os.environ), 'stdin': sys.stdin.read(),"
        f" 'cwd': os.getcwd(), 'files': os.listdir('.')}}, open({str(record)!r}, 'w'))\n"
        f"sys.stdout.write({out!r})\n"
        f"sys.exit({exit_code})\n"
    )
    program.chmod(program.stat().st_mode | stat.S_IXUSR)
    return str(program), record


def ok(text: str) -> dict[str, Any]:
    return {"type": "result", "subtype": "success", "is_error": False, "result": text}


def test_claude_code_is_used_as_a_bare_model_with_only_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AI_REPAIR_GITHUB_TOKEN", "ghp_must_not_be_passed_on")
    monkeypatch.setenv("ENGINE_REPAIR_TOKEN", "engine-token-must-not-be-passed-on")
    program, record = fake_claude(tmp_path, ok('{"action": "finish"}'))
    model = ClaudeCodeModel(TOKEN, "claude-opus-5-5", effort="high", command=program, retries=0)

    answer = model.complete(
        "SYSTEM RULES",
        [
            {"role": "user", "content": "the task"},
            {"role": "assistant", "content": '{"action": "read"}'},
            {"role": "user", "content": "the file"},
        ],
    )

    assert answer == '{"action": "finish"}'
    seen = json.loads(record.read_text())
    argv = seen["argv"]
    assert argv[argv.index("--tools") + 1] == "", "every tool is switched off"
    assert "--strict-mcp-config" in argv
    assert "--no-session-persistence" in argv
    assert argv[argv.index("--system-prompt") + 1] == "SYSTEM RULES"
    assert argv[argv.index("--model") + 1] == "claude-opus-5-5"
    assert argv[argv.index("--output-format") + 1] == "json"
    # The whole conversation goes in on stdin, in order.
    assert (
        seen["stdin"].index("the task")
        < seen["stdin"].index('{"action": "read"}')
        < seen["stdin"].index("the file")
    )
    # Only the token reaches the program; a fresh, empty configuration and working directory.
    env = seen["env"]
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    assert "AI_REPAIR_GITHUB_TOKEN" not in env
    assert "ENGINE_REPAIR_TOKEN" not in env
    assert env["CLAUDE_CONFIG_DIR"] == seen["cwd"] == env["HOME"]
    assert seen["files"] == []
    assert not os.path.exists(seen["cwd"]), "the directory is removed afterwards"


def test_a_single_message_is_sent_as_it_is(tmp_path: Path) -> None:
    program, record = fake_claude(tmp_path, ok("ready"))
    ClaudeCodeModel(TOKEN, "m", command=program, retries=0).complete("s", [{"role": "user", "content": "Hi"}])
    assert json.loads(record.read_text())["stdin"] == "Hi"


@pytest.mark.parametrize(
    ("reply", "retryable", "words"),
    [
        (
            {"is_error": True, "subtype": "success", "result": "Claude AI usage limit reached|1764000000"},
            True,
            "limit",
        ),
        ({"is_error": True, "result": "Invalid API key · Please run /login"}, False, "setup-token"),
        ({"is_error": True, "subtype": "error_during_execution", "result": "boom"}, True, "error"),
        (ok(""), True, "nothing"),
        ("not json at all", True, "failed"),
    ],
)
def test_claude_code_failures_say_whether_asking_again_may_help(
    tmp_path: Path, reply: dict[str, Any] | str, retryable: bool, words: str
) -> None:
    program, _ = fake_claude(tmp_path, reply, exit_code=0 if isinstance(reply, dict) else 1)
    with pytest.raises(ModelError) as raised:
        ClaudeCodeModel(TOKEN, "m", command=program, retries=0).complete(
            "s", [{"role": "user", "content": "x"}]
        )
    assert raised.value.retryable is retryable
    assert words in str(raised.value)
    assert TOKEN not in str(raised.value)


def test_a_missing_claude_program_is_named() -> None:
    model = ClaudeCodeModel(TOKEN, "m", command="/nonexistent/claude", retries=0)
    with pytest.raises(ModelError, match="not installed"):
        model.complete("s", [{"role": "user", "content": "x"}])


class FakeStream:
    def __init__(self, message: Any) -> None:
        self._message = message

    def __enter__(self) -> FakeStream:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def get_final_message(self) -> Any:
        return self._message


class FakeClient:
    def __init__(self, message: Any = None, error: Exception | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.messages = self
        self._message, self._error = message, error

    def stream(self, **request: Any) -> FakeStream:
        self.calls.append(request)
        if self._error is not None:
            raise self._error
        return FakeStream(self._message)

    def close(self) -> None:
        return None


def message(text: str, stop_reason: str = "end_turn") -> Any:
    blocks = [SimpleNamespace(type="thinking", thinking="..."), SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(content=blocks, stop_reason=stop_reason, model="claude-opus-5-5")


def test_the_api_gets_the_conversation_with_adaptive_thinking_and_the_effort() -> None:
    client = FakeClient(message('{"action": "finish"}'))
    model = AnthropicModel("key", "claude-opus-5-5", effort="xhigh", max_tokens=9000, client=client)  # type: ignore[arg-type]
    turns = [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "b"},
        {"role": "user", "content": "c"},
    ]
    assert model.complete("rules", turns) == '{"action": "finish"}'
    (request,) = client.calls
    assert (request["model"], request["system"], request["max_tokens"]) == ("claude-opus-5-5", "rules", 9000)
    assert request["thinking"] == {"type": "adaptive"}
    assert request["output_config"] == {"effort": "xhigh"}
    assert [m["role"] for m in request["messages"]] == ["user", "assistant", "user"], "turns alternate"
    assert request["messages"][-1]["content"] == "b\n\nc"


@pytest.mark.parametrize(
    ("stop_reason", "retryable"),
    [("refusal", False), ("max_tokens", True)],
)
def test_a_refusal_or_a_cut_off_answer_is_not_taken_as_an_answer(stop_reason: str, retryable: bool) -> None:
    model = AnthropicModel("key", "m", client=FakeClient(message("{", stop_reason)))  # type: ignore[arg-type]
    with pytest.raises(ModelError) as raised:
        model.complete("s", [{"role": "user", "content": "x"}])
    assert raised.value.retryable is retryable


def test_api_errors_say_whether_asking_again_may_help() -> None:
    import anthropic
    import httpx2

    request = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")

    def status(cls: type[anthropic.APIStatusError], code: int) -> anthropic.APIStatusError:
        return cls("x", response=httpx2.Response(code, request=request), body=None)

    cases: list[tuple[Exception, bool]] = [
        (status(anthropic.AuthenticationError, 401), False),
        (status(anthropic.RateLimitError, 429), True),
        (status(anthropic.InternalServerError, 500), True),
        (anthropic.APIConnectionError(request=request), True),
    ]
    for error, retryable in cases:
        model = AnthropicModel("key", "m", client=FakeClient(error=error))  # type: ignore[arg-type]
        with pytest.raises(ModelError) as raised:
            model.complete("s", [{"role": "user", "content": "x"}])
        assert raised.value.retryable is retryable, type(error).__name__


def test_one_setting_chooses_the_supplier_and_only_its_secret_is_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "OPENROUTER_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "ANTHROPIC_API_KEY",
        "AI_REPAIR_MODEL_SUPPLIER",
    ):
        monkeypatch.delenv(name, raising=False)
    base = {"engine_repair_token": "t", "ai_repair_github_repo": "o/r"}

    assert isinstance(build_model(Settings(_env_file=None, **base)), OpenRouterModel)  # type: ignore[arg-type]

    laptop = Settings(_env_file=None, ai_repair_model_supplier="claude-code", **base)  # type: ignore[arg-type]
    assert laptop.missing() == ["CLAUDE_CODE_OAUTH_TOKEN"]
    assert isinstance(build_model(laptop), ClaudeCodeModel)
    assert laptop.model_name() == "claude-opus-5-5"

    server = Settings(_env_file=None, ai_repair_model_supplier="anthropic", anthropic_api_key="k", **base)  # type: ignore[arg-type]
    assert server.missing() == []
    assert isinstance(build_model(server), AnthropicModel)
