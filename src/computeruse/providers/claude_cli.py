"""Claude-subscription transport for the ``--model`` seam (``claude[:model]``).

Drives the official Claude Code CLI headlessly (``claude -p``) under the
operator's subscription login — usage draws from the plan, never from
metered API. The child environment is scrubbed of API-key variables so a
stale export cannot silently move billing (``ANTHROPIC_API_KEY`` outranks
the subscription login). The long-lived ``CLAUDE_CODE_OAUTH_TOKEN`` is
deliberately NOT scrubbed: it *is* the subscription.

One decide turn is one ``-p`` call bounded by ``--max-turns 1`` with no
tools allowed, so the agent loop cannot wander: the scaffold's prompt is
answered, not explored. ``--json-schema`` enforces the OODA contract on
decide turns exactly like Codex ``--output-schema``; the auditor binding
omits it (its verdict has its own shape).

Known live limitation: screenshots ride a temp file read back through the
``Read`` tool, which needs one tool turn and is unverified against the
live CLI (the subscription's weekly limit was exhausted during
implementation). Text-only decide turns are fully verified.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Final, cast

from computeruse.providers.cli_bridge import (
    CliBridgeError,
    Runner,
    TempPng,
    find_binary,
    run_cli,
    usage_int,
)
from computeruse.providers.decision_schema import as_dict, strict_decision_schema
from computeruse.providers.openai import ModelCallStats

LOGGER: Final = logging.getLogger(__name__)

#: Env vars that would move billing off the subscription. ``ANTHROPIC_API_KEY``
#: and ``ANTHROPIC_AUTH_TOKEN`` both outrank the subscription login in the
#: CLI's credential precedence; a forgotten export would meter API usage
#: while the operator believes the plan is paying.
_SCRUB_KEYS: Final[tuple[str, ...]] = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

# Prompt-delivery caveat: unlike the sibling transports this one passes the
# prompt as an argv element, so a local process listing can read
# screen-derived text. Switching to stdin is pending live verification —
# it needs one successful subscription turn, and the weekly limit was
# exhausted before that probe could run. See the opencode transport, which
# already delivers via stdin, for the target shape.


def parse_print_result(stdout: str, *, structured: bool) -> tuple[str, int, int]:
    """Answer text plus token usage from ``claude -p --output-format json``.

    Pure: returns ``(text, prompt_tokens, completion_tokens)``. An error
    payload (quota exhaustion arrives here as ``is_error`` with the plan's
    own words, e.g. the weekly limit) is terminal with that text — retrying
    a spent quota only delays the operator finding out. With a schema the
    validated object wins over the free-text result; a dict-shaped object
    is re-serialized because the scaffold parses text, not objects.
    """
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise CliBridgeError(
            f"claude -p emitted non-JSON output: {stdout[:200]} ({exc})"
        ) from exc
    if not isinstance(payload, dict):
        raise CliBridgeError("claude -p emitted a non-object payload")
    record = as_dict(cast(object, payload))
    if record.get("is_error") is True:
        raise CliBridgeError(f"claude -p refused: {record.get('result')!r}")
    text: object = None
    if structured:
        text = record.get("structured_output")
    if text is None:
        text = record.get("result")
    if isinstance(text, dict):
        text = json.dumps(text, ensure_ascii=False)
    if not isinstance(text, str) or not text.strip():
        raise CliBridgeError(
            "claude -p finished with no answer text; nothing to decide from"
        )
    usage = as_dict(record.get("usage"))
    return text, usage_int(usage.get("input_tokens")), usage_int(
        usage.get("output_tokens")
    )


def claude_model(
    model: str | None,
    *,
    timeout_seconds: float,
    stats_sink: Callable[[ModelCallStats], None] | None,
    enforce_decision_schema: bool,
    runner: Runner | None = None,
) -> Callable[..., str]:
    """Build a model callable (prompt, [image_b64] -> reply) on Claude.

    ``model`` is a Claude model id or ``None`` for the CLI default.
    ``runner`` is the testability seam (mirrors ``http_open`` in the OpenAI
    transport); production passes nothing and gets the real bridge runner.
    """
    binary = find_binary("claude")
    run: Runner = (
        runner
        if runner is not None
        else lambda argv, stdin_text, timeout: run_cli(
            argv, stdin_text, timeout, scrub_keys=_SCRUB_KEYS, cwd=None
        )
    )
    selected = model or "default"
    # Built once: a schema that cannot be proven strict must fail here, at
    # startup, never as a rejected turn mid-run.
    _SCHEMA = strict_decision_schema() if enforce_decision_schema else None

    def model_call(prompt: str, image_b64: str | None = None) -> str:
        asked = prompt
        tools = ""
        if _SCHEMA is not None:
            schema_flag = ["--json-schema", json.dumps(_SCHEMA)]
        else:
            schema_flag = []
        if image_b64 is not None:
            # No image-file flag exists on -p: the frame rides a temp file
            # the agent reads back. Read-only and single-purpose — the only
            # tool the turn may touch.
            with TempPng(image_b64, source="claude -p") as image_path:
                asked = (
                    f"{prompt}\n\nThe current screen is saved at {image_path}: "
                    "read it with the Read tool before deciding, then "
                    "answer in the required shape."
                )
                tools = "Read"
                return _invoke(
                    run,
                    _argv(binary, asked, schema_flag, model, tools),
                    timeout_seconds,
                    selected,
                    stats_sink,
                    structured=_SCHEMA is not None,
                )
        return _invoke(
            run,
            _argv(binary, asked, schema_flag, model, tools),
            timeout_seconds,
            selected,
            stats_sink,
            structured=_SCHEMA is not None,
        )

    return model_call


def _argv(
    binary: str,
    prompt: str,
    schema_flag: list[str],
    model: str | None,
    tools: str,
) -> list[str]:
    """One -p invocation shape (pure): prompt first, flags after."""
    argv: list[str] = [binary, "-p", prompt, "--output-format", "json"]
    argv += schema_flag
    argv += ["--max-turns", "1"]
    if model:
        argv += ["--model", model]
    return [*argv, "--allowedTools", tools]


def _invoke(
    run: Runner,
    argv: list[str],
    timeout_seconds: float,
    selected: str,
    stats_sink: Callable[[ModelCallStats], None] | None,
    *,
    structured: bool,
) -> str:
    """One bounded -p call: run, parse, then check, report usage."""
    result = run(argv, None, timeout_seconds)
    # Parse first: refusals (quota exhaustion included) arrive as JSON on
    # stdout with a nonzero exit, and the CLI's own words beat the exit
    # code. A valid answer despite the exit code is returned with a
    # warning — the message passed shape validation either way.
    try:
        answer, prompt_tokens, completion_tokens = parse_print_result(
            result.stdout, structured=structured
        )
    except CliBridgeError as exc:
        if result.returncode != 0:
            raise CliBridgeError(
                f"claude -p exited {result.returncode}: "
                f"{result.stderr or exc}"
            ) from exc
        raise
    if result.returncode != 0:
        LOGGER.warning(
            "claude -p exited %d but answered validly; using the answer",
            result.returncode,
        )
    if stats_sink is not None:
        stats_sink(
            ModelCallStats(
                total_tokens=prompt_tokens + completion_tokens,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                elapsed_s=result.elapsed_s,
            )
        )
    LOGGER.info(
        "claude %s: %.1fs, %d tokens (prompt %d / completion %d)",
        selected,
        result.elapsed_s,
        prompt_tokens + completion_tokens,
        prompt_tokens,
        completion_tokens,
    )
    return answer
