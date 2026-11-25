"""The Engine client and the OpenRouter client, against recorded exchanges."""

from __future__ import annotations

import json

import httpx
import pytest

from ai_repair.engine import EngineClient, EngineUnavailable
from ai_repair.llm import ModelError, OpenRouterModel, parse_proposal

TOKEN = "repair-token-0123456789-0123456789-abcdef"
API_KEY = "sk-or-v1-not-a-real-key"


def test_a_task_is_claimed_with_the_repair_token_and_its_outcome_reported() -> None:
    seen: list[httpx.Request] = []
    task = {
        "id": "t1", "kind": "repair", "adapter_key": "balaji", "adapter_version": "0.1.0", "attempts": 1,
        "request": {},
        "samples": [{"id": "s1", "adapter_version": "0.1.0", "error_class": "SCHEMA_DRIFT",
                     "error_message": "no table", "tracking_number": "LR1", "content_type": "text/html",
                     "body": "<html/>"}],
    }  # fmt: skip

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=task) if request.url.path.endswith("/claim") else httpx.Response(204)

    engine = EngineClient("http://engine.internal:8081/", TOKEN, transport=httpx.MockTransport(handler))
    claimed = engine.claim()
    assert claimed is not None
    assert (claimed.adapter_key, claimed.samples[0].body) == ("balaji", "<html/>")
    engine.published("t1", "abc", "fixed")
    engine.failed("t1", "x" * 5000)
    assert [r.url.path for r in seen] == [
        "/internal/v1/repairs/claim", "/internal/v1/repairs/t1/result", "/internal/v1/repairs/t1/result",
    ]  # fmt: skip
    assert all(r.headers["authorization"] == f"Bearer {TOKEN}" for r in seen)
    assert json.loads(seen[1].content) == {"status": "published", "commit_sha": "abc", "summary": "fixed"}
    assert len(json.loads(seen[2].content)["reason"]) == 2000


def test_no_task_a_refusal_and_an_outage() -> None:
    def client(status: int) -> EngineClient:
        return EngineClient(
            "http://e", TOKEN, transport=httpx.MockTransport(lambda r: httpx.Response(status))
        )

    assert client(204).claim() is None
    for status in (401, 500):
        with pytest.raises(EngineUnavailable) as raised:
            client(status).claim()
        assert TOKEN not in str(raised.value)

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(EngineUnavailable, match="ConnectError"):
        EngineClient("http://e", TOKEN, transport=httpx.MockTransport(refuse)).claim()


def test_the_model_is_asked_through_openrouter_and_nothing_about_the_installation_is_sent() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    model = OpenRouterModel(API_KEY, "vendor/model-x", transport=httpx.MockTransport(handler))
    assert model.complete("rules", [{"role": "user", "content": "task"}]) == "{}"
    (request,) = seen
    assert str(request.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {API_KEY}"
    assert "http-referer" not in request.headers
    assert "x-title" not in request.headers
    body = json.loads(request.content)
    assert body["model"] == "vendor/model-x"
    assert body["messages"] == [{"role": "system", "content": "rules"}, {"role": "user", "content": "task"}]


@pytest.mark.parametrize(
    ("status", "body", "text"),
    [
        (401, {}, "refused the API key"),
        (402, {}, "no credit"),
        (404, {}, "does not know the model"),
        (429, {}, "rate limit"),
        (500, {}, "HTTP 500"),
        (200, {"choices": []}, "no message"),
        (200, {"choices": [{"message": {"content": " "}}]}, "nothing"),
    ],
)
def test_openrouter_problems_are_named_without_the_key(
    status: int, body: dict[str, object], text: str
) -> None:
    model = OpenRouterModel(
        API_KEY, "m", transport=httpx.MockTransport(lambda r: httpx.Response(status, json=body))
    )
    with pytest.raises(ModelError, match=text) as raised:
        model.complete("s", [])
    assert API_KEY not in str(raised.value)


def test_the_models_answer_is_read_strictly() -> None:
    answer = '```json\n{"summary": "s", "files": [{"path": " a.py ", "content": "x = 1"}]}\n```'
    proposal = parse_proposal(answer)
    assert (proposal.summary, proposal.files[0].path, proposal.files[0].content) == ("s", "a.py", "x = 1\n")
    for bad in ["sure, here it is", "[]", '{"files": "x"}', '{"files": [{"path": "a"}]}', '{"files": [3]}']:
        with pytest.raises(ModelError):
            parse_proposal(bad)
