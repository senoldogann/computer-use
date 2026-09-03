"""Law 3 two-stage RETRIEVE tests (OODA step 3).

The loop's RETRIEVE seam is wired: Stage 1 scans the summary index with the
goal, Stage 2 loads the top same-app match and mounts its full instructions
into the provider context — and the provider can swap it explicitly with a
``load_skill`` action. These tests pin the mount/refuse/degrade semantics and
the end-to-end agent wiring through the real driver.
"""

from __future__ import annotations

from collections.abc import Callable

from computeruse.agent import Agent, AgentConfig
from computeruse.orchestrator.loop import AxProbeResult, OodaRunner, WorkingState
from computeruse.orchestrator.prompts import decision_prompt
from computeruse.orchestrator.schemas import AgentTurn, Finish, LoadSkill, MouseClick
from computeruse.security.autonomy import AutonomyLevel
from computeruse.skills.registry import RelevanceMatch, SkillRegistry
from computeruse.skills.schemas import SkillDefinition, SkillSummary
from tests.smoke.conftest import SOCKET_PATH


def _summary(skill_id: str = "safari.export-report.abc", app: str = "Safari") -> SkillSummary:
    return SkillSummary(
        skill_id=skill_id,
        description="export a report via File > Export",
        app=app,
        tags=("export", "menu"),
    )


def _match(
    skill_id: str = "safari.export-report.abc", app: str = "Safari", score: int = 2
) -> RelevanceMatch:
    """A scored scan result; score >= 2 clears the mount gate (Law 3.2)."""
    return RelevanceMatch(summary=_summary(skill_id=skill_id, app=app), score=score)


def _definition(skill_id: str = "safari.export-report.abc") -> SkillDefinition:
    return SkillDefinition(
        skill_id=skill_id,
        description="export a report via File > Export",
        app="Safari",
        tags=("export", "menu"),
        steps=("open File menu", "choose Export", "pick CSV"),
        signature="sig-1",
    )


def _provider() -> Callable[[WorkingState], AgentTurn]:
    """Click, then finish — the distiller's minimum is not needed here."""
    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return AgentTurn(
                thought="first",
                sub_goal="click",
                action=MouseClick(type="mouse_click", x=1, y=1),
            )
        return AgentTurn(
            thought="done",
            sub_goal="done",
            action=Finish(type="finish", status="success", summary="ok"),
        )

    return provider


def _runner(**overrides: object) -> OodaRunner:
    """A runner wired with a scan + loader that resolve the fixture skill."""
    defaults: dict[str, object] = {
        "provider": _provider(),
        "execute_physical": lambda _action: None,
        "skill_scan": lambda _query: (_match(),),
        "skill_loader": _definition,
        "app": "Safari",
    }
    defaults.update(overrides)
    return OodaRunner(**defaults)


def test_retrieve_mounts_same_app_skill() -> None:
    """Stage 2: the provider sees the mounted skill from its very first turn."""
    seen: list[SkillDefinition | None] = []

    def provider(state: WorkingState) -> AgentTurn:
        seen.append(state.skill)
        if state.step_index == 0:
            return AgentTurn(
                thought="first",
                sub_goal="click",
                action=MouseClick(type="mouse_click", x=1, y=1),
            )
        return AgentTurn(
            thought="done",
            sub_goal="done",
            action=Finish(type="finish", status="success", summary="ok"),
        )

    runner = _runner(provider=provider)
    final = runner.run("export the menu")
    assert seen == [_definition(), _definition()]
    assert final.skill is not None and final.skill.skill_id == "safari.export-report.abc"


def test_retrieve_refuses_other_app_skill() -> None:
    """Skills are app-scoped (Law 3): a Chrome workflow must not mount in a\n    Safari run, even if its tags match the goal."""
    seen: list[SkillDefinition | None] = []

    def provider(state: WorkingState) -> AgentTurn:
        seen.append(state.skill)
        if state.step_index == 0:
            return AgentTurn(
                thought="first",
                sub_goal="click",
                action=MouseClick(type="mouse_click", x=1, y=1),
            )
        return AgentTurn(
            thought="done",
            sub_goal="done",
            action=Finish(type="finish", status="success", summary="ok"),
        )

    runner = _runner(
        provider=provider,
        skill_scan=lambda _query: (_match(skill_id="chrome.x", app="Chrome"),),
    )
    final = runner.run("export the menu")
    assert seen == [None, None]
    assert final.skill is None


def test_retrieve_looks_past_a_stronger_other_app_match() -> None:
    """A foreign skill at the top of the ranking must not hide a usable one.

    Skills are app-scoped, so the highest-scoring match is often unmountable;
    reading only the ranking's winner threw away the same-app skill one place
    below it. Measured on a store grown to 100 skills, that lost a usable skill
    in 7% of retrievals, and the loss grows with the store.
    """
    seen: list[SkillDefinition | None] = []

    def provider(state: WorkingState) -> AgentTurn:
        seen.append(state.skill)
        return AgentTurn(
            thought="done",
            sub_goal="done",
            action=Finish(type="finish", status="success", summary="ok"),
        )

    runner = _runner(
        provider=provider,
        skill_scan=lambda _query: (
            _match(skill_id="chrome.x", app="Chrome", score=5),
            _match(),
        ),
    )
    final = runner.run("export the menu")
    assert final.skill is not None
    assert final.skill.skill_id == "safari.export-report.abc"


def test_retrieve_stops_at_the_relevance_floor() -> None:
    """The scan must not reach past the floor to find its own application.

    Matches arrive sorted, so a same-app skill below the threshold is a weak
    coincidence — exactly what the floor exists to reject.
    """
    runner = _runner(
        skill_scan=lambda _query: (
            _match(skill_id="chrome.x", app="Chrome", score=5),
            _match(score=1),
        ),
    )
    assert runner.run("export the menu").skill is None


def test_retrieve_preserves_open_tabs_after_mount() -> None:
    """M3: mounting a skill must not drop the observed browser tabs.

    The RETRIEVE rebuild happens between OBSERVE and the provider decision, so
    a rebuild that silently resets ``open_tabs`` to ``()`` would blind the
    model to stray tabs exactly when it needs them.
    """
    seen_tabs: list[tuple[str, ...]] = []

    def provider(state: WorkingState) -> AgentTurn:
        seen_tabs.append(state.open_tabs)
        if state.step_index == 0:
            return AgentTurn(
                thought="first",
                sub_goal="click",
                action=MouseClick(type="mouse_click", x=1, y=1),
            )
        return AgentTurn(
            thought="done",
            sub_goal="done",
            action=Finish(type="finish", status="success", summary="ok"),
        )

    tabs = ("GitHub — computeruse", "Inbox")
    runner = _runner(
        provider=provider,
        ax_probe=lambda: AxProbeResult(
            summaries=('Button "Reload" at (232,68) 44x24',),
            open_tabs=tabs,
        ),
    )
    final = runner.run("export the menu")
    assert seen_tabs, "the provider must have decided at least once"
    # Every provider turn — including the skill-mount one — sees the tabs.
    assert all(turn_tabs == tabs for turn_tabs in seen_tabs)
    assert final.open_tabs == tabs


def test_explicit_load_skill_mounts_and_replaces() -> None:
    """The provider can pull any skill by id (Stage 2 on demand)."""
    seen: list[SkillDefinition | None] = []

    def provider(state: WorkingState) -> AgentTurn:
        seen.append(state.skill)
        if state.step_index == 0:
            return AgentTurn(
                thought="manual",
                sub_goal="mount manual flow",
                action=LoadSkill(type="load_skill", skill_id="manual.flow"),
            )
        if state.step_index == 1:
            return AgentTurn(
                thought="first",
                sub_goal="click",
                action=MouseClick(type="mouse_click", x=1, y=1),
            )
        return AgentTurn(
            thought="done",
            sub_goal="done",
            action=Finish(type="finish", status="success", summary="ok"),
        )

    loaded: list[str] = []

    def loader(skill_id: str) -> SkillDefinition:
        loaded.append(skill_id)
        return _definition(skill_id)

    runner = _runner(provider=provider, skill_loader=loader)
    final = runner.run("g")
    # RETRIEVE auto-mounted the fixture skill first; the explicit load_skill
    # then *replaces* it (Law 3.2 on-demand swap).
    assert loaded == ["safari.export-report.abc", "manual.flow"]
    # The swap is visible to the provider from the click turn onward.
    assert seen[1] is not None and seen[1].skill_id == "manual.flow"
    assert final.skill is not None and final.skill.skill_id == "manual.flow"


def test_explicit_load_failure_surfaces_in_last_error() -> None:
    """A missing skill id must surface as last_error, not crash the run."""

    def loader(_skill_id: str) -> SkillDefinition:
        raise KeyError("no skill with id 'nope.nope'")

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return AgentTurn(
                thought="manual",
                sub_goal="mount missing skill",
                action=LoadSkill(type="load_skill", skill_id="nope.nope"),
            )
        return _provider()(state)

    runner = _runner(provider=provider, skill_loader=loader)
    final = runner.run("g")
    assert final.last_error is not None and "KeyError" in final.last_error
    assert final.skill is None


def test_scan_failure_degrades_without_aborting() -> None:
    """A broken index must not kill the workflow (best-effort, Law 6.3)."""
    seen: list[SkillDefinition | None] = []

    def provider(state: WorkingState) -> AgentTurn:
        seen.append(state.skill)
        if state.step_index == 0:
            return AgentTurn(
                thought="first",
                sub_goal="click",
                action=MouseClick(type="mouse_click", x=1, y=1),
            )
        return AgentTurn(
            thought="done",
            sub_goal="done",
            action=Finish(type="finish", status="success", summary="ok"),
        )

    def scan(_query: str) -> tuple[RelevanceMatch, ...]:
        raise RuntimeError("index unavailable")

    runner = _runner(provider=provider, skill_scan=scan)
    final = runner.run("g")
    assert final.last_error is None, "a scan failure must not surface as a step error"
    assert seen == [None, None]


def test_no_retrieval_wiring_is_backwards_compatible() -> None:
    seen: list[SkillDefinition | None] = []
    runner = OodaRunner(
        provider=_provider_with_seen(seen),
        execute_physical=lambda _action: None,
    )
    final = runner.run("g")
    assert seen == [None, None]
    assert final.skill is None


def _provider_with_seen(seen: list[SkillDefinition | None]) -> Callable[[WorkingState], AgentTurn]:
    def provider(state: WorkingState) -> AgentTurn:
        seen.append(state.skill)
        if state.step_index == 0:
            return AgentTurn(
                thought="first",
                sub_goal="click",
                action=MouseClick(type="mouse_click", x=1, y=1),
            )
        return AgentTurn(
            thought="done",
            sub_goal="done",
            action=Finish(type="finish", status="success", summary="ok"),
        )

    return provider


def test_decision_prompt_renders_mounted_skill() -> None:
    state = WorkingState(goal="g", skill=_definition())
    prompt = decision_prompt(state, app="Safari")
    assert "Mounted skill: safari.export-report.abc — export a report via File > Export" in prompt
    assert "1. open File menu" in prompt
    assert "3. pick CSV" in prompt
    bare = WorkingState(goal="g")
    assert "Mounted skill:" not in decision_prompt(bare, app="Safari")


def test_agent_auto_mounts_matching_skill(tmp_path) -> None:
    """End to end: a pre-existing skill matching the goal is mounted by RETRIEVE."""
    store_dir = tmp_path / "store"
    SkillRegistry(store_dir / "skills").save(_definition("safari.export.abc"))

    def provider(state: WorkingState) -> AgentTurn:
        if state.step_index == 0:
            return AgentTurn(
                thought="first",
                sub_goal="click",
                action=MouseClick(type="mouse_click", x=1, y=1),
            )
        if state.step_index == 1:
            return AgentTurn(
                thought="second",
                sub_goal="click",
                action=MouseClick(type="mouse_click", x=2, y=2),
            )
        return AgentTurn(
            thought="done",
            sub_goal="done",
            action=Finish(type="finish", status="success", summary="ok"),
        )

    config = AgentConfig(
        goal="export the menu in safari",
        app="Safari",
        provider=provider,
        socket_path=str(SOCKET_PATH),
        store_dir=store_dir,
        autonomy_level=AutonomyLevel.GUARDED,
        enable_visual_verification=False,
        max_steps=10,
    )
    result = Agent(config).run()
    assert result.skill is not None and result.skill.skill_id == "safari.export.abc"
    assert result.state.skill is result.skill
