"""Claude-subscription transport: result parsing, argv shape, errors."""

from __future__ import annotations

import json

import pytest

from computeruse.providers.claude_cli import (
    claude_model,
    parse_print_result,
)
from computeruse.providers.cli_bridge import CliBridgeError, CliResult
from computeruse.providers.openai import ModelCallStats

_OK = json.dumps(
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": '{"thought": "t", "sub_goal": "s"}',
        "usage": {"input_tokens": 50, "output_tokens": 10},
        "total_cost_usd": 0,
        "num_turns": 1,
    }
)

_STRUCTURED = json.dumps(
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "free text",
        "structured_output": {"thought": "t", "sub_goal": "s"},
        "usage": {"input_tokens": 60, "output_tokens": 12},
        "total_cost_usd": 0,
        "num_turns": 1,
    }
)

_QUOTA = json.dumps(
    {
        "type": "result",
        "subtype": "error",
        "is_error": True,
        "result": "You've hit your weekly limit · resets 7am (Europe/Helsinki)",
        "usage": {"input_tokens": 0, "output_tokens": 0},
        "total_cost_usd": 0,
        "num_turns": 1,
    }
)


def test_parse_print_result_reads_text_and_usage() -> None:
    text, prompt_tokens, completion_tokens = parse_print_result(
        _OK, structured=False
    )
    assert json.loads(text)["sub_goal"] == "s"
    assert (prompt_tokens, completion_tokens) == (50, 10)


def test_parse_print_result_prefers_structured_output() -> None:
    text, _, _ = parse_print_result(_STRUCTURED, structured=True)
    assert json.loads(text)["sub_goal"] == "s"


def test_parse_print_result_quota_refusal_is_terminal() -> None:
    """A spent quota is a thing the operator must fix: retrying it only
    delays their finding out (and burns nothing by waiting)."""
    with pytest.raises(CliBridgeError, match="weekly limit"):
        parse_print_result(_QUOTA, structured=False)


def test_parse_print_result_rejects_garbage() -> None:
    with pytest.raises(CliBridgeError, match="non-JSON"):
        parse_print_result("not json", structured=False)
    with pytest.raises(CliBridgeError, match="no answer text"):
        parse_print_result(
            json.dumps({"type": "result", "is_error": False}), structured=False
        )


def _runner(
    seen_argv: list[list[str]],
    stdout: str,
    returncode: int = 0,
) -> object:
    def run(
        argv: list[str], stdin_text: str | None, timeout: float
    ) -> CliResult:
        seen_argv.append(argv)
        assert stdin_text is None
        assert timeout == 300.0
        return CliResult(
            returncode=returncode, stdout=stdout, stderr="", elapsed_s=2.0
        )

    return run


def test_model_call_argv_shape_and_stats() -> None:
    seen: list[list[str]] = []
    stats: list[ModelCallStats] = []
    model = claude_model(
        "sonnet",
        timeout_seconds=300.0,
        stats_sink=stats.append,
        enforce_decision_schema=True,
        runner=_runner(seen, _OK),  # type: ignore[arg-type]
    )
    assert json.loads(model("decide this"))["sub_goal"] == "s"
    argv = seen[0]
    assert argv[:3] == [argv[0], "-p", "decide this"]
    assert "--output-format" in argv and argv[argv.index("--output-format") + 1] == "json"
    assert "--max-turns" in argv and argv[argv.index("--max-turns") + 1] == "1"
    assert "--model" in argv and argv[argv.index("--model") + 1] == "sonnet"
    assert "--allowedTools" in argv and argv[argv.index("--allowedTools") + 1] == ""
    assert "--json-schema" in argv
    assert stats[0].total_tokens == 60
    assert stats[0].elapsed_s == 2.0


def test_auditor_binding_omits_the_schema_flag() -> None:
    seen: list[list[str]] = []
    model = claude_model(
        None,
        timeout_seconds=300.0,
        stats_sink=None,
        enforce_decision_schema=False,
        runner=_runner(seen, _OK),  # type: ignore[arg-type]
    )
    model("audit this claim")
    assert "--json-schema" not in seen[0]
    assert "-m" not in seen[0] and "--model" not in seen[0]


def test_stdout_refusal_beats_a_bare_exit_code() -> None:
    """The quota refusal arrives as JSON on stdout with exit 1 and empty
    stderr: the parse must surface the CLI's words, not 'no reason given'."""
    model = claude_model(
        None,
        timeout_seconds=300.0,
        stats_sink=None,
        enforce_decision_schema=False,
        runner=_runner([], _QUOTA, returncode=1),  # type: ignore[arg-type]
    )
    with pytest.raises(CliBridgeError, match="weekly limit"):
        model("decide this")


def test_unparseable_stdout_falls_back_to_the_exit_code() -> None:
    model = claude_model(
        None,
        timeout_seconds=300.0,
        stats_sink=None,
        enforce_decision_schema=True,
        runner=_runner([], "garbage", returncode=1),  # type: ignore[arg-type]
    )
    with pytest.raises(CliBridgeError, match="exited 1"):
        model("decide this")
