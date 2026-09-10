"""OpenCode-provider transport: stream parsing, argv shape, errors."""

from __future__ import annotations

import json

import pytest

from computeruse.providers.cli_bridge import CliBridgeError, CliResult
from computeruse.providers.openai import ModelCallStats
from computeruse.providers.opencode_cli import (
    opencode_model,
    parse_run_stream,
    split_spec,
)

_STREAM = "\n".join(
    [
        json.dumps({"type": "step_start", "part": {"type": "step-start"}}),
        json.dumps(
            {
                "type": "text",
                "part": {
                    "type": "text",
                    "text": '{"thought": "t", "sub_goal": ',
                },
            }
        ),
        json.dumps(
            {"type": "text", "part": {"type": "text", "text": '"s"}'}}
        ),
        json.dumps(
            {
                "type": "step_finish",
                "part": {
                    "type": "step-finish",
                    "tokens": {"input": 70, "output": 8},
                },
            }
        ),
    ]
)


def test_parse_run_stream_concatenates_text_and_sums_tokens() -> None:
    text, prompt_tokens, completion_tokens = parse_run_stream(_STREAM)
    assert json.loads(text)["sub_goal"] == "s"
    assert (prompt_tokens, completion_tokens) == (70, 8)


def test_parse_run_stream_without_text_is_terminal() -> None:
    with pytest.raises(CliBridgeError, match="no text answer"):
        parse_run_stream('{"type": "step_start", "part": {"type": "x"}}\n')


def test_split_spec_requires_provider_slash_model() -> None:
    assert split_spec(None) is None
    assert split_spec("openrouter/x") == "openrouter/x"
    with pytest.raises(CliBridgeError, match="provider/model"):
        split_spec("justaname")


def _runner(
    seen: list[tuple[list[str], str | None]],
    stdout: str,
    returncode: int = 0,
) -> object:
    def run(
        argv: list[str], stdin_text: str | None, timeout: float
    ) -> CliResult:
        seen.append((argv, stdin_text))
        assert timeout == 300.0
        return CliResult(
            returncode=returncode, stdout=stdout, stderr="", elapsed_s=3.0
        )

    return run


def test_model_call_prompt_travels_on_stdin() -> None:
    """Screen-derived text must not sit in argv where any local process
    listing can read it: the prompt rides stdin, flags ride argv."""
    seen: list[tuple[list[str], str | None]] = []
    stats: list[ModelCallStats] = []
    model = opencode_model(
        "openrouter/x",
        timeout_seconds=300.0,
        stats_sink=stats.append,
        runner=_runner(seen, _STREAM),  # type: ignore[arg-type]
    )
    assert json.loads(model("decide this"))["sub_goal"] == "s"
    argv, stdin_text = seen[0]
    assert stdin_text == "decide this"
    assert "decide this" not in argv
    assert argv[argv.index("-m") + 1] == "openrouter/x"
    assert "--format" in argv and argv[argv.index("--format") + 1] == "json"
    assert "-f" not in argv
    assert stats[0].total_tokens == 78
    assert stats[0].elapsed_s == 3.0


def test_bare_spec_omits_the_model_flag() -> None:
    seen: list[list[str]] = []
    model = opencode_model(
        None,
        timeout_seconds=300.0,
        stats_sink=None,
        runner=_runner(seen, _STREAM),  # type: ignore[arg-type]
    )
    model("decide this")
    assert "-m" not in seen[0]


def test_stream_error_beats_a_bare_exit_code() -> None:
    model = opencode_model(
        None,
        timeout_seconds=300.0,
        stats_sink=None,
        runner=_runner([], "garbage", returncode=1),  # type: ignore[arg-type]
    )
    with pytest.raises(CliBridgeError, match="exited 1"):
        model("decide this")
