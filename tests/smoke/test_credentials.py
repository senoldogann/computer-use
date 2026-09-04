"""The credential boundary: the agent never types into a password field.

Not a permission question, which is why it is not in the autonomy guard: no
level, no capability grant and no human approval makes it acceptable. The agent
has no business knowing the user's password, and a model improvising one into a
redacted box is a mistake with no recovery — the string is gone into a field
nobody can read back, possibly a different field than intended, possibly
submitted.

Nothing stopped it before this. ``SecureTextField`` appeared in the element
list the model reads, and a ``type_text`` aimed at it went straight through.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from computeruse.orchestrator.failures import (
    FailureKind,
    classify_failure,
    recovery_hint,
)
from computeruse.orchestrator.loop import (
    AxProbeResult,
    CredentialEntryRefused,
    OodaRunner,
    WorkingState,
)
from computeruse.orchestrator.schemas import (
    AgentTurn,
    ClipboardPaste,
    Finish,
    MouseClick,
    TypeText,
)
from computeruse.vision.ax import AXElement, asks_for_a_credential


def _element(role: str, children: tuple[AXElement, ...] = ()) -> AXElement:
    return AXElement(
        role=role,
        title="",
        value="",
        focused=False,
        x=0.0,
        y=0.0,
        width=10.0,
        height=10.0,
        children=children,
    )


# --- detection --------------------------------------------------------------


def test_a_password_box_anywhere_in_the_tree_is_found() -> None:
    """Buried, not focused, and still the answer is yes."""
    tree = _element(
        "Application",
        (
            _element("Window", (_element("Group", (_element("SecureTextField"),)),)),
        ),
    )
    assert asks_for_a_credential(tree)


def test_an_ordinary_form_is_not_a_credential_prompt() -> None:
    tree = _element(
        "Application",
        (_element("Window", (_element("TextField"), _element("Button"))),),
    )
    assert not asks_for_a_credential(tree)


# --- the gate ---------------------------------------------------------------


def _runner_on(screen_asks: bool, executed: list[str]) -> OodaRunner:
    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return AgentTurn(
                thought="fill it in",
                sub_goal="enter the password",
                action=TypeText(type="type_text", text="hunter2"),
            )
        return AgentTurn(
            thought="cannot continue",
            sub_goal="the person has to sign in",
            action=Finish(type="finish", status="failed", summary="needs a human"),
        )

    return OodaRunner(
        provider=provider,
        execute_physical=lambda action: executed.append(action.type),
        ax_probe=lambda: AxProbeResult(
            summaries=('SecureTextField "" at (10,10) 10x10',),
            asks_for_credential=screen_asks,
        ),
        max_steps=4,
    )


def test_typing_is_refused_while_a_password_field_is_on_screen() -> None:
    executed: list[str] = []
    state = _runner_on(True, executed).run(goal="sign in to the portal")

    assert "type_text" not in executed, "a keystroke reached a password screen"
    # The refusal is recoverable: the run ends by the model's own finish, not
    # by an exception escaping the loop.
    assert state.completed_steps


def test_typing_is_untouched_on_an_ordinary_screen() -> None:
    """The gate must not make the agent unable to type at all."""
    executed: list[str] = []
    _runner_on(False, executed).run(goal="fill in the form")
    assert "type_text" in executed


def test_pasting_is_refused_too() -> None:
    """``clipboard_paste`` reaches the same field by another route."""
    runner = OodaRunner(
        provider=lambda state: AgentTurn(
            thought="paste it",
            sub_goal="enter the password",
            action=ClipboardPaste(type="clipboard_paste", text="hunter2"),
        ),
        execute_physical=lambda _action: None,
        max_steps=1,
    )
    runner._observation = replace(runner._observation, asks_for_credential=True)
    with pytest.raises(CredentialEntryRefused):
        runner._refuse_credential_entry(
            ClipboardPaste(type="clipboard_paste", text="hunter2")
        )


def test_reading_and_clicking_a_sign_in_screen_stay_allowed() -> None:
    """Stopping those would make the agent unable to even report what it sees."""
    runner = OodaRunner(
        provider=lambda state: AgentTurn(
            thought="t", sub_goal="s", action=Finish(type="finish", status="success", summary="x")
        ),
        execute_physical=lambda _action: None,
        max_steps=1,
    )
    runner._observation = replace(runner._observation, asks_for_credential=True)
    # Neither raises.
    runner._refuse_credential_entry(MouseClick(type="mouse_click", x=1, y=1))
    runner._refuse_credential_entry(Finish(type="finish", status="failed", summary="x"))


# --- what the model is told -------------------------------------------------


def test_the_guidance_tells_the_model_to_stop_not_to_try_differently() -> None:
    """No retry, no alternate route and no approval makes this acceptable, so
    the hint must not read like the others, which all say "try another way"."""
    failure = classify_failure(
        CredentialEntryRefused("a password field is on screen"),
        TypeText(type="type_text", text="hunter2"),
    )
    assert failure.kind is FailureKind.CREDENTIALS
    hint = recovery_hint(failure, 1)
    assert "never type one" in hint
    assert "has to sign in" in hint
