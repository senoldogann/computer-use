"""OpenCode-provider transport for the ``--model`` seam (``opencode[:provider/model]``).

Drives the official OpenCode CLI headlessly (``opencode run``) against any
provider already authenticated in ``~/.local/share/opencode/auth.json`` —
Copilot OAuth, API keys, free tiers alike. No schema enforcement exists on
this path, so the scaffold's corrective retry stays the contract enforcer
(measured live: shape drift happens and is recovered).

Two orderings matter and both are load-bearing. The message precedes the
flags: ``-f`` before the message swallows the message as a second file
(measured live). The model flag pins ``provider/model`` explicitly; a bare
``opencode`` omits ``-m`` and inherits the CLI default. No env scrubbing:
unlike the subscription CLIs there is no single billing identity to
protect — the named provider bills its own way, which is the choice the
spec already expresses.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Final

from computeruse.providers.cli_bridge import (
    CliBridgeError,
    Runner,
    TempPng,
    find_binary,
    iter_jsonl,
    run_cli,
    usage_int,
)
from computeruse.providers.decision_schema import as_dict
from computeruse.providers.openai import ModelCallStats

LOGGER: Final = logging.getLogger(__name__)


def parse_run_stream(stdout: str) -> tuple[str, int, int]:
    """Answer text plus token usage from ``opencode run --format json``.

    Pure: returns ``(text, prompt_tokens, completion_tokens)``. ``text``
    parts concatenate in arrival order across steps; token counts sum over
    every ``step_finish`` (one answer can span steps when the agent uses
    tools despite the no-tools instruction). Empty text is terminal: there
    is nothing to decide from.
    """
    texts: list[str] = []
    prompt_tokens = 0
    completion_tokens = 0
    for event in iter_jsonl(stdout, source="opencode run"):
        part = as_dict(event.get("part"))
        part_type = part.get("type")
        if part_type == "text":
            text = part.get("text")
            if isinstance(text, str):
                texts.append(text)
        elif part_type == "step-finish":
            tokens = as_dict(part.get("tokens"))
            prompt_tokens += usage_int(tokens.get("input"))
            completion_tokens += usage_int(tokens.get("output"))
    answer = "".join(texts).strip()
    if not answer:
        raise CliBridgeError(
            "opencode run finished with no text answer; nothing to decide from"
        )
    return answer, prompt_tokens, completion_tokens


def split_spec(spec: str | None) -> str | None:
    """``provider/model`` from a ``--model opencode:...`` spec (pure).

    ``None`` (bare ``opencode``) means inherit the CLI default. Anything
    else must name ``provider/model`` — failing here names the fix
    (``opencode models`` lists them) instead of letting the CLI fail
    opaquely three layers down.
    """
    if spec is None:
        return None
    if "/" not in spec:
        raise CliBridgeError(
            f"opencode model must be 'provider/model', got {spec!r}; "
            "run `opencode models` to list them"
        )
    return spec


def opencode_model(
    model: str | None,
    *,
    timeout_seconds: float,
    stats_sink: Callable[[ModelCallStats], None] | None,
    runner: Runner | None = None,
) -> Callable[..., str]:
    """Build a model callable (prompt, [image_b64] -> reply) on OpenCode.

    ``model`` is ``provider/model`` or ``None`` for the CLI default.
    ``runner`` is the testability seam (mirrors ``http_open`` in the OpenAI
    transport); production passes nothing and gets the real bridge runner.
    """
    binary = find_binary("opencode")
    run: Runner = (
        runner
        if runner is not None
        else lambda argv, stdin_text, timeout: run_cli(
            argv, stdin_text, timeout, scrub_keys=(), cwd=None
        )
    )
    selected = split_spec(model)

    def model_call(prompt: str, image_b64: str | None = None) -> str:
        # The prompt travels on stdin, never argv: screen-derived text in a
        # command line is readable by any local process listing (ps), while
        # stdin is not. (An earlier revision passed the message positionally
        # and learned that -f before it eats the message as a file path —
        # moot now that no positional message exists.)
        # -m pins provider/model; omitted for the bare spec so the CLI
        # default applies.
        argv: list[str] = [binary, "run"]
        if selected is not None:
            argv += ["-m", selected]
        argv += ["--format", "json"]
        if image_b64 is not None:
            with TempPng(image_b64, source="opencode run") as image_path:
                return _invoke(
                    run,
                    [*argv, "-f", image_path],
                    prompt,
                    selected or "default",
                    timeout_seconds,
                    stats_sink,
                )
        return _invoke(
            run, argv, prompt, selected or "default", timeout_seconds, stats_sink
        )

    return model_call


def _invoke(
    run: Runner,
    argv: list[str],
    prompt: str,
    selected: str,
    timeout_seconds: float,
    stats_sink: Callable[[ModelCallStats], None] | None,
) -> str:
    """One bounded run call: run, parse, then check, report usage."""
    result = run(argv, prompt, timeout_seconds)
    # Parse first, like the sibling transports: the CLI's own stream beats
    # the exit code, and an empty answer with exit 0 is still a failure.
    try:
        answer, prompt_tokens, completion_tokens = parse_run_stream(result.stdout)
    except CliBridgeError as exc:
        if result.returncode != 0:
            raise CliBridgeError(
                f"opencode run exited {result.returncode}: "
                f"{result.stderr or exc}"
            ) from exc
        raise
    if result.returncode != 0:
        LOGGER.warning(
            "opencode run exited %d but answered validly; using the answer",
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
        "opencode %s: %.1fs, %d tokens (prompt %d / completion %d)",
        selected,
        result.elapsed_s,
        prompt_tokens + completion_tokens,
        prompt_tokens,
        completion_tokens,
    )
    return answer
