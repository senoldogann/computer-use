"""OpenAI transport tests (``--model openai[:model]``).

The transport is a plain ``prompt -> str`` callable with an injectable HTTP
layer, so the whole chain — request shape, error mapping, and a full OODA run
driven by a faked OpenAI endpoint — is exercised offline, deterministically.
"""

from __future__ import annotations

import io
import json
import os
import ssl
import urllib.error
from collections.abc import Callable
from pathlib import Path
from typing import Any, Self

import pytest

from computeruse.cli import load_model
from computeruse.orchestrator.loop import OodaRunner, WorkingState
from computeruse.orchestrator.prompts import scaffolded_provider
from computeruse.orchestrator.schemas import AgentTurn
from computeruse.providers.openai import (
    _RETRY_AFTER_MAX_SECONDS,
    DEFAULT_MODEL,
    OpenAIError,
    _retry_after_seconds,
    openai_model,
)

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


def test_openai_model_retries_transient_tls_failures() -> None:
    """A flaky-wire SSL failure (e.g. SSLV3_ALERT_BAD_RECORD_MAC) must be
    retried with backoff, not kill the run: two TLS failures then success."""
    opener, captured = _fake_opener(
        ssl.SSLError("SSLV3_ALERT_BAD_RECORD_MAC ssl/tls alert bad record mac"),
        ssl.SSLError("SSLV3_ALERT_BAD_RECORD_MAC ssl/tls alert bad record mac"),
        _FakeResponse(
            json.dumps({"choices": [{"message": {"content": "the reply"}}]})
        ),
    )
    model = openai_model("gpt-5.6-terra", api_key="sk-test", http_open=opener)
    assert model("hi") == "the reply"
    assert len(captured) == 3, "the two failures must be retried, then succeed"


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


def test_a_cut_off_reply_says_so_instead_of_looking_malformed() -> None:
    """Truncation and malformation need different answers from the model.

    `type_text` and `clipboard_paste` carry their payload inside the decision
    object, so a long piece of writing can exhaust the completion budget and
    come back as a half-finished JSON string. Reported as a contract violation
    it reads as "you formatted this wrong", and the model re-sends the same
    too-long object: measured on "research the AI news and write me a summary
    in Notes", four consecutive invalid decisions, every one of them exactly at
    the limit, and the run gave up without typing a word.
    """
    opener, _ = _fake_opener(
        _FakeResponse(
            json.dumps(
                {
                    "choices": [
                        {
                            "message": {"content": '{"thought": "writing the su'},
                            "finish_reason": "length",
                        }
                    ]
                }
            )
        )
    )
    model = openai_model("gpt-5.6-terra", api_key="sk-test", http_open=opener)
    with pytest.raises(OpenAIError, match="cut off"):
        model("p")


def test_the_completion_budget_fits_a_piece_of_writing() -> None:
    """A cap sized for a click makes every writing goal impossible."""
    opener, sent = _fake_opener(
        _FakeResponse(json.dumps({"choices": [{"message": {"content": "ok"}}]}))
    )
    model = openai_model("gpt-5.6-terra", api_key="sk-test", http_open=opener)
    model("p")
    assert sent[0]["body"]["max_completion_tokens"] >= 2048  # type: ignore[index]


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
    # The label carries what the step was for: a history of bare verbs is no
    # memory at all, and an agent given one re-does what it has already done.
    assert final.completed_steps == (
        "step_0:mouse_click — click the thing",
        "step_1:finish — workflow complete",
    )
    # The distilled trajectory excludes the orchestrator-internal finish.
    assert [a.type for a in runner.executed_trajectory] == ["mouse_click"]
    assert final.last_error is None
    # The first prompt carried the working context (goal + action contract).
    first_prompt = captured[0]["body"]["messages"][0]["content"]
    assert "Goal: g" in first_prompt
    assert "You are operating a real macOS computer" in first_prompt


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


# --- API key resolution ------------------------------------------------------


def test_api_key_resolution_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An exported key wins; otherwise a local .env beats the shared store.

    The precedence matters operationally: a user must be able to run one goal
    against a different key without editing files, and a checkout must be able
    to override the machine default without touching it.
    """
    import computeruse.cli as cli_module

    local = tmp_path / ".env"
    shared = tmp_path / "store-env"
    local.write_text('OPENAI_API_KEY="local-key"\n', encoding="utf-8")
    shared.write_text("# a comment\n\nOPENAI_API_KEY=shared-key\n", encoding="utf-8")
    monkeypatch.setattr(cli_module, "ENV_FILES", (local, shared))

    monkeypatch.setenv("OPENAI_API_KEY", "exported-key")
    assert cli_module.load_api_key() is True
    assert os.environ["OPENAI_API_KEY"] == "exported-key"

    monkeypatch.delenv("OPENAI_API_KEY")
    assert cli_module.load_api_key() is True
    assert os.environ["OPENAI_API_KEY"] == "local-key"

    monkeypatch.delenv("OPENAI_API_KEY")
    monkeypatch.setattr(cli_module, "ENV_FILES", (shared,))
    assert cli_module.load_api_key() is True
    assert os.environ["OPENAI_API_KEY"] == "shared-key"

    monkeypatch.delenv("OPENAI_API_KEY")
    monkeypatch.setattr(cli_module, "ENV_FILES", (tmp_path / "missing",))
    assert cli_module.load_api_key() is False


# --- NET-01: "not now" is not "not ever" ------------------------------------


def _http_error(status: int, message: str, headers: dict[str, str]) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.openai.com/v1/chat/completions",
        status,
        "error",
        headers,  # type: ignore[arg-type]
        io.BytesIO(json.dumps({"error": {"message": message}}).encode("utf-8")),
    )


def test_a_rate_limit_is_retried_rather_than_ending_the_run() -> None:
    """429 clears on its own in seconds; treating it as terminal ended runs.

    Defensible while a human sat watching — they could restart it. Not
    defensible for an unattended session, where one 429 at 3am ends the night.
    """
    opener, captured = _fake_opener(
        _http_error(429, "rate limit reached", {"Retry-After": "0"}),
        _FakeResponse(json.dumps({"choices": [{"message": {"content": "the reply"}}]})),
    )
    model = openai_model("gpt-5.6-terra", api_key="sk-test", http_open=opener)
    assert model("hi") == "the reply"
    assert len(captured) == 2, "the throttled call must be retried, then succeed"


def test_a_service_blip_is_retried() -> None:
    opener, captured = _fake_opener(
        _http_error(503, "service unavailable", {}),
        _FakeResponse(json.dumps({"choices": [{"message": {"content": "the reply"}}]})),
    )
    model = openai_model("gpt-5.6-terra", api_key="sk-test", http_open=opener)
    assert model("hi") == "the reply"
    assert len(captured) == 2


def test_a_bad_key_is_not_retried() -> None:
    """Every other 4xx stays terminal: retrying only delays the real answer."""
    opener, captured = _fake_opener(_http_error(401, "invalid api key", {}))
    model = openai_model("gpt-5.6-terra", api_key="sk-test", http_open=opener)
    with pytest.raises(OpenAIError, match="401"):
        model("hi")
    assert len(captured) == 1, "an unauthorized call must fail on the first try"


def test_a_rate_limit_that_never_clears_still_ends_the_call() -> None:
    """The retry ladder is bounded; a permanent 429 must not loop forever."""
    opener, captured = _fake_opener(
        *[_http_error(429, "quota", {"Retry-After": "0"}) for _ in range(6)]
    )
    model = openai_model("gpt-5.6-terra", api_key="sk-test", http_open=opener)
    with pytest.raises(OpenAIError, match="429"):
        model("hi")
    assert len(captured) == 3, "bounded by _MAX_TRANSPORT_RETRIES"


def test_retry_after_is_honoured_but_capped() -> None:
    """The service knows when it will answer; an absurd value is still refused."""
    assert _retry_after_seconds("2.5", 9.0) == 2.5
    assert _retry_after_seconds(None, 9.0) == 9.0
    assert _retry_after_seconds("not-a-number", 9.0) == 9.0
    assert _retry_after_seconds("0", 9.0) == 9.0
    assert _retry_after_seconds("86400", 9.0) == _RETRY_AFTER_MAX_SECONDS
