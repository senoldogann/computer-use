"""OpenAI transport for the ``--model`` seam (Law 2.1).

The orchestrator's scaffold (`orchestrator/prompts.py`) turns any raw
``Callable[[str], str]`` model into a well-behaved provider: it builds the
prompt, parses the reply through the Pydantic gate, and re-prompts with
corrective hints on invalid JSON. This module is the *transport* half — a
plain ``prompt -> text`` callable backed by OpenAI's Chat Completions API.

Model choice (August 2026 lineup): the default is ``gpt-5.6-terra`` — the
balanced tier of the GPT-5.6 family. OpenAI's July 30, 2026 price cut set
Terra at $2/$12 per 1M tokens (input/output), Luna at $0.20/$1.20, and left
Sol at $5/$30. Our loop makes one small JSON decision per turn against a
compact context (goal, UI-element summaries, mounted skill, action
contract), and the scaffold + visual verification already compensate for
weaker models — so the flagship ``gpt-5.6-sol`` is ~2.5x Terra's price for
marginal gain, while ``gpt-5.6-luna`` risks hallucinated coordinates on a
*physical* host, where a miss costs real retries. Terra remains the
cost/quality sweet spot; pass ``openai:gpt-5.6-luna`` (or any other id) to
override.

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
import logging
import os
import ssl
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Final, Protocol, Self, cast

LOGGER = logging.getLogger(__name__)

# The balanced GPT-5.6 tier (see module docstring for the rationale).
DEFAULT_MODEL: Final[str] = os.environ.get("OPENAI_MODEL", "gpt-5.6-terra")

OPENAI_CHAT_URL: Final[str] = "https://api.openai.com/v1/chat/completions"


class HttpResponse(Protocol):
    """Minimal response surface required by the transport."""

    def __enter__(self) -> Self: ...
    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None: ...
    def read(self) -> bytes: ...


Opener = Callable[..., HttpResponse]

# A transport failure is *transient* (worth retrying) only when it smells
# like the wire: timeouts, resets, and TLS alerts. Anything else (e.g. a
# protocol bug in the opener) fails fast — re-running won't fix it.
_MAX_TRANSPORT_RETRIES: Final[int] = 3
_TRANSPORT_BACKOFF_BASE_SECONDS: Final[float] = 0.5


def _is_retryable_oserror(exc: OSError) -> bool:
    """Pure: is this OS error one a retry could plausibly fix?"""
    return isinstance(exc, (TimeoutError, ConnectionError, ssl.SSLError))


@dataclass(frozen=True)
class ModelCallStats:
    """Usage + latency of one successful model call (pure data).

    Consumed by a caller-provided sink so the panel/CLI can show live token
    and elapsed-time counters without the transport knowing anything about
    the UI layer.
    """

    total_tokens: int
    prompt_tokens: int
    completion_tokens: int
    elapsed_s: float


class OpenAIError(RuntimeError):
    """An OpenAI API call failed; carries the API's own message for context."""


@dataclass(frozen=True)
class TokenPrice:
    """USD per 1M tokens for one model (pure data)."""

    input_per_million: float
    output_per_million: float


#: Published list prices, USD per 1M tokens, from OpenAI's July 30 2026 price
#: cut (the same figures the module docstring reasons about). Used only for the
#: CLI's ``--max-cost`` guardrail: it is a ceiling the operator sets so an
#: unattended run cannot spend unboundedly, NOT an invoice. A model missing
#: from this table has no price here, and the flag says so rather than
#: guessing — a wrong number would be worse than no number.
MODEL_PRICES: Final[Mapping[str, TokenPrice]] = {
    "gpt-5.6-terra": TokenPrice(input_per_million=2.0, output_per_million=12.0),
    "gpt-5.6-luna": TokenPrice(input_per_million=0.20, output_per_million=1.20),
    "gpt-5.6-sol": TokenPrice(input_per_million=5.0, output_per_million=30.0),
}


def price_for(model: str) -> TokenPrice:
    """The published price of one model, or a typed error naming the gap."""
    price = MODEL_PRICES.get(model)
    if price is None:
        known = ", ".join(sorted(MODEL_PRICES))
        raise OpenAIError(
            f"no published price is known for model {model!r}, so a cost budget "
            f"cannot be enforced for it (priced models: {known}). Use a token "
            "budget instead."
        )
    return price


def call_cost_usd(price: TokenPrice, stats: ModelCallStats) -> float:
    """What one model call cost at the given price (pure)."""
    return (
        stats.prompt_tokens * price.input_per_million
        + stats.completion_tokens * price.output_per_million
    ) / 1_000_000


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
    max_tokens: int = 2048,
    http_open: Opener | None = None,
    stats_sink: Callable[[ModelCallStats], None] | None = None,
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
        started_at = time.perf_counter()
        if image_b64:
            user_content: list[dict[str, object]] = [
                {"type": "text", "text": prompt},
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{image_b64}",
                        # The frame is already a 512px-max screenshot map
                        # (downscale_to_max_side), so "low" detail is a no-op:
                        # the model sees exactly the map whose scale factor
                        # the coordinate gate knows. ~85 tokens instead of
                        # ~1,100 (512px tiling) per turn — a real latency/cost
                        # win per step, with coordinates still exact.
                        "detail": "low",
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
            # A click decision is ~100-200 tokens, and 512 was set for that.
            # But `type_text` and `clipboard_paste` carry their payload inside
            # the same JSON object, so the cap silently made every goal that
            # writes something longer than a sentence impossible: the object
            # was cut mid-string and came back as a contract violation, not as
            # truncated text. Measured on "research the AI news and write me a
            # summary in Notes" — four consecutive invalid decisions, every one
            # of them exactly 512 completion tokens, and the run gave up
            # without ever typing a word. Still bounded, and unspent capacity
            # costs nothing.
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
        # Transient transport failures (timeouts, connection resets, TLS/SSL
        # alerts like SSLV3_ALERT_BAD_RECORD_MAC from a flaky middlebox) must
        # never kill a run outright: exponential backoff with warning logs
        # (AGENTS.md §7.2) before raising the terminal OpenAIError. HTTP-level
        # errors stay terminal — the API answered, and 4xx/5xx tell the user
        # what to fix (key, quota, model).
        attempt = 0
        while True:
            try:
                with opener(request, timeout=timeout_seconds) as response:
                    payload: dict[str, object] = json.loads(response.read().decode("utf-8"))
                break
            except urllib.error.HTTPError as exc:
                # HTTPError is an OSError; catch it first and surface the API's
                # own body (e.g. "model not found", "insufficient quota").
                detail = exc.read().decode("utf-8", errors="replace")[:500]
                raise OpenAIError(f"OpenAI API error {exc.code}: {detail}") from exc
            except OSError as exc:
                attempt += 1
                if attempt >= _MAX_TRANSPORT_RETRIES or not _is_retryable_oserror(exc):
                    raise OpenAIError(f"OpenAI request failed: {exc}") from exc
                wait_s = _TRANSPORT_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                LOGGER.warning(
                    "OpenAI transport failure (%s); retrying in %.1fs (attempt %d/%d)",
                    exc,
                    wait_s,
                    attempt,
                    _MAX_TRANSPORT_RETRIES,
                )
                time.sleep(wait_s)
        elapsed_s = time.perf_counter() - started_at
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            raise OpenAIError(f"OpenAI response missing choices: {payload}")
        choice = _require_dict(cast(object, choices[0]), "choice")
        message = _require_dict(choice.get("message"), "message")
        content = message.get("content")
        if not isinstance(content, str):
            raise OpenAIError(f"OpenAI response missing text content: {payload}")
        # A cut-off reply is not a malformed one, and saying so is the
        # difference between a model that shortens its answer and one that
        # keeps re-sending the same too-long object until the run gives up.
        if choice.get("finish_reason") == "length":
            raise OpenAIError(
                f"OpenAI stopped at the {max_tokens}-token completion limit, so "
                "the JSON object is cut off and cannot be parsed. Whatever text "
                "the action carries has to be shorter."
            )
        # usage is optional on the wire; when absent (proxies, some fakes) the
        # sink still gets the latency with zeroed token counts.
        usage_raw = payload.get("usage")
        # dict[Unknown, Unknown] from the isinstance narrowing would poison
        # strict typing; an explicit cast keeps the element type concrete.
        usage: dict[str, object]
        if isinstance(usage_raw, dict):
            usage = cast(dict[str, object], usage_raw)
        else:
            usage = dict[str, object]()  # proxies may omit usage entirely

        def usage_int(key: str, default: int = 0) -> int:
            """Read an int usage field, tolerating proxies that send strings."""
            value = usage.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, str)):
                return default
            try:
                return int(value)
            except ValueError:
                return default

        prompt_tokens = usage_int("prompt_tokens")
        completion_tokens = usage_int("completion_tokens")
        total = prompt_tokens + completion_tokens
        LOGGER.info(
            "openai %s: %.1fs, %d tokens (prompt %d / completion %d)",
            selected,
            elapsed_s,
            total,
            prompt_tokens,
            completion_tokens,
        )
        if stats_sink is not None:
            stats_sink(
                ModelCallStats(
                    total_tokens=total,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    elapsed_s=elapsed_s,
                )
            )
        return content

    return model_call
