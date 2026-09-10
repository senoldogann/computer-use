"""ChatGPT-subscription transport for the ``--model`` seam (``codex[:model]``).

Drives the official Codex CLI headlessly (``codex exec``) under the
operator's ChatGPT login — metered against the subscription's rolling
window, never against an API key. The child environment is scrubbed of
API-key variables so a stale export cannot silently move billing.

``--output-schema`` makes the CLI itself enforce the decision contract, so
the scaffold's corrective path stays a backstop rather than the norm.
Usage (input/output tokens) arrives on the ``turn.completed`` event and is
reported through ``stats_sink``; cost is unknown (subscription) and stays
with the caller (``--max-cost`` cannot price it — see ``resolve_cost_price``).
"""

from __future__ import annotations

import json
import logging
import tempfile
from collections.abc import Callable
from pathlib import Path
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
from computeruse.providers.decision_schema import as_dict, strict_decision_schema
from computeruse.providers.openai import ModelCallStats

LOGGER: Final = logging.getLogger(__name__)

#: Env vars that would move billing off the subscription. ``OPENAI_API_KEY``
#: is a Codex login method in its own right; ``CODEX_API_KEY`` is scoped to
#: exactly one invocation by the Codex docs and must never leak in job-wide.
_SCRUB_KEYS: Final[tuple[str, ...]] = ("OPENAI_API_KEY", "CODEX_API_KEY")


def parse_exec_stream(stdout: str) -> tuple[str, int, int]:
    """Final answer text plus token usage from a ``codex exec --json`` stream.

    Pure: returns ``(text, prompt_tokens, completion_tokens)``. An ``error``
    event is terminal with the CLI's own message (quota refusals arrive
    here, not on stderr). A stream with no final agent message is terminal:
    there is nothing to decide from.
    """
    answer: str | None = None
    prompt_tokens = 0
    completion_tokens = 0
    for event in iter_jsonl(stdout, source="codex exec"):
        etype = event.get("type")
        if etype == "error":
            raise CliBridgeError(f"codex exec refused: {event}")
        if etype == "turn.completed":
            usage = as_dict(event.get("usage"))
            prompt_tokens = usage_int(usage.get("input_tokens"))
            completion_tokens = usage_int(usage.get("output_tokens"))
        if etype == "item.completed":
            item = as_dict(event.get("item"))
            if item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    answer = text
    if answer is None:
        raise CliBridgeError(
            "codex exec finished with no final agent message; nothing to decide from"
        )
    return answer, prompt_tokens, completion_tokens


def codex_model(
    model: str | None,
    *,
    timeout_seconds: float,
    stats_sink: Callable[[ModelCallStats], None] | None,
    enforce_decision_schema: bool,
    runner: Runner | None = None,
) -> Callable[..., str]:
    """Build a model callable (prompt, [image_b64] -> reply) on Codex.

    ``model`` is a Codex model id or ``None`` for the CLI default. ``runner``
    is the testability seam (mirrors ``http_open`` in the OpenAI transport);
    production passes nothing and gets the real bridge runner.
    ``enforce_decision_schema`` is True for decide turns (the CLI enforces
    the OODA contract) and False for the completion auditor, whose verdict
    shape is different — enforcing the decision schema there rejects every
    audit reply before it is read.
    """
    binary = find_binary("codex")
    # Built once: a schema that cannot be proven strict must fail here, at
    # startup, never as a rejected turn mid-run.
    _SCHEMA = strict_decision_schema() if enforce_decision_schema else None
    run: Runner = (
        runner
        if runner is not None
        else lambda argv, stdin_text, timeout: run_cli(
            argv, stdin_text, timeout, scrub_keys=_SCRUB_KEYS, cwd=None
        )
    )
    selected = model or "default"

    def model_call(prompt: str, image_b64: str | None = None) -> str:
        # The schema file exists only when enforced; without it there is no
        # flag either. The auditor's verdict has its own shape, and an
        # absent flag cannot reject it.
        schema_path: str | None = None
        if _SCHEMA is not None:
            with tempfile.NamedTemporaryFile(
                suffix=".json", prefix="computeruse-codex-schema-", delete=False
            ) as schema_file:
                schema_file.write(json.dumps(_SCHEMA).encode("utf-8"))
            schema_path = schema_file.name
        try:
            argv: list[str] = [binary, "exec", "--json"]
            if schema_path is not None:
                argv += ["--output-schema", schema_path]
            argv += [
                "--ephemeral",
                # The operator's config.toml is for interactive use and must
                # not leak into a headless decide turn: measured live, a
                # codex-router-managed config pointed the transport at a
                # dead local proxy (exit 1) and carried danger-full-access
                # + approval never. Authentication still uses CODEX_HOME.
                "--ignore-user-config",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
            ]
            if model:
                argv += ["-m", model]
            if image_b64 is not None:
                with TempPng(image_b64, source="codex exec") as image_path:
                    argv += ["-i", image_path, "-"]
                    return _invoke(
                        run, argv, prompt, timeout_seconds, selected, stats_sink
                    )
            argv += ["-"]
            return _invoke(run, argv, prompt, timeout_seconds, selected, stats_sink)
        finally:
            if schema_path is not None:
                try:
                    Path(schema_path).unlink(missing_ok=True)
                except OSError as exc:
                    LOGGER.warning("could not remove codex schema file: %s", exc)

    return model_call


def _invoke(
    run: Runner,
    argv: list[str],
    prompt: str,
    timeout_seconds: float,
    selected: str,
    stats_sink: Callable[[ModelCallStats], None] | None,
) -> str:
    """One bounded exec call: run, parse, then check, report usage."""
    result = run(argv, prompt, timeout_seconds)
    # Parse first, like the Claude transport: refusals arrive as events on
    # stdout and the CLI's own words beat the exit code.
    try:
        answer, prompt_tokens, completion_tokens = parse_exec_stream(result.stdout)
    except CliBridgeError as exc:
        if result.returncode != 0:
            raise CliBridgeError(
                f"codex exec exited {result.returncode}: "
                f"{result.stderr or exc}"
            ) from exc
        raise
    if result.returncode != 0:
        LOGGER.warning(
            "codex exec exited %d but answered validly; using the answer",
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
        "codex %s: %.1fs, %d tokens (prompt %d / completion %d)",
        selected,
        result.elapsed_s,
        prompt_tokens + completion_tokens,
        prompt_tokens,
        completion_tokens,
    )
    return answer
