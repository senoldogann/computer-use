"""Weak-model scaffolding tests (Law 2.1).

The scaffolding turns a raw ``Callable[[str], str]`` model into a
well-behaved OODA provider: prompt building from working state, strict
JSON/schema gates with corrective hints, and bounded re-prompting. These tests
cover the parse tolerance, the retry flow, and one full Agent run driven by a
plain text-emitting fake model.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from computeruse.agent import Agent, AgentConfig
from computeruse.orchestrator.loop import WorkingState
from computeruse.orchestrator.prompts import (
    InvalidDecisionError,
    _call_model,
    decision_prompt,
    parse_decision,
    scaffolded_provider,
    state_context,
)


def test_call_model_does_not_mask_internal_type_error() -> None:
    """L10: a TypeError raised *inside* the model must propagate, not be
    mistaken for "image_b64 unsupported" and silently retried without it."""
    calls: list[tuple[object, ...]] = []

    def flaky(prompt: str, image_b64: str | None = None) -> str:
        calls.append((prompt, image_b64))
        raise TypeError("internal boom")

    with pytest.raises(TypeError, match="internal boom"):
        _call_model(flaky, "p", "b64")
    assert len(calls) == 1, "the genuine TypeError must propagate, not re-invoke"


def test_call_model_legacy_prompt_only_transport() -> None:
    """L10: a prompt-only transport still works and never sees the image arg."""
    seen: list[object] = []

    def legacy(prompt: str) -> str:
        seen.append(prompt)
        return "{}"

    assert _call_model(legacy, "p", "b64") == "{}"
    assert _call_model(legacy, "p", None) == "{}"
    assert seen == ["p", "p"]
from computeruse.security.autonomy import AutonomyLevel
from tests.smoke.conftest import SOCKET_PATH

VALID = '{"thought": "t", "sub_goal": "s", "action": {"type": "mouse_click", "x": 10, "y": 20}}'


# --- parse_decision tolerance and gates --------------------------------------


def test_parse_clean_json() -> None:
    turn = parse_decision(VALID)
    assert turn.action.type == "mouse_click"
    assert turn.action.x == 10  # type: ignore[union-attr]


def test_parse_markdown_fenced_json() -> None:
    turn = parse_decision("Here is my decision:\n```json\n" + VALID + "\n```")
    assert turn.action.type == "mouse_click"


def test_parse_fenced_json_ignores_preceding_braces() -> None:
    turn = parse_decision("Plan {step 1}:\n```json\n" + VALID + "\n```")
    assert turn.action.type == "mouse_click"


def test_parse_prose_wrapped_json() -> None:
    turn = parse_decision("I will click. " + VALID + " That should do it.")
    assert turn.action.type == "mouse_click"


def test_parse_rejects_no_json_with_hint() -> None:
    with pytest.raises(InvalidDecisionError) as exc:
        parse_decision("I think I should click the button.")
    assert "exactly one JSON object" in exc.value.hint


def test_parse_rejects_malformed_json_with_hint() -> None:
    with pytest.raises(InvalidDecisionError) as exc:
        parse_decision('{"thought": "t", "sub_goal": "s", "action": {}}')
    # {} is structurally valid JSON but fails the schema gate.
    assert "action contract" in exc.value.hint


def test_parse_normalizes_aliases() -> None:
    # plain click alias (L13: folded in from the legacy tests/test_prompts.py)
    t0 = parse_decision('{"thought": "c", "sub_goal": "click", "action": {"type": "click", "x": 10, "y": 20}}')
    assert t0.action.type == "mouse_click"

    # double click alias
    t1 = parse_decision('{"thought": "d", "sub_goal": "open", "action": {"type": "double_click", "x": 50, "y": 60}}')
    assert t1.action.type == "mouse_click"
    assert t1.action.click_count == 2  # type: ignore[union-attr]

    # right click alias
    t2 = parse_decision('{"thought": "r", "sub_goal": "menu", "action": {"type": "right_click", "x": 50, "y": 60}}')
    assert t2.action.type == "mouse_click"
    assert t2.action.button == "right"  # type: ignore[union-attr]

    # paste alias
    t3 = parse_decision('{"thought": "p", "sub_goal": "paste", "action": {"type": "paste", "text": "hello"}}')
    assert t3.action.type == "clipboard_paste"
    assert t3.action.text == "hello"  # type: ignore[union-attr]

    # open_app alias


def test_parse_unescapes_html_in_pasted_url() -> None:
    """Regression: a model-escaped URL (`&amp;`) must reach the driver plain.

    Observed in the field: the agent pasted `watch?v=...&amp;t=1860s` and the
    literal `&amp;` broke the `t=` seek. The scaffold unescapes paste/type text
    deterministically.
    """
    turn = parse_decision(
        '{"thought": "t", "sub_goal": "seek", "action": {"type": "clipboard_paste", "text": "https://www.youtube.com/watch?v=abc&amp;t=1860s"}}'
    )
    assert turn.action.text == "https://www.youtube.com/watch?v=abc&t=1860s"  # type: ignore[union-attr]


def test_parse_batch_normalizes_aliases_and_unescapes() -> None:
    """Batch items ride the same alias + unescape path as the single action."""
    turn = parse_decision(
        '{"thought": "t", "sub_goal": "b", '
        '"action": {"type": "click", "x": 1, "y": 1}, '
        '"actions": [{"type": "click", "x": 1, "y": 1}, '
        '{"type": "paste", "text": "a&amp;b"}, '
        '{"type": "finish", "status": "success", "summary": "ok"}]}'
    )
    assert turn.actions is not None
    assert turn.actions[0].type == "mouse_click"
    assert turn.actions[1].type == "clipboard_paste"
    assert turn.actions[1].text == "a&b"
    assert turn.actions[2].type == "finish"
    t4 = parse_decision('{"thought": "o", "sub_goal": "launch", "action": {"type": "open_app", "app": "Notes"}}')
    assert t4.action.type == "activate_app"
    assert t4.action.app == "Notes"  # type: ignore[union-attr]

    # hotkey without explicit modifiers
    t5 = parse_decision('{"thought": "k", "sub_goal": "press enter", "action": {"type": "hotkey", "key": "return"}}')
    assert t5.action.type == "press_hotkey"
    assert t5.action.modifiers == []  # type: ignore[union-attr]

    # named-key normalization (L13: folded in from the legacy tests/test_prompts.py)
    tab = parse_decision('{"thought": "t", "sub_goal": "next", "action": {"type": "press_hotkey", "modifiers": [], "key": "Tab"}}')
    assert tab.action.key == "tab"  # type: ignore[union-attr]

    composite = parse_decision('{"thought": "k", "sub_goal": "copy", "action": {"type": "hotkey", "key": "Cmd+Shift+P"}}')
    assert composite.action.modifiers == ["command", "shift"]  # type: ignore[union-attr]
    assert composite.action.key == "p"  # type: ignore[union-attr]


def test_parse_rejects_unknown_action_type() -> None:
    with pytest.raises(InvalidDecisionError):
        parse_decision('{"thought": "t", "sub_goal": "s", "action": {"type": "teleport"}}')


# --- prompt building ---------------------------------------------------------


def test_decision_prompt_injects_state_and_contract() -> None:
    state = WorkingState(
        goal="export the report",
        completed_steps=("step_0:mouse_click",),
        last_error="VerificationFailedError: nothing changed",
        knowledge=("[Safari] shortcut.save: Cmd+S",),
    )
    prompt = decision_prompt(state, app="Safari")
    assert "Goal: export the report" in prompt
    assert "step_0:mouse_click" in prompt
    assert "Last error to recover from: VerificationFailedError" in prompt
    assert "[Safari] shortcut.save: Cmd+S" in prompt
    assert "mouse_click" in prompt  # the action contract is spelled out
    assert "OBSERVE" in prompt
    assert "SAFE BROWSER NAVIGATION" in prompt
    assert "VISUAL GROUNDING" in prompt


def test_decision_prompt_correction_appended() -> None:
    state = WorkingState(goal="x")
    plain = decision_prompt(state, app="Safari")
    corrected = decision_prompt(state, app="Safari", correction="your JSON was malformed")
    assert "your JSON was malformed" in corrected
    assert corrected != plain


def test_state_context_minimal_without_extras() -> None:
    text = state_context(WorkingState(goal="x"))
    assert "Goal: x" in text
    assert "Step 0 of 100" in text


# --- scaffolded provider retry flow ------------------------------------------


def test_scaffolding_retries_with_corrective_hint() -> None:
    """A model that emits garbage first, then valid JSON, is steered back."""
    calls: list[str] = []

    def fake_model(prompt: str) -> str:
        calls.append(prompt)
        if len(calls) == 1:
            return "I will click. (no json here)"
        return VALID

    provider = scaffolded_provider(fake_model, app="Safari", max_retries=2)
    turn = provider(WorkingState(goal="click"))
    assert turn.action.type == "mouse_click"
    assert len(calls) == 2
    # The corrective hint from the first failure reached the model.
    assert "exactly one JSON object" in calls[1]


def test_scaffolding_gives_up_after_retries() -> None:
    def always_garbage(_prompt: str) -> str:
        return "nope"

    provider = scaffolded_provider(always_garbage, app="Safari", max_retries=1)
    with pytest.raises(InvalidDecisionError):
        provider(WorkingState(goal="click"))


# --- end to end: a raw text model drives the agent ---------------------------


def _text_model_workflow() -> tuple[Callable[[str], str], Callable[[], int]]:
    """A fake raw-text model emitting a valid 2-click workflow as JSON text."""
    calls: list[int] = []

    def model(_prompt: str) -> str:
        step = len(calls)
        calls.append(step)
        if step == 0:
            return '{"thought": "first", "sub_goal": "click one", "action": {"type": "mouse_click", "x": 100, "y": 100}}'
        if step == 1:
            return '{"thought": "second", "sub_goal": "click two", "action": {"type": "mouse_click", "x": 200, "y": 200}}'
        return '{"thought": "done", "sub_goal": "done", "action": {"type": "finish", "status": "success", "summary": "ok"}}'

    def count() -> int:
        return len(calls)

    return model, count


def test_agent_runs_with_raw_text_model(tmp_path) -> None:
    """The full product path with a *text* model, scaffolded — Law 2.1 live."""
    model, _count = _text_model_workflow()
    config = AgentConfig(
        goal="open the menu",
        app="Safari",
        provider=scaffolded_provider(model, app="Safari"),
        socket_path=str(SOCKET_PATH),
        store_dir=tmp_path / "store",
        autonomy_level=AutonomyLevel.GUARDED,
        enable_visual_verification=False,
        max_steps=10,
    )
    result = Agent(config).run()
    assert [s.type for s in result.trajectory] == ["mouse_click", "mouse_click"]
    assert result.distilled is not None and result.distilled.kind == "skill"
    assert len(result.episodes) == 1


def test_cli_runs_with_scaffolded_model(tmp_path) -> None:
    """The CLI --model hook: a raw text model drives the whole stack.

    A tiny module on the path exports a turn-counting fake model that emits
    JSON text; the CLI wraps it with the scaffolding and runs the real driver.
    """
    from tests.smoke.conftest import DRIVER_BIN, REPO_ROOT

    if not DRIVER_BIN.exists():
        pytest.skip("actuation-driver not built; run `cargo build` in driver/")
    model_mod = tmp_path / "fake_model.py"
    model_mod.write_text(
        "_calls = 0\n"
        "def model(_prompt: str) -> str:\n"
        "    global _calls\n"
        "    _calls += 1\n"
        "    if _calls == 1:\n"
        "        return '{\"thought\": \"one\", \"sub_goal\": \"click one\", "
        "\"action\": {\"type\": \"mouse_click\", \"x\": 100, \"y\": 100}}'\n"
        "    if _calls == 2:\n"
        "        return '{\"thought\": \"two\", \"sub_goal\": \"click two\", "
        "\"action\": {\"type\": \"mouse_click\", \"x\": 200, \"y\": 200}}'\n"
        "    return '{\"thought\": \"done\", \"sub_goal\": \"done\", "
        "\"action\": {\"type\": \"finish\", \"status\": \"success\", "
        "\"summary\": \"ok\"}}'\n"
    )
    socket_path = Path("/tmp/computeruse-cli-model-test.sock")
    store_dir = tmp_path / "store"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join((str(REPO_ROOT / "src"), str(tmp_path)))
    run = subprocess.run(  # noqa: PLW1510 - manual returncode assert gives a richer failure message
        [
            sys.executable,
            "-m",
            "computeruse",
            "--goal",
            "open the menu",
            "--app",
            "Safari",
            "--model",
            "fake_model:model",
            "--driver",
            str(DRIVER_BIN),
            "--socket",
            str(socket_path),
            "--store",
            str(store_dir),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    assert run.returncode == 0, f"CLI failed:\n{run.stdout}\n{run.stderr}"
    assert "distill     : skill" in run.stdout
    assert len(list((store_dir / "episodes").glob("*.json"))) == 1


# --- Goal-completion auditor -------------------------------------------------


def test_completion_prompt_isolates_the_auditor_from_the_actor() -> None:
    """The checker sees the goal, the claim and the screen — nothing else.

    Sharing the actor's reasoning would let the story that produced a wrong
    claim also justify it. The whole value of a second read is that it is
    uncontaminated by the first one's beliefs, so the plan, the step history
    and the recovery diagnostics are deliberately withheld.
    """
    from computeruse.orchestrator.prompts import completion_prompt

    state = WorkingState(
        goal="sign out of the account",
        completed_steps=("step_0:mouse_click", "step_1:press_hotkey"),
        last_error="VerificationFailedError: nothing changed",
        active_window="Safari — Account settings",
        ui_elements=('Button "Sign out" at (10,20) 40x12',),
        screenshot_b64="AAAA",
    )
    prompt = completion_prompt(state, "I clicked sign out so we are done", app="Safari")
    assert "sign out of the account" in prompt
    assert "I clicked sign out so we are done" in prompt
    assert 'Button "Sign out"' in prompt
    # The actor's own trace must not leak into the check.
    assert "step_0:mouse_click" not in prompt
    assert "VerificationFailedError" not in prompt


def test_completion_parser_rejects_a_missing_verdict() -> None:
    """An auditor reply without a real boolean is a parse failure, not a guess.

    Defaulting here would quietly decide policy: the caller treats a broken
    auditor as "cannot judge" and accepts the finish, and that decision belongs
    in one place, not in a parser fallback.
    """
    from computeruse.orchestrator.prompts import parse_completion

    verdict = parse_completion('{"satisfied": true, "evidence": "the page shows Signed out"}')
    assert verdict.satisfied is True
    assert "Signed out" in verdict.evidence
    # Missing evidence still parses — the verdict is the load-bearing field.
    assert parse_completion('{"satisfied": false}').evidence
    for bad in ('{"satisfied": "yes"}', "{}", "not json at all"):
        with pytest.raises(InvalidDecisionError):
            parse_completion(bad)


def test_completion_auditor_reads_the_attached_screenshot() -> None:
    """The auditor is multimodal: it judges the screen, not the claim's prose."""
    from computeruse.orchestrator.prompts import completion_auditor

    seen: list[tuple[str, str | None]] = []

    def model(prompt: str, image_b64: str | None = None) -> str:
        seen.append((prompt, image_b64))
        return '{"satisfied": false, "evidence": "the page still shows the sign-in form"}'

    audit = completion_auditor(model, app="Safari")
    verdict = audit(
        WorkingState(goal="sign out", screenshot_b64="PNGDATA"), "signed out"
    )
    assert verdict.satisfied is False
    assert seen[0][1] == "PNGDATA"
