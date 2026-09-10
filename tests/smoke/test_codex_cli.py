"""ChatGPT-subscription transport: stream parsing, argv shape, errors."""

from __future__ import annotations

import json
from typing import get_args

import pytest
from pydantic import BaseModel

from computeruse.providers.cli_bridge import CliBridgeError, CliResult
from computeruse.providers.codex_cli import codex_model, parse_exec_stream
from computeruse.providers.decision_schema import strict_decision_schema
from computeruse.providers.openai import ModelCallStats

_MESSAGE_TEXT = json.dumps(
    {"thought": "t", "sub_goal": "s", "action": {"type": "wait", "duration_ms": 1, "reason": "r"}}
)
_STREAM = "\n".join(
    [
        json.dumps({"type": "thread.started"}),
        "",
        json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": _MESSAGE_TEXT},
            }
        ),
        json.dumps(
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 100, "output_tokens": 20},
            }
        ),
    ]
)


def test_parse_exec_stream_returns_final_message_and_usage() -> None:
    text, prompt_tokens, completion_tokens = parse_exec_stream(_STREAM)
    assert json.loads(text)["sub_goal"] == "s"
    assert (prompt_tokens, completion_tokens) == (100, 20)


def test_parse_exec_stream_error_event_is_terminal() -> None:
    with pytest.raises(CliBridgeError, match="refused"):
        parse_exec_stream('{"type": "error", "message": "quota exceeded"}\n')


def test_parse_exec_stream_without_answer_is_terminal() -> None:
    with pytest.raises(CliBridgeError, match="no final agent message"):
        parse_exec_stream('{"type": "thread.started"}\n')


def _walk(node: object) -> object:
    """Yield every dict node of a schema tree (pure test helper)."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def test_strict_schema_is_fully_specified() -> None:
    """Every rule the live backend enforced, pinned: complete ``required``,
    no references, no consts, no free-form maps, no stripped keywords."""
    schema = strict_decision_schema()
    assert not isinstance(schema.get("$defs"), dict)
    for node in _walk(schema):
        assert isinstance(node, dict)
        assert "$ref" not in node
        assert "discriminator" not in node
        assert "const" not in node
        assert "default" not in node
        assert not (set(node) & {"minimum", "maximum", "pattern", "minLength"})
        if node.get("type") == "object" or "properties" in node:
            assert node.get("additionalProperties") is False
            assert set(node.get("required", [])) == set(node.get("properties", {}))


def test_strict_schema_covers_every_action_but_call_tool() -> None:
    """call_tool.arguments is a free-form map the strict subset cannot
    express; everything else must survive normalization."""
    from computeruse.orchestrator.schemas import ActionUnion

    schema = strict_decision_schema()
    action = schema["properties"]["action"]
    assert isinstance(action, dict)
    variants = action["anyOf"]
    assert isinstance(variants, list)
    names = {
        member["properties"]["type"]["enum"][0]
        for member in variants
        if isinstance(member, dict)
    }
    def flatten(variants: object) -> object:
        for variant in get_args(variants):  # type: ignore[arg-type]
            if isinstance(variant, type) and issubclass(variant, BaseModel):
                yield variant
            else:
                yield from flatten(variant)

    expected = set()
    for variant in flatten(ActionUnion):
        assert isinstance(variant, type) and issubclass(variant, BaseModel)
        literal = get_args(variant.model_fields["type"].annotation)
        assert len(literal) == 1
        expected.add(literal[0])
    expected.discard("call_tool")
    assert names == expected


def test_strict_schema_keeps_actions_nullable() -> None:
    """The batch stays optional-in-effect: required, but null is accepted,
    so the single-action form needs no contract change."""
    schema = strict_decision_schema()
    assert isinstance(schema, dict)
    actions = schema["properties"]["actions"]
    assert isinstance(actions, dict)
    branches = actions["anyOf"]
    assert isinstance(branches, list)
    assert {"type": "null"} in branches


def test_schema_shaped_decisions_parse() -> None:
    """Single source of truth, two enforcers: what the schema allows,
    ``parse_decision`` accepts."""
    from computeruse.orchestrator.prompts import parse_decision

    single = (
        '{"thought": "t", "sub_goal": "s", '
        '"action": {"type": "wait", "duration_ms": 1, "reason": "r"}, '
        '"actions": null}'
    )
    batch = (
        '{"thought": "t", "sub_goal": "s", '
        '"action": {"type": "wait", "duration_ms": 1, "reason": "r"}, '
        '"actions": [{"type": "wait", "duration_ms": 1, "reason": "r"}]}'
    )
    assert parse_decision(single).action.type == "wait"
    assert parse_decision(batch).actions is not None


def _runner(
    seen_argv: list[list[str]],
    stdout: str,
    returncode: int = 0,
    stderr: str = "",
    expected_stdin: str = "decide this",
) -> object:
    def run(
        argv: list[str], stdin_text: str | None, timeout: float
    ) -> CliResult:
        seen_argv.append(argv)
        assert stdin_text == expected_stdin
        assert timeout == 300.0
        return CliResult(
            returncode=returncode, stdout=stdout, stderr=stderr, elapsed_s=1.5
        )

    return run


def test_model_call_argv_shape_and_stats() -> None:
    seen: list[list[str]] = []
    stats: list[ModelCallStats] = []
    model = codex_model(
        "gpt-5.6-codex",
        timeout_seconds=300.0,
        stats_sink=stats.append,
        enforce_decision_schema=True,
        runner=_runner(seen, _STREAM),  # type: ignore[arg-type]
    )
    assert json.loads(model("decide this"))["sub_goal"] == "s"
    argv = seen[0]
    assert argv[1:3] == ["exec", "--json"]
    assert "--ephemeral" in argv and "--skip-git-repo-check" in argv
    assert "--ignore-user-config" in argv
    assert "--output-schema" in argv
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert argv[argv.index("-m") + 1] == "gpt-5.6-codex"
    assert argv[-1] == "-"
    assert stats[0].total_tokens == 120
    assert stats[0].elapsed_s == 1.5


def test_model_without_id_omits_model_flag() -> None:
    seen: list[list[str]] = []
    model = codex_model(
        None,
        timeout_seconds=300.0,
        stats_sink=None,
        enforce_decision_schema=True,
        runner=_runner(seen, _STREAM),  # type: ignore[arg-type]
    )
    model("decide this")
    assert "-m" not in seen[0]


def test_auditor_binding_omits_the_schema_flag() -> None:
    """The completion auditor's verdict has its own shape: enforcing the
    decision schema there rejects every audit before it is read (measured
    live — the audit died with an empty parse error)."""
    seen: list[list[str]] = []
    model = codex_model(
        None,
        timeout_seconds=300.0,
        stats_sink=None,
        enforce_decision_schema=False,
        runner=_runner(seen, _STREAM, expected_stdin="audit this claim"),  # type: ignore[arg-type]
    )
    model("audit this claim")
    assert "--output-schema" not in seen[0]
    assert seen[0][-1] == "-"


def test_nonzero_exit_carries_the_cli_reason() -> None:
    model = codex_model(
        None,
        timeout_seconds=300.0,
        stats_sink=None,
        enforce_decision_schema=True,
        runner=_runner([], "", returncode=1, stderr="not logged in"),  # type: ignore[arg-type]
    )
    with pytest.raises(CliBridgeError, match="exited 1: not logged in"):
        model("decide this")


def test_stream_error_event_beats_a_bare_exit_code() -> None:
    """Refusals arrive as events on stdout: surface the event, not the code."""
    model = codex_model(
        None,
        timeout_seconds=300.0,
        stats_sink=None,
        enforce_decision_schema=True,
        runner=_runner(  # type: ignore[arg-type]
            [], '{"type": "error", "message": "quota exceeded"}\n', returncode=1
        ),
    )
    with pytest.raises(CliBridgeError, match="quota exceeded"):
        model("decide this")
