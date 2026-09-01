"""OpenAI transport tests (``--model openai[:model]``).

The transport is a plain ``prompt -> str`` callable with an injectable HTTP
layer, so the whole chain — request shape, error mapping, and a full OODA run
driven by a faked OpenAI endpoint — is exercised offline, deterministically.
"""

from __future__ import annotations

import io
import json
import urllib.error
from collections.abc import Callable
from typing import Any, Self

import pytest

from computeruse.cli import load_model
from computeruse.orchestrator.loop import OodaRunner, WorkingState
from computeruse.orchestrator.prompts import scaffolded_provider
from computeruse.orchestrator.schemas import AgentTurn
from computeruse.providers.openai import DEFAULT_MODEL, OpenAIError, openai_model

CLICK_DECISION = json.dumps(
    {
        "thought": "click",
        "sub_goal": "click the thing",
        "action": {"type": "mouse_click", "x": 1, "y": 1},
    }
)
FINISH_DECISION = json.dumps(
    {
        "thought": "done",
        "sub_goal": "workflow complete",
        "action": {"type": "finish", "status": "success", "summary": "ok"},
    }
)


class _FakeResponse:
    """A minimal file-like context manager standing in for an HTTP response."""

    def __init__(self, body: str) -> None:
        self._body = body.encode("utf-8")

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _fake_opener(*items: object) -> tuple[Callable[..., Any], list[dict[str, object]]]:
    """Return an opener serving `items` in order (FakeResponse or Exception)."""
    queue = list(items)
    captured: list[dict[str, object]] = []

    def opener(request: object, *, timeout: float) -> Any:
        body = json.loads(request.data.decode("utf-8"))
        captured.append(
            {
                "body": body,
                # HTTPMessage normalizes header names (Content-type); fold to
                # lowercase so assertions are stable.
                "headers": {
                    name.lower(): value for name, value in request.header_items()
                },
            }
        )
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return opener, captured


def test_openai_model_requires_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(OpenAIError, match="OPENAI_API_KEY"):
        openai_model("gpt-5.6-terra")


def test_openai_model_success_and_request_shape() -> None:
    opener, captured = _fake_opener(
        _FakeResponse(json.dumps({"choices": [{"message": {"content": "the reply"}}]}))
    )
    model = openai_model("gpt-5.6-terra", api_key="sk-test", http_open=opener)
    assert model("the prompt") == "the reply"

    (request,) = captured
    body = request["body"]
    assert body["model"] == "gpt-5.6-terra"
    assert body["response_format"] == {"type": "json_object"}
    assert body["messages"] == [{"role": "user", "content": "the prompt"}]
    headers = request["headers"]
    assert headers["authorization"] == "Bearer sk-test"
    assert headers["content-type"] == "application/json"


def test_openai_model_defaults_to_terra() -> None:
    opener, captured = _fake_opener(
        _FakeResponse(json.dumps({"choices": [{"message": {"content": "x"}}]}))
    )
    model = openai_model(api_key="sk-test", http_open=opener)
    model("p")
    assert captured[0]["body"]["model"] == DEFAULT_MODEL == "gpt-5.6-terra"


def test_openai_model_http_error_carries_api_message() -> None:
    body = io.BytesIO(b'{"error": {"message": "model not found"}}')
    error = urllib.error.HTTPError(
        "https://api.openai.com/v1/chat/completions", 404, "Not Found", {}, body
    )
    model = openai_model("gpt-5.6-terra", api_key="sk-test", http_open=_fake_opener(error)[0])
    with pytest.raises(OpenAIError) as excinfo:
        model("p")
    message = str(excinfo.value)
    assert "404" in message and "model not found" in message


def test_openai_model_missing_choices_is_explicit() -> None:
    opener, _ = _fake_opener(_FakeResponse(json.dumps({"error": {"code": "x"}})))
    model = openai_model("gpt-5.6-terra", api_key="sk-test", http_open=opener)
    with pytest.raises(OpenAIError, match="missing choices"):
        model("p")


def test_openai_model_missing_content_is_explicit() -> None:
    opener, _ = _fake_opener(_FakeResponse(json.dumps({"choices": [{"message": {}}]})))
    model = openai_model("gpt-5.6-terra", api_key="sk-test", http_open=opener)
    with pytest.raises(OpenAIError, match="missing text content"):
        model("p")


def test_openai_transport_drives_the_ooda_loop() -> None:
    """Full chain: OpenAI transport -> weak-model scaffold -> OODA run."""
    opener, captured = _fake_opener(
        _FakeResponse(json.dumps({"choices": [{"message": {"content": CLICK_DECISION}}]})),
        _FakeResponse(json.dumps({"choices": [{"message": {"content": FINISH_DECISION}}]})),
    )
    model = openai_model("gpt-5.6-terra", api_key="sk-test", http_open=opener)
    runner = OodaRunner(
        provider=scaffolded_provider(model, app="Safari"),
        execute_physical=lambda _action: None,
    )
    final = runner.run("g")
    assert final.completed_steps == ("step_0:mouse_click", "step_1:finish")
    # The distilled trajectory excludes the orchestrator-internal finish.
    assert [a.type for a in runner.executed_trajectory] == ["mouse_click"]
    assert final.last_error is None
    # The first prompt carried the working context (goal + action contract).
    first_prompt = captured[0]["body"]["messages"][0]["content"]
    assert "Goal: g" in first_prompt
    assert "You are driving a physical macOS computer" in first_prompt


def test_cli_load_model_resolves_openai_specs(monkeypatch: pytest.MonkeyPatch) -> None:
    import computeruse.cli as cli_module

    seen: list[str | None] = []

    def fake_openai_model(model: str | None) -> Callable[[str], str]:
        seen.append(model)
        return lambda _prompt: CLICK_DECISION

    monkeypatch.setattr(cli_module, "openai_model", fake_openai_model)
    # Bare `openai` resolves to the transport default (None -> DEFAULT_MODEL).
    provider = load_model("openai", app="Safari")
    turn = provider(WorkingState(goal="g"))
    assert isinstance(turn, AgentTurn)
    assert turn.action.type == "mouse_click"
    assert seen == [None]
    # An explicit tier is passed through.
    load_model("openai:gpt-5.6-luna", app="Safari")
    assert seen == [None, "gpt-5.6-luna"]


def test_openai_model_multimodal_request_shape() -> None:
    opener, captured = _fake_opener(
        _FakeResponse(json.dumps({"choices": [{"message": {"content": CLICK_DECISION}}]}))
    )
    model = openai_model("gpt-5.6-terra", api_key="sk-test", http_open=opener)
    reply = model("look at this", "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAAAAAA6fptVAAAACklEQVR4nGNiAAAABgADNjd8qAAAAABJRU5ErkJggg==")
    assert reply == CLICK_DECISION

    (request,) = captured
    body = request["body"]
    msg = body["messages"][0]
    assert msg["role"] == "user"
    assert isinstance(msg["content"], list)
    assert msg["content"][0] == {"type": "text", "text": "look at this"}
    assert msg["content"][1]["type"] == "image_url"
    assert "data:image/png;base64," in msg["content"][1]["image_url"]["url"]
