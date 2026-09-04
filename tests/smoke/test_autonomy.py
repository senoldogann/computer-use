"""Tests for Law 5.1: the autonomy-level permission guard."""

from __future__ import annotations

from dataclasses import replace

import pytest

from computeruse.agent import guarded
from computeruse.orchestrator.loop import (
    EMPTY_OBSERVATION,
    AxProbeResult,
    Observation,
    OodaRunner,
    WorkingState,
    target_element_label,
)
from computeruse.orchestrator.schemas import (
    AgentTurn,
    CallTool,
    ClipboardPaste,
    Finish,
    LoadSkill,
    MouseClick,
    PressHotkey,
    Wait,
    WebFetch,
    WebSearch,
)
from computeruse.security.autonomy import (
    AutonomyLevel,
    PermissionConfirmationRequired,
    PermissionDecision,
    PermissionDeniedError,
    Risk,
    classify_risk,
    decide_permission,
)


def _turn(**action: object) -> AgentTurn:
    action_fields = {"type": "mouse_click", "x": 10, "y": 10}
    action_fields.update(action)
    return AgentTurn.model_validate(
        {"thought": "do it", "sub_goal": action_fields.get("sub", "click"), "action": action_fields}
    )


def test_destructive_risk_detected_in_typed_payload() -> None:
    turn = _turn(
        type="type_text",
        text="rm -rf /  # remove",
        wpm=40,
        sub="clean up files",
    )
    assert classify_risk(turn) is Risk.DESTRUCTIVE


def test_clipboard_command_is_destructive() -> None:
    turn = AgentTurn(
        thought="paste command",
        sub_goal="open terminal",
        action=ClipboardPaste(type="clipboard_paste", text="sudo rm -rf /"),
    )
    assert classify_risk(turn) is Risk.DESTRUCTIVE


def test_benign_click_is_none() -> None:
    assert classify_risk(_turn(sub="open editor")) is Risk.NONE


def test_routine_marker_is_routine() -> None:
    assert classify_risk(_turn(sub="confirm dialog", type="press_hotkey", key="enter")) is Risk.ROUTINE


def test_prose_that_mentions_a_command_is_not_one() -> None:
    """Writing *about* shutdown is not shutting anything down.

    Measured on a live run of "research the AI news and write me a summary in
    Notes": the agent produced a correct 1,575-character summary whose first
    bullet reported an "automated shutdown" capability, and pasting that
    article into a note classified as issuing a shutdown command. An unattended
    run at full autonomy stopped to ask permission, and died waiting.
    """
    article = (
        "Güncel Yapay Zekâ Haberleri Özeti\n3 Eylül 2026\n\n"
        "• OpenAI: şirket mektubuna göre yapay zekâ araçları için "
        "\u201cotomatik kapatma\u201d (automated shutdown) yetenekleri "
        "geliştiriyor. Şirket ayrıca sistemlerin kullandığı dijital araçları "
        "daha yakından izlemeyi planlıyor. " + "Ayrıntılar sürüyor. " * 40
    )
    turn = AgentTurn(
        thought="",
        sub_goal="özeti nota yapıştır",
        action=ClipboardPaste(type="clipboard_paste", text=article),
    )
    assert classify_risk(turn) is Risk.NONE


def test_a_command_stays_a_command_however_it_is_dressed() -> None:
    """The exemption is for prose, and only for prose."""
    def paste(text: str) -> AgentTurn:
        return AgentTurn(
            thought="",
            sub_goal="type it",
            action=ClipboardPaste(type="clipboard_paste", text=text),
        )

    # A command line, whichever half you read.
    assert classify_risk(paste("echo hi; rm -rf ~")) is Risk.DESTRUCTIVE
    # A command at the head of a long payload is still a command.
    assert classify_risk(paste("shutdown -h now\n" + "a" * 900)) is Risk.DESTRUCTIVE


def test_reading_the_web_is_not_a_dialog_button() -> None:
    """A fetch presses nothing, so its prose must not be scanned for buttons.

    The markers name UI controls the agent might press. Matching them against
    the model's narration stopped a live run dead: it asked to fetch a page
    "to confirm its title and comment count" and the word "confirm" made a
    network read look like someone pressing OK on a dialog. ``finish``,
    ``wait`` and ``load_skill`` were exempted for exactly this reason and the
    web tools were not added when they arrived.
    """
    fetch = AgentTurn(
        thought="read it",
        sub_goal="Read the page text to confirm its title and comment count",
        action=WebFetch(type="web_fetch", url="https://example.com/"),
    )
    search = AgentTurn(
        thought="look it up",
        sub_goal="Search to confirm the release date",
        action=WebSearch(type="web_search", query="release date"),
    )
    assert classify_risk(fetch) is Risk.NONE
    assert classify_risk(search) is Risk.NONE
    assert decide_permission(AutonomyLevel.GUARDED, classify_risk(fetch)) is (
        PermissionDecision.ALLOW
    )


def test_an_mcp_tool_is_still_someone_elses_program() -> None:
    """``call_tool`` runs code the operator did not write; it keeps its scrutiny."""
    turn = AgentTurn(
        thought="use it",
        sub_goal="delete the stale records",
        action=CallTool(type="call_tool", tool="db.exec", arguments={}),
    )
    assert classify_risk(turn) is Risk.DESTRUCTIVE


def test_level0_observer_blocks_everything() -> None:
    assert decide_permission(AutonomyLevel.OBSERVER, Risk.NONE) is PermissionDecision.BLOCK
    assert decide_permission(AutonomyLevel.OBSERVER, Risk.DESTRUCTIVE) is PermissionDecision.BLOCK


def test_level1_supervised_confirms_even_benign() -> None:
    assert decide_permission(AutonomyLevel.SUPERVISED, Risk.NONE) is PermissionDecision.CONFIRM


def test_level2_guarded_blocks_destructive() -> None:
    assert decide_permission(AutonomyLevel.GUARDED, Risk.DESTRUCTIVE) is PermissionDecision.CONFIRM
    assert decide_permission(AutonomyLevel.GUARDED, Risk.NONE) is PermissionDecision.ALLOW


def test_level2_guarded_confirms_routine_stateful_actions() -> None:
    """M2: save/close/confirm dialog actions pause for a sign-off in guarded
    mode — the ``_ROUTINE_MARKERS`` classification must be live policy, not
    dead code that falls through to ALLOW like ordinary navigation."""
    assert decide_permission(AutonomyLevel.GUARDED, Risk.ROUTINE) is PermissionDecision.CONFIRM


def test_level3_full_auto_runs_routine_actions() -> None:
    """M2: full autonomy still auto-runs routine-but-stateful actions; only
    destructive ones require confirmation (Law 5.3 safety boundary)."""
    assert decide_permission(AutonomyLevel.FULL, Risk.ROUTINE) is PermissionDecision.ALLOW


def test_level3_full_still_confirms_destructive_actions() -> None:
    # Full autonomy never bypasses the destructive-action safety boundary.
    assert decide_permission(AutonomyLevel.FULL, Risk.DESTRUCTIVE) is PermissionDecision.CONFIRM
    assert decide_permission(AutonomyLevel.FULL, Risk.NONE) is PermissionDecision.ALLOW


def test_ooda_runner_blocks_destructive_via_guard() -> None:
    destructive_exc: PermissionDeniedError | PermissionConfirmationRequired | None = None

    def provider(_state: WorkingState) -> AgentTurn:
        return _turn(type="type_text", text="rm -rf ~", wpm=40, sub="cleanup")

    def execute(_action: object) -> None:
        raise AssertionError("destructive action must never reach the driver")

    from computeruse.security.autonomy import (
        AutonomyLevel,
        classify_risk,
        decide_permission,
    )

    def guard(turn: AgentTurn, _observation: Observation) -> PermissionDecision:
        return decide_permission(AutonomyLevel.GUARDED, classify_risk(turn))

    runner = OodaRunner(
        provider=provider, execute_physical=execute, guard=guard, max_steps=3
    )
    # GUARDED + destructive -> at least CONFIRM; we assert *no* physical call.
    try:
        runner.run(goal="cleanup")
    except (PermissionDeniedError, PermissionConfirmationRequired) as exc:
        destructive_exc = exc
    assert destructive_exc is not None, "guard did not stop the destructive action"


def test_ooda_runner_guard_off_when_not_provided() -> None:
    """Without a guard the runner behaves exactly as before (feature flag off)."""
    executed: list[str] = []

    def provider(state: WorkingState) -> AgentTurn:

        if state.step_index == 0:
            return _turn(type="mouse_click", x=1, y=2, sub="click")
        return AgentTurn.model_validate(
            {"thought": "", "sub_goal": "", "action": {"type": "finish", "status": "success", "summary": "ok"}}
        )

    def execute(action: object) -> None:
        executed.append(str(action))

    runner = OodaRunner(provider=provider, execute_physical=execute, max_steps=5)
    final = runner.run(goal="ok")
    assert final.step_index >= 1
    assert executed, "physical action should have run when guard is absent"

def test_internal_actions_never_need_permission() -> None:
    """finish/wait/load_skill touch nothing, so the gate has nothing to guard.

    Observed in a live run: a completed task ended with the sub-goal "Confirm
    the URL has been placed in the address bar", the word "confirm" matched a
    dialog-button marker, and the run stopped dead waiting for a human to
    approve the model's own English. The markers describe controls the agent
    might press — not the prose it narrates in.
    """
    for action in (
        Finish(type="finish", status="success", summary="done"),
        Wait(type="wait", duration_ms=10, reason="settle"),
        LoadSkill(type="load_skill", skill_id="safari.reload"),
    ):
        turn = AgentTurn(
            thought="",
            # Deliberately loaded with routine AND destructive markers.
            sub_goal="Confirm and save, then delete the temporary files",
            action=action,
        )
        assert classify_risk(turn) is Risk.NONE
        # Guarded (the default) and Full let these through untouched.
        for level in (AutonomyLevel.GUARDED, AutonomyLevel.FULL):
            assert decide_permission(level, classify_risk(turn)) is PermissionDecision.ALLOW
        # Observer and Supervised still gate them — but that is each level's own
        # rule ("never act" / "approve every step"), not a misread of prose.
        assert (
            decide_permission(AutonomyLevel.SUPERVISED, classify_risk(turn))
            is PermissionDecision.CONFIRM
        )


def test_physical_actions_keep_their_marker_classification() -> None:
    """Exempting internal actions must not weaken the real guard."""
    destructive = AgentTurn(
        thought="",
        sub_goal="delete the selected files",
        action=MouseClick(type="mouse_click", x=10, y=10),
    )
    assert classify_risk(destructive) is Risk.DESTRUCTIVE
    assert (
        decide_permission(AutonomyLevel.FULL, classify_risk(destructive))
        is PermissionDecision.CONFIRM
    )


# ── The guard reads the screen, not the model's narration ──────────────────


def _observation(
    *, raw: tuple[str, ...] = (), image: tuple[str, ...] = ()
) -> Observation:
    """An observation carrying only the two element lists the guard reads."""
    return replace(EMPTY_OBSERVATION, raw_ui_elements=raw, ui_elements=image)


def _click_turn(x: int, y: int, sub_goal: str) -> AgentTurn:
    return AgentTurn(
        thought="proceeding",
        sub_goal=sub_goal,
        action=MouseClick(type="mouse_click", x=x, y=y),
    )


def test_click_risk_comes_from_the_button_not_the_narration() -> None:
    """A blandly-described click on a destructive control is still destructive."""
    turn = _click_turn(100, 200, sub_goal="continue with the flow")
    assert classify_risk(turn) is Risk.NONE, "narration alone says nothing"
    observation = _observation(raw=('Button "Hesabı Sil" at (100,200) 80x24',))
    label = target_element_label(turn.action, observation)
    assert label == "Hesabı Sil"
    assert classify_risk(turn, target_label=label) is Risk.DESTRUCTIVE
    assert (
        guarded(AutonomyLevel.FULL, authorize=None)(turn, observation) is PermissionDecision.CONFIRM
    ), "even unattended autonomy asks before a destructive control"


def test_guard_looks_up_the_element_in_screen_points() -> None:
    """The lookup uses the logical-point list, never the image-space one.

    The coordinate gate has already converted the model's coordinates by the
    time the guard runs, so reading the ~3x smaller image-space list would
    answer about whichever element sits at the scaled-down position — a safety
    verdict about the wrong button.
    """
    turn = _click_turn(300, 300, sub_goal="pick the option")
    misleading = _observation(
        raw=('Button "Cancel" at (300,300) 80x24',),
        image=('Button "Delete everything" at (300,300) 80x24',),
    )
    assert target_element_label(turn.action, misleading) == "Cancel"
    assert classify_risk(turn, target_label="Cancel") is Risk.NONE


def test_unknown_target_leaves_the_verdict_to_the_narration() -> None:
    """A budget-capped element list means "no information", not "safe"."""
    turn = _click_turn(100, 200, sub_goal="delete the file")
    empty = _observation()
    assert target_element_label(turn.action, empty) is None
    assert classify_risk(turn, target_label=None) is Risk.DESTRUCTIVE


def test_element_value_never_drives_the_verdict() -> None:
    """A search box containing the word "delete" is not a destructive control."""
    turn = _click_turn(50, 50, sub_goal="focus the search box")
    observation = _observation(
        raw=('SearchField "Search" at (50,50) 200x24 value="delete my account"',)
    )
    assert target_element_label(turn.action, observation) == "Search"
    assert guarded(AutonomyLevel.FULL, authorize=None)(turn, observation) is PermissionDecision.ALLOW


def test_non_positional_actions_have_no_target_element() -> None:
    """Only a click or a drag has something under it to classify."""
    hotkey = AgentTurn(
        thought="t",
        sub_goal="submit",
        action=PressHotkey(type="press_hotkey", modifiers=[], key="return"),
    )
    observation = _observation(raw=('Button "Delete" at (0,0) 500x500',))
    assert target_element_label(hotkey.action, observation) is None


def test_runner_refuses_a_destructive_button_the_model_described_as_routine() -> None:
    """End to end: the AX probe feeds the guard, and the driver is never called."""
    executed: list[object] = []

    def provider(_state: WorkingState) -> AgentTurn:
        return _click_turn(100, 200, sub_goal="proceed to the next screen")

    def ax_probe() -> AxProbeResult:
        return AxProbeResult(summaries=('Button "Delete account" at (100,200) 80x24',))

    runner = OodaRunner(
        provider=provider,
        execute_physical=executed.append,
        guard=guarded(AutonomyLevel.GUARDED, authorize=None),
        ax_probe=ax_probe,
        max_steps=3,
    )
    with pytest.raises(PermissionConfirmationRequired):
        runner.run(goal="finish the signup flow")
    assert executed == [], "a destructive control must never reach the driver"


# --- SEC-01: an MCP call is read, not taken on trust ------------------------


def _tool_turn(sub_goal: str, tool: str, arguments: dict[str, object]) -> AgentTurn:
    return AgentTurn(
        thought="use it",
        sub_goal=sub_goal,
        action=CallTool(type="call_tool", tool=tool, arguments=arguments),
    )


def test_a_shell_command_in_tool_arguments_is_destructive() -> None:
    """The reported hole: the payload was never read at all.

    ``classify_risk`` looked only at the model's own ``sub_goal``, so a call
    that ran ``rm -rf /`` under an innocent description scored ``Risk.NONE``
    and executed unattended at full autonomy.
    """
    turn = _tool_turn("organize the folder", "bash", {"command": "rm -rf /"})
    assert classify_risk(turn) is Risk.DESTRUCTIVE
    assert decide_permission(AutonomyLevel.FULL, classify_risk(turn)) is (
        PermissionDecision.CONFIRM
    )


def test_a_command_nested_inside_tool_arguments_is_still_found() -> None:
    """Reading only the top level would miss the same call spelled as a list."""
    turn = _tool_turn("tidy up", "exec", {"exec": {"argv": ["rm", "-rf", "/"]}})
    assert classify_risk(turn) is Risk.DESTRUCTIVE


def test_a_destructive_tool_name_is_enough_on_its_own() -> None:
    """A tool name is a short identifier the server chose, not model prose."""
    turn = _tool_turn("clean the workspace", "delete_file", {"path": "/tmp/x"})
    assert classify_risk(turn) is Risk.DESTRUCTIVE


def test_a_tool_call_that_matches_nothing_floors_at_routine() -> None:
    """Someone else's program with side effects we cannot see is not benign.

    Guarded mode asks about it; full autonomy still runs it, so the MCP
    storefront keeps working unattended.
    """
    turn = _tool_turn("check the file", "files.read", {"path": "/tmp/notes.txt"})
    assert classify_risk(turn) is Risk.ROUTINE
    assert decide_permission(AutonomyLevel.FULL, Risk.ROUTINE) is PermissionDecision.ALLOW
    assert decide_permission(AutonomyLevel.GUARDED, Risk.ROUTINE) is (
        PermissionDecision.CONFIRM
    )


def test_a_document_body_that_mentions_deleting_is_not_a_deletion() -> None:
    """The false positive that once stopped a full-autonomy run dead.

    An MCP tool that takes a document body will be handed prose about deleting
    and shutting things down. Flagging that is the same category error as
    reading a note's text as a shell command.
    """
    body = (
        "Today the vendor announced an automated shutdown capability that can "
        "delete stale records without operator involvement. "
    ) * 4
    turn = _tool_turn("save the summary", "notes.create", {"body": body})
    assert classify_risk(turn) is not Risk.DESTRUCTIVE


# --- the guard has to stay quiet to stay useful -----------------------------


@pytest.mark.parametrize(
    ("sub_goal", "target_label"),
    [
        # `intent_words` splits on hyphens, so a "drop" marker made every
        # dropdown in every application destructive.
        ("open the country list", "Drop-down"),
        # The module's own history: a full-autonomy run of exactly this goal
        # died to a false positive on its own payload.
        ("write a summary in Notes", None),
        ("Notlara özeti yaz", None),
        # Describing something that already happened is not asking for it.
        ("verify the file was deleted", None),
        # Too ordinary to read off prose; still caught inside a SQL payload.
        ("update the spreadsheet", None),
        ("find and replace the text", "Replace All"),
    ],
)
def test_ordinary_work_does_not_ask_permission(
    sub_goal: str, target_label: str | None
) -> None:
    """A guard that fires on everything is a guard nobody reads.

    Fail-closed is the right instinct for one decision and the wrong one in
    aggregate: an approval queue full of dropdowns trains its reader to approve
    without looking, which costs more safety than it buys.
    """
    turn = AgentTurn(
        thought="t",
        sub_goal=sub_goal,
        action=MouseClick(type="mouse_click", x=1, y=1),
    )
    assert classify_risk(turn, target_label=target_label) is not Risk.DESTRUCTIVE


@pytest.mark.parametrize(
    ("sub_goal", "target_label"),
    [
        # The macOS delete verb is the Trash, not the word "delete".
        ("clean up the desktop", "Move to Trash"),
        ("tidy up", "Empty Trash"),
        ("continue with the flow", "Buy now"),
        # A gerund is how a model narrates an intention it is about to act on.
        ("deleting the old export", None),
    ],
)
def test_the_quieting_did_not_open_a_hole(
    sub_goal: str, target_label: str | None
) -> None:
    turn = AgentTurn(
        thought="t",
        sub_goal=sub_goal,
        action=MouseClick(type="mouse_click", x=1, y=1),
    )
    assert classify_risk(turn, target_label=target_label) is Risk.DESTRUCTIVE


@pytest.mark.parametrize(
    "payload",
    [
        {"sql": "TRUNCATE users"},
        {"sql": "UPDATE users SET admin = 1"},
        {"command": "rm -rf /"},
    ],
)
def test_a_statement_inside_a_payload_is_still_caught(
    payload: dict[str, object],
) -> None:
    """Verbs too ordinary for prose stay dangerous inside an argument.

    Nobody passes a paragraph about updating things as a tool's ``sql``
    parameter, so the argument set can be broader than the subject set without
    costing a single false positive.
    """
    turn = AgentTurn(
        thought="t",
        sub_goal="run it",
        action=CallTool(type="call_tool", tool="db", arguments=payload),
    )
    assert classify_risk(turn) is Risk.DESTRUCTIVE
