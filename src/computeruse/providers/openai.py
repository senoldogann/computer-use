"""OpenAI transport for the ``--model`` seam (Law 2.1).

The orchestrator's scaffold (`orchestrator/prompts.py`) turns any raw
``Callable[[str], str]`` model into a well-behaved provider: it builds the
prompt, parses the reply through the Pydantic gate, and re-prompts with
corrective hints on invalid JSON. This module is the *transport* half — a
plain ``prompt -> text`` callable backed by OpenAI's Chat Completions API.

Model choice (August 2026 lineup): the default is ``gpt-5.6-terra`` — the
balanced tier of the GPT-5.6 family ($2.50/$15 per 1M tokens). Our loop makes
one small JSON decision per turn against a compact context (goal, UI-element
summaries, mounted skill, action contract), and the scaffold + visual
verification already compensate for weaker models — so the flagship
``gpt-5.6-sol`` ($5/$30) is 2x the price for marginal gain, while
``gpt-5.6-luna`` ($1/$6) risks hallucinated coordinates on a *physical* host,
where a miss costs real retries. Terra is the cost/quality sweet spot; pass
``openai:gpt-5.6-luna`` (or any other id) to override.

Cost friendliness: the API key comes from ``OPENAI_API_KEY`` (never from the
repo). The scaffold's ``decision_prompt`` keeps the stable prefix (application
name + action contract) before the per-turn state, so OpenAI's automatic
prompt caching discounts the repeated prefix by ~90% as a run progresses.

Errors are explicit (:class:`OpenAIError`) with the API's own message, so the
OODA loop folds the real reason into ``last_error`` instead of a generic
failure (Law 6.3).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Final, Protocol, Self, cast

# The balanced GPT-5.6 tier (see module docstring for the rationale).
DEFAULT_MODEL: Final[str] = os.environ.get("OPENAI_MODEL", "gpt-5.6-terra")

OPENAI_CHAT_URL: Final[str] = "https://api.openai.com/v1/chat/completions"


class HttpResponse(Protocol):
    """Minimal response surface required by the transport."""

    def __enter__(self) -> Self: ...
    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None: ...
    def read(self) -> bytes: ...


Opener = Callable[..., HttpResponse]


class OpenAIError(RuntimeError):
    """An OpenAI API call failed; carries the API's own message for context."""


def _require_dict(value: object, what: str) -> dict[str, object]:
    """Narrow an unknown JSON node to a fully-typed dict (Law 6 strict typing).

    ``isinstance`` narrows to ``dict[Unknown, Unknown]`` under strict mode, so
    we cast to the fully-typed shape — the value came from ``json.loads``, so
    its keys are strings by construction.
    """
    if not isinstance(value, dict):
        raise OpenAIError(f"OpenAI response {what} is malformed")
    return cast(dict[str, object], value)


def openai_model(
    model: str | None = None,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
    timeout_seconds: float = 180.0,
    max_tokens: int = 512,
    http_open: Opener | None = None,
) -> Callable[..., str]:
    """Build a model callable (prompt, [image_b64] -> reply) backed by OpenAI.

    ``model`` defaults to :data:`DEFAULT_MODEL` (``gpt-5.6-terra``). The API
    key is read from ``OPENAI_API_KEY`` unless ``api_key`` is passed — the key
    must never be committed or hardcoded. Supports multimodal visual perception
    when an ``image_b64`` PNG is passed. ``response_format`` requests strict
    JSON so the scaffold's parser gets well-formed output.
    """
    selected = model or DEFAULT_MODEL
    key = api_key or os.environ.get("OPENAI_API_KEY")
    if not key:
        raise OpenAIError(
            "OPENAI_API_KEY is not set; export it (e.g. export OPENAI_API_KEY=...) "
            "before running with --model openai"
        )
    opener = http_open if http_open is not None else urllib.request.urlopen

    def model_call(prompt: str, image_b64: str | None = None) -> str:
        if image_b64:
            user_content: list[dict[str, object]] = [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{image_b64}",
                        "detail": "high",
                    },
                },
            ]
            messages: list[dict[str, object]] = [{"role": "user", "content": user_content}]
        else:
            messages = [{"role": "user", "content": prompt}]

        body: dict[str, object] = {
            "model": selected,
            "messages": messages,
            # The action contract in the prompt instructs JSON; this makes the
            # API enforce it, so parse_decision rarely needs its corrective path.
            "response_format": {"type": "json_object"},
            # A single JSON action decision is ~100-200 tokens; cap at 512 to
            # prevent the model from rambling and wasting latency/cost.
            # Newer OpenAI models require max_completion_tokens instead of max_tokens.
            "max_completion_tokens": max_tokens,
        }
        request = urllib.request.Request(
            base_url or OPENAI_CHAT_URL,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with opener(request, timeout=timeout_seconds) as response:
                payload: dict[str, object] = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # HTTPError is an OSError; catch it first and surface the API's
            # own body (e.g. "model not found", "insufficient quota").
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise OpenAIError(f"OpenAI API error {exc.code}: {detail}") from exc
        except OSError as exc:
            raise OpenAIError(f"OpenAI request failed: {exc}") from exc
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise OpenAIError(f"OpenAI response missing choices: {payload}")
        choice = _require_dict(cast(object, choices[0]), "choice")
        message = _require_dict(choice.get("message"), "message")
        content = message.get("content")
        if not isinstance(content, str):
            raise OpenAIError(f"OpenAI response missing text content: {payload}")
        return content

    return model_call
