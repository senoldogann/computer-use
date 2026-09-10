"""The navigate action: one verified outcome instead of three blind keystrokes.

Focusing the address bar, pasting and submitting each verify cleanly while
the page never moves (measured live: a verified submit that never left
GitHub, then a full three-step retry of the identical sequence). Navigate
actuates that sequence and verifies the *outcome* — the window title must
move — folding a stationary page into the recovery ladder like any other
verification failure.
"""

from __future__ import annotations

import pytest

from computeruse.agent import guarded
from computeruse.orchestrator.loop import (
    OodaRunner,
    WorkingState,
    _looks_like_error_page,
    _navigation_host_core,
)
from computeruse.orchestrator.schemas import (
    AgentTurn,
    Finish,
    Navigate,
)
from computeruse.security.autonomy import AutonomyLevel, Risk, classify_risk
from computeruse.security.permissions import PermissionDecision
from computeruse.vision.focus import FocusedWindow


def _turn(action: object) -> AgentTurn:
    return AgentTurn.model_validate({"thought": "", "sub_goal": "", "action": action})


def _nav(url: str = "https://example.com/news") -> Navigate:
    return Navigate(type="navigate", url=url)


# --- contract: web only -------------------------------------------------------


@pytest.mark.parametrize("url", ["https://example.com/", "http://x.test/a?b=1"])
def test_http_and_https_validate(url: str) -> None:
    assert _nav(url).url == url


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        "data:text/html,<h1>x</h1>",
        "file:///etc/passwd",
        "ftp://files.example.com/x",
        "about:blank",
        "example.com/no-scheme",
        "",
    ],
)
def test_non_web_schemes_rejected_at_the_gate(url: str) -> None:
    """javascript: would run code in the browser's context; file:/data:
    smuggle local content past the permission model. None of them may
    validate — the loop must never see one."""
    with pytest.raises(ValueError):
        Navigate(type="navigate", url=url)


# --- guard: routine-but-stateful ----------------------------------------------


def test_navigate_is_routine_not_silent() -> None:
    """Opening a URL replaces page state with remote content the model
    chose: recoverable via Back, but stateful. Guarded/Supervised ask,
    Full runs — the tier those levels exist to enforce."""
    turn = AgentTurn(
        thought="t", sub_goal="open the news site", action=_nav().model_dump()
    )
    assert classify_risk(turn) is Risk.ROUTINE


def test_destructive_prose_still_wins_over_navigate() -> None:
    turn = AgentTurn(
        thought="t",
        sub_goal="navigate to the page to delete the file",
        action=_nav().model_dump(),
    )
    assert classify_risk(turn) is Risk.DESTRUCTIVE


def test_contract_advertises_navigate_and_bans_manual_url_bar() -> None:
    """The model must reach for navigate, not re-derive Cmd+L by hand."""
    from computeruse.orchestrator.prompts import ACTION_CONTRACT, parse_decision

    assert '"navigate"' in ACTION_CONTRACT
    assert "THE ONLY WAY to open a URL" in ACTION_CONTRACT
    turn = parse_decision(
        '{"thought": "t", "sub_goal": "s", '
        '"action": {"type": "navigate", "url": "https://example.com/"}, '
        '"actions": null}'
    )
    assert isinstance(turn.action, Navigate)


def test_strict_schema_covers_navigate() -> None:
    """The Codex/Claude schema enforcer must offer the new variant too —
    otherwise the transport silently withholds the fastest action."""
    from computeruse.providers.decision_schema import strict_decision_schema

    schema = strict_decision_schema()
    assert isinstance(schema, dict)
    variants = schema["properties"]["action"]["anyOf"]
    assert isinstance(variants, list)
    names = {
        member["properties"]["type"]["enum"][0]
        for member in variants
        if isinstance(member, dict)
    }
    assert "navigate" in names
    assert "call_tool" not in names


def test_guarded_level_asks_about_navigation() -> None:
    from computeruse.orchestrator.loop import EMPTY_OBSERVATION

    turn = AgentTurn(
        thought="t", sub_goal="open the news site", action=_nav().model_dump()
    )
    decision = guarded(AutonomyLevel.GUARDED, authorize=None)(
        turn, EMPTY_OBSERVATION
    )
    assert decision is PermissionDecision.CONFIRM


# --- pure helpers --------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "core"),
    [
        ("https://www.google.com/search?q=x", "google"),
        ("https://github.com/a/b", "github"),
        ("http://localhost:3000/app", "localhost"),
        ("https://omaleima.fi/contact", "omaleima"),
    ],
)
def test_host_core_extraction(url: str, core: str) -> None:
    assert _navigation_host_core(url) == core


@pytest.mark.parametrize(
    "title",
    ["This site can’t be reached", "ERR_CONNECTION_REFUSED", "Server not found"],
)
def test_error_page_titles_recognized(title: str) -> None:
    assert _looks_like_error_page(title) is True


@pytest.mark.parametrize(
    "title",
    ["latest AI news - Google'da Ara - Google Chrome", "AI News | Latest News", ""],
)
def test_ordinary_titles_not_flagged(title: str) -> None:
    assert _looks_like_error_page(title) is False


# --- loop: arrival is judged by the title --------------------------------------


def _window(title: str) -> FocusedWindow:
    return FocusedWindow(pid=1, app_name="Google Chrome", window_title=title)


def _runner(
    titles: list[str],
    executed: list[str],
    provider_calls: list[int],
) -> OodaRunner:
    """Fake loop: the window probe walks ``titles`` per call; every driver
    action is recorded, never actuated."""
    calls = {"n": 0}

    def window_probe() -> FocusedWindow:
        calls["n"] += 1
        return _window(titles[min(calls["n"] - 1, len(titles) - 1)])

    def provider(state: WorkingState) -> AgentTurn:
        provider_calls.append(1)
        if state.step_index == 0:
            return _turn(_nav("https://example.com/news").model_dump())
        return _turn(Finish(type="finish", status="success", summary="done"))

    def execute(action: object) -> None:
        executed.append(getattr(action, "type", "?"))

    return OodaRunner(
        provider=provider,
        execute_physical=execute,
        window_probe=window_probe,
        app="Google Chrome",
        max_steps=5,
    )


def test_successful_navigation_confirms_on_title_move() -> None:
    executed: list[str] = []
    calls: list[int] = []
    runner = _runner(["New Tab", "Example News - Google Chrome"], executed, calls)
    final = runner.run(goal="open the news")
    assert final.step_index == 2
    # One composite step, not three keystroke steps — and the sub-steps
    # rode the existing driver path in order.
    assert executed == ["press_hotkey", "clipboard_paste", "press_hotkey"]
    assert len(calls) == 2, "navigate + finish: two turns, not four"


def test_stationary_page_fails_into_the_ladder() -> None:
    """The exact live miss: verified keystrokes, unmoved page. The second
    turn must see a recovery hint about the failed navigation, not silence."""
    executed: list[str] = []
    calls: list[int] = []
    seen_errors: list[str | None] = []

    titles = ["GitHub", "GitHub", "GitHub", "GitHub"]

    def provider(state: WorkingState) -> AgentTurn:
        calls.append(1)
        seen_errors.append(state.last_error)
        if state.step_index >= 3:
            return _turn(Finish(type="finish", status="failed", summary="stuck"))
        return _turn(_nav("https://example.com/news").model_dump())

    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda action: executed.append(
            getattr(action, "type", "?")
        ),
        window_probe=lambda: _window(titles[min(len(calls), len(titles) - 1)]),
        app="Google Chrome",
        max_steps=10,
    )
    runner.run(goal="open the news")
    assert any(
        error is not None and "did not move" in error for error in seen_errors
    ), f"ladder never named the stationary page: {seen_errors}"


def test_error_page_is_not_confirmed_as_arrival() -> None:
    executed: list[str] = []
    seen_errors: list[str | None] = []
    navigated = {"done": False}

    def provider(state: WorkingState) -> AgentTurn:
        seen_errors.append(state.last_error)
        if state.step_index >= 1:
            return _turn(Finish(type="finish", status="failed", summary="dns?"))
        return _turn(_nav("https://no-such-host-xyz.test/").model_dump())

    def execute(action: object) -> None:
        executed.append(getattr(action, "type", "?"))
        if getattr(action, "type", "?") == "press_hotkey":
            navigated["done"] = True

    runner = OodaRunner(
        provider=provider,
        execute_physical=execute,
        # The error page appears once actuation happened, however many
        # settle polls read "New Tab" first — deterministic regardless of
        # poll counts.
        window_probe=lambda: _window("This site can’t be reached")
        if navigated["done"]
        else _window("New Tab"),
        app="Google Chrome",
        max_steps=5,
    )
    runner.run(goal="open the missing site")
    assert any(
        error is not None and "error page" in error for error in seen_errors
    ), f"error page not named: {seen_errors}"


def test_credential_in_url_refuses_before_touching() -> None:
    """userinfo pastes a secret where the browser remembers it (history,
    sync): same refusal as typing into a password field, before any act."""
    executed: list[str] = []

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return _turn(
                Navigate(
                    type="navigate", url="https://user:s3cret@example.com/"
                ).model_dump()
            )
        return _turn(Finish(type="finish", status="failed", summary="refused"))

    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda action: executed.append(
            getattr(action, "type", "?")
        ),
        window_probe=lambda: _window("New Tab"),
        app="Google Chrome",
        max_steps=5,
    )
    runner.run(goal="open the cred url")
    assert executed == [], "refused navigation must touch nothing"


def test_batch_ends_after_navigation() -> None:
    """A [navigate, click] batch must not click a coordinate that belonged
    to the departed page: the navigate step ends the batch, the click
    re-decides from the new screen next turn."""
    from computeruse.orchestrator.schemas import MouseClick

    executed: list[str] = []

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return AgentTurn.model_validate(
                {
                    "thought": "",
                    "sub_goal": "",
                    "action": _nav("https://example.com/news").model_dump(),
                    "actions": [
                        _nav("https://example.com/news").model_dump(),
                        MouseClick(type="mouse_click", x=10, y=10).model_dump(),
                    ],
                }
            )
        return _turn(Finish(type="finish", status="success", summary="done"))

    probe_calls = {"n": 0}

    def window_probe() -> FocusedWindow:
        probe_calls["n"] += 1
        return _window(
            "Example News - Google Chrome" if probe_calls["n"] > 1 else "New Tab"
        )

    runner = OodaRunner(
        provider=provider,
        execute_physical=lambda action: executed.append(
            getattr(action, "type", "?")
        ),
        window_probe=window_probe,
        app="Google Chrome",
        max_steps=5,
    )
    runner.run(goal="open then click")
    assert "mouse_click" not in executed, f"batch leaked past navigation: {executed}"
    assert executed == ["press_hotkey", "clipboard_paste", "press_hotkey"]


def test_navigate_replays_from_a_distilled_recipe() -> None:
    """Recipes store the URL (it IS the workflow meaning here) and rebuild
    it verbatim — coordinates were never stored, so there is nothing stale."""
    from computeruse.orchestrator.loop import rebuild_recipe_action
    from computeruse.skills.distiller import (
        Trajectory,
        build_recipe,
        distill,
        signature_of,
    )
    from computeruse.skills.schemas import summary_of

    traj = Trajectory(
        app="Google Chrome",
        description="open the news",
        steps=(
            _nav("https://Example.com/News"),
            _nav("https://Example.com/News/sports"),
        ),
        step_targets=("", ""),
    )
    recipe = build_recipe(traj)
    assert len(recipe) == 2
    assert recipe[0].params.get("url") == "https://Example.com/News"
    rebuilt = rebuild_recipe_action(recipe[0], ())
    assert isinstance(rebuilt, Navigate)
    assert rebuilt.url == "https://Example.com/News"
    distilled = distill(traj, set())
    assert distilled.definition is not None
    assert summary_of(distilled.definition).has_recipe is True
    # Same goal twice → same signature (dedup); different hosts → different.
    other = Trajectory(
        app="Google Chrome",
        description="open the docs",
        steps=(_nav("https://docs.example.com/"),),
        step_targets=("",),
    )
    assert signature_of(traj) != signature_of(other)
