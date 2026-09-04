"""Top-level agent composition — the product shell (Law 6: imperative).

Every tier of the constitution exists as a tested component; this module is
where they get bolted together into one runnable OODA loop:

* **ADR-1** — the Rust driver is reached only through :class:`ActuationClient`
  (separate process, Unix-socket JSON-RPC).
* **ADR-2 / Law 2** — ``sensor`` is the driver's screen capture; the ORIENT
  step verifies clicks visually.
* **Law 5.1** — the autonomy guard maps a configured level onto every decision
  before it becomes physical (``guarded``).
* **Law 5.2** — the kill-switch is polled before every step.
* **Law 3 + Law 4** — ``on_complete`` records an episode and distills a skill
  from every terminal run; the recorded episode's signature feeds the
  distiller's de-dup, so a re-run is never re-distilled.

The caller brings only the *provider* (an LLM or a scripted fake); the agent
brings everything else with sane defaults. ``run`` opens and closes the driver
connection itself, so a caller never has to manage client lifecycles.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

LOGGER: Final = logging.getLogger(__name__)

# AX grounding budget for the per-turn element summaries. Chrome's own chrome
# UI (toolbar, omnibox, tab strip) is traversed before the web page, so a
# small cap silently hides page-content elements (links, buttons) — the model
# then guesses coordinates from the screenshot. 64 balances context budget
# with covering the first actionable page elements.
AX_MAX_ELEMENTS: Final[int] = 64

from computeruse.mcp import DEFAULT_CONFIG_PATH, McpRegistry, load_server_configs
from computeruse.memory.episodic import EpisodicStore, episode_from_trace
from computeruse.memory.schemas import Episode, EpisodeOutcome
from computeruse.memory.semantic import SemanticStore
from computeruse.orchestrator.client import (
    AX_MAX_DEPTH,
    AX_MAX_NODES,
    OCR_MAX_LINES,
    OCR_MIN_CONFIDENCE,
    ActuationClient,
)
from computeruse.orchestrator.evidence import CompletionVerdict
from computeruse.orchestrator.loop import (
    SETTLE_INTERVAL_S,
    SETTLE_MAX_POLLS,
    AxProbeResult,
    Observation,
    OodaRunner,
    WorkingState,
    target_element_label,
)
from computeruse.orchestrator.planner import GoalPlan
from computeruse.orchestrator.schemas import Action, AgentTurn
from computeruse.orchestrator.trace import RunTracer, StepTrace, event_line, new_run_id
from computeruse.security.approvals import now_utc as grant_now
from computeruse.security.autonomy import (
    AutonomyLevel,
    PermissionDecision,
    Risk,
    classify_risk,
)
from computeruse.security.grants import (
    GrantStore,
    GrantVerdict,
    authorize,
    decide_with_grant,
)
from computeruse.security.killswitch import KillSwitch
from computeruse.skills.distiller import DistillResult, Trajectory, distill
from computeruse.skills.playbook import PlaybookRegistry
from computeruse.skills.registry import SkillRegistry, refined_route, skill_for_goal
from computeruse.skills.schemas import SkillDefinition, SkillSummary
from computeruse.vision import AXElement
from computeruse.vision.ax import (
    asks_for_a_credential,
    content_digest,
    interactive_summaries,
    open_tabs_from_tree,
    recognized_summaries,
)
from computeruse.vision.ax import focused_text_value as _focused_text_value_from_tree
from computeruse.vision.capture import ScreenCapture
from computeruse.vision.coordinates import Point, Rect, Size
from computeruse.vision.focus import FocusedWindow

#: How few accessibility elements count as "this window told us nothing".
#: Above it the AX tree is the primary source and OCR would only add noise and
#: a Vision pass to every turn; at or below it the model has essentially no
#: indexed target and is reduced to guessing coordinates off a screenshot.
#: A handful rather than zero, because a window that exposes only its own title
#: bar is as unusable as one that exposes nothing.
AX_BLINDNESS_THRESHOLD: Final[int] = 3


def ax_left_us_blind(summaries: tuple[str, ...], *, threshold: int) -> bool:
    """Should the OCR fallback run against this frame (pure)?

    ADR-2 makes the accessibility tree primary, so this answers "did that
    source fail", not "would more data be nice". A frame with real elements
    gets none of it: a text pass costs a few hundred milliseconds and would
    bury the tree's own controls under duplicate readings of their labels.

    The truncation note the probe appends when it exhausts its element budget
    is not an element, and counting it would let a *rich* frame look like an
    empty one — the exact inversion, since a truncated list means the tree had
    more to say rather than less.
    """
    grounded = [line for line in summaries if not line.startswith("(")]
    return len(grounded) <= threshold



@dataclass(frozen=True)
class AgentConfig:
    """Everything the agent needs to run one goal (caller-provided)."""

    goal: str
    provider: Callable[[WorkingState], AgentTurn]
    socket_path: str
    store_dir: Path
    # Target application; None means "discover the frontmost app from the
    # driver's focused-window probe at run start" (ADR-2 OBSERVE).
    app: str | None = None
    autonomy_level: AutonomyLevel = AutonomyLevel.FULL
    confirm_handler: Callable[[AgentTurn, str | None], bool] | None = None
    # Trust mode (--yes): the operator has explicitly asked for uninterrupted
    # autonomy. CONFIRM decisions are auto-approved and logged, BLOCK still
    # blocks, kill-switch/budget/verification/stuck-guard still run. This is
    # delegation in advance, not removal of the guard.
    auto_approve: bool = False
    enable_visual_verification: bool = True
    enable_vision: bool = True
    # Law 5.2: when True (default), the driver's global kill-hotkey poll is
    # composed into the effective kill-switch — Command+Shift+Escape on the
    # host reclaims control even with no other channel configured.
    enable_hotkey_killswitch: bool = True
    # OBSERVE precondition: bring the *configured* app to the front before
    # the loop. The caller sets this from --real; without it the agent acts
    # on whatever window is frontmost (typically the launching terminal),
    # which is almost never the app the goal means.
    activate_app_on_start: bool = False
    # True when the app name was *inferred from the goal* rather than named by
    # the user. An inference can name an app that is not installed; an
    # activation failure must then degrade to acting on the frontmost app
    # (with a loud warning) instead of aborting the run — autonomy means the
    # agent adapts, not that a guess becomes a hard stop.
    tolerate_activation_failure: bool = False
    kill_switch: KillSwitch | None = None
    # Goal-completion auditor: re-reads the current screen against the model's
    # own success claim before the run is allowed to end. None means the claim
    # is accepted on the model's word — acceptable for a scripted provider,
    # never for an LLM driving a real host.
    completion_check: Callable[[WorkingState, str], CompletionVerdict] | None = None
    # Post-action settle budget in seconds (polls x interval). Zero disables
    # the wait, which is only correct against a backend that never renders —
    # a real host needs the beat or verification reads a half-drawn frame.
    settle_max_polls: int = SETTLE_MAX_POLLS
    settle_interval_s: float = SETTLE_INTERVAL_S
    max_steps: int = 100
    connect_retries: int = 3
    # ADR-1 resilience: called when the driver socket stops answering, to give
    # whoever owns the driver process a chance to bring it back before the run
    # is declared handless. ``None`` for callers attached to a driver they did
    # not start — restarting someone else's process is not theirs to do.
    driver_recover: Callable[[], None] | None = None
    # Phase 3: when True, the goal is decomposed into sub-goals and the loop
    # advances through them (a ``finish`` marks the current sub-goal done).
    # Session checkpoints are written under ``store_dir/checkpoints`` so an
    # interrupted run can be resumed with the same plan.
    enable_planning: bool = False
    # Called with the plan after every sub-goal transition, so an owner outside
    # the agent (a mission store, a UI) can persist progress as it happens.
    # The agent deliberately does not know what a mission is: it reports where
    # it got to, and whoever asked for the work decides what that means.
    on_plan_progress: Callable[[GoalPlan], None] | None = None
    # Observability: when set, every step of the run is appended as one JSON
    # object to ``trace_dir/<run_id>/steps.jsonl``. None disables tracing
    # entirely — a run pays nothing for a diagnostic nobody asked for.
    trace_dir: Path | None = None
    # Which display the run observes and acts on. 0 is the main display; a
    # secondary display's id comes from the host. The capture carries that
    # display's global origin, so coordinates read off its screenshot convert
    # back into the global space the driver actuates in.
    display_id: int = 0
    # Act on elements directly instead of moving the cursor and clicking, when
    # the target exposes an accessibility press. Off by default: it changes how
    # every click reaches the host, and the ordinary path is the verified one.
    # On, the agent can work in an application the user has in the background
    # without stealing focus or the pointer.
    background_actuation: bool = False
    # Connect the MCP servers declared in ~/.computeruse/mcp.json and lend the
    # agent their tools. Off by default: these are other people's programs,
    # started as subprocesses, and turning them on should be a decision rather
    # than a surprise.
    enable_mcp: bool = False
    # Whether the OBSERVE screenshot is annotated with the AX element boxes
    # (Set-of-Marks). ``click_mark`` itself does not depend on this — it reads
    # the element list, which exists with vision off entirely.
    enable_set_of_marks: bool = True
    # Whether each traced step also saves the exact frame the model decided
    # from. Off by default: a thirty-step run is thirty PNGs, which is the
    # right trade only when someone is actually looking at them.
    trace_screenshots: bool = False
    # Run-ceiling check called once per step (wall clock / tokens / cost). The
    # counters live with whoever owns the model transport, so the agent takes
    # the check as a callable rather than owning a budget it cannot measure.
    budget_guard: Callable[[], None] | None = None


@dataclass(frozen=True)
class AgentResult:
    """What one run produced: the final state, trajectory, and its learnings."""

    state: WorkingState
    # The app the run actually targeted — the configured name, or the
    # frontmost app discovered when the config left it None.
    app: str
    trajectory: tuple[Action, ...]
    distilled: DistillResult | None
    episodes: tuple[Episode, ...]
    skills: tuple[SkillSummary, ...]
    # Law 4.2: the app knowledge strings the provider saw during the run.
    knowledge: tuple[str, ...]
    # Law 3.2: the skill mounted into the working context by RETRIEVE (if any).
    skill: SkillDefinition | None = None
    # This run's identity. Always present (a run is identifiable even when
    # nothing is being written), and the directory name under ``trace_dir``.
    run_id: str = ""


def guarded(
    level: AutonomyLevel,
    *,
    authorize: Callable[[AgentTurn, str | None], GrantVerdict] | None,
    auto_approve: bool = False,
) -> Callable[[AgentTurn, Observation], PermissionDecision]:
    """Build the VALIDATE-step guard for an autonomy level (pure).

    The guard is handed the decision *and* the observation it was made against,
    because the two sources disagree about what an action does and only one of
    them is trustworthy. The model's ``sub_goal`` is its own account ("continue
    with the flow"); the accessibility title under the pointer is the machine's
    ("Delete account"). Classifying the control the click will actually hit is
    what makes Law 5.1 a guard rather than a request for the model's opinion.

    ``authorize`` consults the user's standing capability grants, and is asked
    only about actions the classifier already called destructive — a grant can
    turn one of those confirmations into permission, and can do nothing else
    (see :func:`~computeruse.security.grants.decide_with_grant`). ``None`` runs
    the constitution's plain level/risk table, which is what a caller with no
    grant store should get: no grants means everything destructive asks.

    ``auto_approve`` is trust mode (--yes): CONFIRM becomes ALLOW, BLOCK still
    blocks. The safety floor (kill-switch, budget, verification, stuck-guard)
    keeps running; only the human prompt is skipped, and the caller logs it.
    """

    def guard(turn: AgentTurn, observation: Observation) -> PermissionDecision:
        label = target_element_label(turn.action, observation)
        risk = classify_risk(turn, target_label=label)
        verdict = (
            authorize(turn, label)
            if authorize is not None and risk is Risk.DESTRUCTIVE
            else None
        )
        base = decide_with_grant(level, risk, verdict)
        if auto_approve and base is PermissionDecision.CONFIRM:
            LOGGER.info(
                "trust mode: auto-approved %s for %r (risk=%s)",
                turn.action.type,
                turn.sub_goal,
                risk.value,
            )
            return PermissionDecision.ALLOW
        return base

    return guard


def _focused_element(root: AXElement) -> AXElement | None:
    """The element the application has focused, if any (pure).

    Depth-first: a focused leaf is what actually receives keystrokes, and a
    container may be marked focused on the way down to it.
    """
    for child in root.children:
        found = _focused_element(child)
        if found is not None:
            return found
    return root if root.focused and root.width > 0 and root.height > 0 else None


def _display_viewport(
    client: ActuationClient, display_id: int, window_pid: int | None = None
) -> Rect | None:
    """The observed screen's rect in global logical points (best effort).

    Follows whatever the sensor photographs: the display normally, the target
    window in background mode. If it did not, the filter would discard the very
    elements the model is meant to act on.

    Returns ``None`` when the screen cannot be captured — Screen Recording
    consent may be absent, and a missing viewport must widen perception back to
    "everything" rather than narrow it to nothing.
    """
    try:
        capture = (
            client.capture(display_id)
            if window_pid is None
            else client.capture(display_id, window_pid=window_pid)
        )
    except Exception as exc:  # noqa: BLE001 - perception degrades, never blocks
        LOGGER.debug("viewport probe failed; AX filtering stays off: %s", exc)
        return None
    scale = capture.scale or 1.0
    return Rect(
        Point(capture.origin_x, capture.origin_y),
        Size(capture.width / scale, capture.height / scale),
    )


def _remember_route(registry: SkillRegistry, fresh: SkillDefinition) -> None:
    """File a freshly distilled skill under one-skill-per-goal (Law 3.3).

    Signature de-duplication only catches workflows too small to vary. Measured
    on the real store, three of four repeated goals distilled a brand-new skill
    every run, because the agent had genuinely taken a different route — and
    each new skill started with an empty track record while the one it had just
    mounted kept the credit. Repetition, which is the only thing that can teach
    this store anything, was filling it with rivals instead.
    """
    existing = skill_for_goal(
        registry.index(), app=fresh.app, description=fresh.description
    )
    if existing is None:
        registry.save(fresh)
        return
    refined = refined_route(fresh, registry.load(existing.skill_id))
    if refined is not None:
        registry.save(refined)



class Agent:
    """Imperative shell composing every tier into one run.

    Constructed from an :class:`AgentConfig`; :meth:`run` opens the driver
    connection, runs the OODA loop with the standard wiring, persists the
    run's learnings, and returns an :class:`AgentResult`. One agent instance
    is reusable across goals (each ``run`` is self-contained).
    """

    def __init__(self, config: AgentConfig) -> None:
        if not config.goal.strip():
            raise ValueError("goal must be a non-empty string")
        self._config = config

    def run(self) -> AgentResult:
        episodes_store = EpisodicStore(self._config.store_dir / "episodes")
        skills_registry = SkillRegistry(self._config.store_dir / "skills")
        playbook_registry = PlaybookRegistry()
        semantic_store = SemanticStore(self._config.store_dir / "semantic")
        # Law 5.1 delegation: the user's standing capability grants. They apply
        # whenever any exist — a permission someone deliberately wrote, with an
        # expiry and a use count, should not also need a flag to be honoured,
        # and `--grants` is what makes them visible. With an empty store the
        # authorizer below never fires and every destructive action asks.
        grant_store = GrantStore(self._config.store_dir / "grants")
        distilled: DistillResult | None = None
        # Every run is identifiable, whether or not anything is written down:
        # the id is what ties a log line, a trace directory and a user's
        # bug report to the same run.
        run_id = new_run_id()
        # The UI panel reads stdout, so every step is announced there as a
        # structured line whether or not a trace file is being written. Without
        # it the window watching a run could only show the prose the log
        # happened to emit, while the plan, the reasoning and the verification
        # verdict stayed inside the process.
        def announce(record: StepTrace) -> None:
            print(event_line(record), flush=True)

        trace_sink: Callable[[StepTrace], None] = announce
        if self._config.trace_dir is not None:
            tracer = RunTracer(
                self._config.trace_dir,
                run_id=run_id,
                save_screenshots=self._config.trace_screenshots,
            )
            def announce_and_record(record: StepTrace) -> None:
                announce(record)
                tracer.record(record)

            trace_sink = announce_and_record
            LOGGER.info("run %s tracing to %s", run_id, tracer.directory)
        else:
            LOGGER.info("run %s starting", run_id)

        def on_complete(
            trajectory: Trajectory,
            outcome: EpisodeOutcome,
            retrospective: str | None,
            mounted_skill_id: str | None,
            forced_completion: bool,
        ) -> None:
            # A failed run is remembered but never distilled. Both halves
            # matter: a workflow that did not work must not become a skill the
            # next run is handed as a recipe, and a run that fought for twenty
            # steps before hitting a wall is exactly the trace worth keeping
            # (Law 4.1 failure retrospectives).
            #
            # A force-accepted finish counts as unverified, not as success:
            # the auditor rejected every claim and the stalemate guard let it
            # through to end the run, so neither the skill store, the skill's
            # win counter, nor semantic memory may learn from this flow.
            nonlocal distilled
            verified = outcome == "success" and not forced_completion
            # Reinforcement: a mounted skill is a claim about how to do this,
            # and the run just tested it. Recording the verdict is what turns
            # the store from a pile of recipes into one that gets better —
            # a skill that keeps failing is eventually withheld.
            if mounted_skill_id is not None:
                skills_registry.record_outcome(
                    mounted_skill_id, succeeded=verified
                )
            if not trajectory.steps:
                # Nothing ran, so there is nothing to remember or distil — the
                # skill's verdict above is the whole point of this call.
                return
            if verified:
                # Distill against known history FIRST, then remember — so the
                # fresh run is novel, and any future identical run is a
                # duplicate (Law 3.3 wired through Law 4 memory).
                distilled = distill(trajectory, episodes_store.known_signatures())
                if distilled.kind == "skill" and distilled.definition is not None:
                    _remember_route(skills_registry, distilled.definition)
            episodes_store.record(
                episode_from_trace(
                    app=trajectory.app,
                    description=trajectory.description,
                    steps=trajectory.steps,
                    step_descriptions=trajectory.step_descriptions,
                    outcome=outcome,
                    retrospective=retrospective,
                    # The join key to this run's UsageRecord: without it a
                    # score can say what happened but never what it cost.
                    run_id=run_id,
                    forced_completion=forced_completion,
                )
            )
            if verified:
                from computeruse.memory.semantic import extract_facts_from_run

                for fact in extract_facts_from_run(
                    app=trajectory.app,
                    steps=trajectory.steps,
                    step_descriptions=trajectory.step_descriptions,
                ):
                    semantic_store.upsert(fact)

        with ActuationClient(
            self._config.socket_path,
            connect_retries=self._config.connect_retries,
            recover=self._config.driver_recover,
        ) as client:
            # OBSERVE precondition: when the caller named a specific app,
            # bring it forward before any probe — otherwise the focused
            # window (and every click) would target whatever was frontmost
            # when the CLI launched. An explicit name that cannot be
            # activated is a setup error, not a degradable probe: clicking
            # blind on the wrong foreground app is worse than failing loudly
            # (Law 6.3).
            # Background actuation does not need the app in front, and
            # fronting it once at startup gives away the entire point: a live
            # run finished correctly but with the target pulled to the
            # foreground, exactly what the user asked to avoid.
            activate_on_start = (
                self._config.activate_app_on_start
                and not self._config.background_actuation
            )
            if activate_on_start and self._config.app is not None:
                try:
                    client.activate_app(self._config.app)
                except Exception as exc:
                    if self._config.tolerate_activation_failure:
                        # Goal-inferred app that LaunchServices cannot resolve
                        # (not installed, misspelled): proceed on whatever is
                        # frontmost and let the OODA loop adapt (Law 5.1
                        # autonomy — a guess must never hard-stop a run).
                        LOGGER.warning(
                            "goal-inferred app %r could not be activated (%s); "
                            "proceeding on the frontmost app",
                            self._config.app,
                            exc,
                        )
                    else:
                        raise RuntimeError(
                            f"cannot activate app {self._config.app!r}: {exc}"
                        ) from exc
            # ADR-2 OBSERVE: probe the frontmost app once at run start. It
            # names the app (when none was configured) and yields the pid that
            # feeds the per-turn AX grounding probe below — the agent knows
            # what it is looking at without being told.
            focused: FocusedWindow | None = None
            try:
                focused = client.focused_window()
            except Exception as exc:  # noqa: BLE001 - discovery is best-effort
                LOGGER.warning("focused-window probe failed: %s", exc)
            # Fail-fast for the visual sensor: when ORIENT verification is
            # requested but the driver cannot capture (e.g. Screen Recording
            # consent missing), the loop would otherwise grind every click
            # into a noisy retry against the same permanent condition. Probe
            # once before the loop so the user gets one clean, actionable
            # error instead of a page of repeated failures (Law 6.3).
            if self._config.enable_visual_verification or self._config.enable_vision:
                try:
                    client.capture(self._config.display_id)
                except Exception as exc:
                    if self._config.enable_visual_verification:
                        raise RuntimeError(
                            "visual verification is enabled but the screen sensor is "
                            f"unavailable: {exc}. Grant Screen Recording consent "
                            "(System Settings > Privacy & Security > Screen & System "
                            "Audio Recording), restart the driver, or rerun without "
                            "--verify."
                        ) from exc
                    LOGGER.warning(
                        "screen capture sensor is unavailable (%s). "
                        "Grant Screen Recording consent (System Settings > Privacy & Security "
                        "> Screen & System Audio Recording) for Vision.",
                        exc,
                    )
            app = self._config.app
            if app is None:
                app = (focused.app_name if focused is not None else "") or "unknown"
            # Law 4.2 RETRIEVE: the app's known preferences/patterns/shortcuts
            # are staged into the working context as compact strings, so the
            # provider makes decisions against what the system already knows
            # about the (possibly just-discovered) app.
            knowledge = tuple(
                f"[{entry.app}] {entry.key}: {entry.value}"
                for entry in semantic_store.search("", app=app)
            )
            # ADR-2 grounding: the AX tree of the frontmost app,
            # summarized into the compact lines the provider sees every turn —
            # so a decision's coordinates come from real elements, and the
            # pixel pipeline still verifies whatever the provider picks.
            # One focused-window read per OBSERVE cycle is shared by every
            # probe (window probe, AX grounding, focused-text): re-reading the
            # frontmost pid inside each probe would triple the per-step RPC
            # traffic for the same information (L9).
            cached_pid: int | None = None
            background_actuation = self._config.background_actuation
            target_window_warned = False
            # Which application perception should read. It starts as the one
            # the run was launched against and moves when the agent says it has
            # moved: a goal like "research this, then write it into Notes" is
            # two applications, and reading the first one forever means the
            # agent works blind in the second.
            working_app: str | None = self._config.app

            def window_probe() -> FocusedWindow:
                """Where the agent is, as one line the provider reads (§5).

                In background mode that is emphatically not the system's
                answer. The target is deliberately kept behind another window,
                so ``focused_window`` names whatever the *user* is doing and
                never moves when the agent's actions land. Measured on a real
                run: twenty-five consecutive steps shown the same foreign
                title while the agent drove Chrome from behind, opening a
                comments page and going back, opening it and going back, with
                no evidence in front of it that anything had happened.
                """
                nonlocal cached_pid
                nonlocal target_window_warned
                if background_actuation:
                    pid = target_pid()
                    if pid is not None:
                        try:
                            window = client.app_window(pid)
                        except Exception as exc:  # noqa: BLE001 - best-effort
                            # Falling back to the system-wide reading keeps the
                            # run alive, but it is a downgrade the operator has
                            # to know about: from here the agent is told about
                            # a window it is not acting on. Warned once, since
                            # the cause (an older driver, a refused consent)
                            # will not change mid-run.
                            if not target_window_warned:
                                target_window_warned = True
                                LOGGER.warning(
                                    "cannot read %s's own window (%s); falling back "
                                    "to the frontmost window, which in background "
                                    "mode is not the one being acted on",
                                    self._config.app,
                                    exc,
                                )
                        else:
                            if window.pid > 0:
                                cached_pid = window.pid
                            return window
                current = client.focused_window()
                if current.pid > 0:
                    cached_pid = current.pid
                return current

            def _current_pid() -> int | None:
                """The last pid perception resolved, re-read when unset.

                Frontmost in an ordinary run; in background mode the window
                probe caches the *target* instead, which is the pid every
                other probe in that mode wants anyway.
                """
                nonlocal cached_pid
                if cached_pid is not None and cached_pid > 0:
                    return cached_pid
                try:
                    current = client.focused_window()
                    if current.pid > 0:
                        cached_pid = current.pid
                except Exception:  # noqa: BLE001 - probe is best-effort fallback
                    if focused is not None and focused.pid > 0:
                        cached_pid = focused.pid
                if cached_pid is None or cached_pid <= 0:
                    return None
                return cached_pid

            # The observed display's rect in global logical points, resolved
            # once: display geometry does not change mid-run, and re-capturing
            # a Retina frame per probe is the most expensive thing the loop can
            # do. Everything outside it is unreachable — not a target and not
            # evidence — so perception spends its budget inside it.
            def _follow_app(app: str) -> None:
                """Point perception at the application the agent moved to."""
                nonlocal working_app
                working_app = app

            def target_pid() -> int | None:
                """The app this run works in, which in background mode is not
                the frontmost one.

                Resolved fresh rather than cached: an app can be launched or
                relaunched mid-run, and a stale pid would silently point
                perception at a process that no longer exists.
                """
                if not self._config.background_actuation or working_app is None:
                    return _current_pid()
                try:
                    return client.app_pid(working_app) or _current_pid()
                except Exception as exc:  # noqa: BLE001 - fall back to frontmost
                    LOGGER.debug("target pid lookup failed: %s", exc)
                    return _current_pid()

            def quiet_type(text: str) -> bool:
                """Put text into whichever element the target app has focused.

                Typing has no coordinates — it goes wherever focus is — so the
                quiet path needs to find that element itself. The AX tree marks
                it, and its centre is the point the driver writes to.
                """
                pid = target_pid()
                if pid is None:
                    return False
                try:
                    tree = client.ax_snapshot(
                        pid=pid, max_depth=AX_MAX_DEPTH, max_nodes=AX_MAX_NODES
                    )
                except Exception as exc:  # noqa: BLE001 - fall back to the keyboard
                    LOGGER.debug("focused-element lookup failed: %s", exc)
                    return False
                focused = _focused_element(tree)
                if focused is None:
                    return False
                return client.ax_set_value(
                    pid,
                    focused.x + focused.width / 2,
                    focused.y + focused.height / 2,
                    text,
                )

            def sense() -> ScreenCapture:
                """The frame the model reasons about and verification diffs.

                In background mode this is the *target window*, not the
                display. Photographing the display there would hand the model a
                picture of whatever the user has in front while it acts on
                something else entirely — the one blind spot that made the mode
                weak in practice, since AX told the truth and the picture did
                not. The frame carries the window's own origin, so coordinates
                convert off it with the machinery already in place.
                """
                pid = target_pid() if self._config.background_actuation else None
                # Passed only when it is asked for, so a sensor that predates
                # window capture — another client, an older driver — keeps its
                # exact previous call.
                if pid is None:
                    return client.capture(self._config.display_id)
                return client.capture(self._config.display_id, window_pid=pid)

            viewport = _display_viewport(
                client,
                self._config.display_id,
                target_pid() if self._config.background_actuation else None,
            )

            def quiet_press(point: Point) -> bool:
                """Press the element under a point inside the target app.

                Resolved against the app's own pid rather than system-wide: a
                system-wide hit test answers by z-order, so it returns whatever
                window is on top. Measured with Chrome covering Calculator,
                three system-wide presses all reported success, Chrome absorbed
                them, and the calculator never moved.
                """
                current_pid = target_pid()
                if current_pid is None:
                    return False
                return client.ax_press(current_pid, point.x, point.y)

            def ocr_fallback(summaries: tuple[str, ...]) -> tuple[str, ...]:
                """Read the screen with OCR when the AX tree gave us nothing.

                ADR-2's fallback, and it fires only where ADR-2 says it should:
                the accessibility tree is the primary source, so this stays out
                of the way whenever that source answered. A Vision pass on every
                turn would cost a few hundred milliseconds and bury the real
                elements under duplicate readings of their own labels.

                Best-effort by contract. An older driver returns nothing, a
                refused Screen Recording consent raises, and either way the run
                continues exactly as it did before — a fallback that could end
                a run would be worse than the blindness it is treating.
                """
                if not ax_left_us_blind(
                    summaries, threshold=AX_BLINDNESS_THRESHOLD
                ):
                    return ()
                try:
                    lines = client.recognize_text(
                        display_id=self._config.display_id,
                        window_pid=(
                            _current_pid()
                            if self._config.background_actuation
                            else None
                        ),
                        min_confidence=OCR_MIN_CONFIDENCE,
                        max_lines=OCR_MAX_LINES,
                    )
                except Exception as exc:  # noqa: BLE001 - a fallback may not raise
                    LOGGER.debug("OCR fallback unavailable: %s", exc)
                    return ()
                if not lines:
                    return ()
                LOGGER.info(
                    "AX exposed %d element(s); grounding on %d OCR line(s) instead",
                    len(summaries),
                    len(lines),
                )
                return recognized_summaries(lines)

            def ax_probe() -> AxProbeResult:
                current_pid = target_pid()
                if current_pid is None:
                    return AxProbeResult()
                tree = client.ax_snapshot(
                    pid=current_pid,
                    max_depth=AX_MAX_DEPTH,
                    max_nodes=AX_MAX_NODES,
                )
                summaries = interactive_summaries(
                    tree,
                    max_count=AX_MAX_ELEMENTS,
                    viewport=viewport,
                )
                if len(summaries) >= AX_MAX_ELEMENTS:
                    # The DFS budget was exhausted: page content deeper in the
                    # tree is absent from this list. Say so explicitly so the
                    # model grounds on the screenshot instead of assuming the
                    # listed elements are the whole actionable surface. The
                    # count is interpolated so the note cannot desync from the
                    # configured budget (L15).
                    truncation_note = (
                        f"(AX grounding truncated at {AX_MAX_ELEMENTS} elements — page content may "
                        "be missing; rely on the screenshot map for coordinates)"
                    )
                    summaries = summaries + (truncation_note,)
                summaries = summaries + ocr_fallback(summaries)
                return AxProbeResult(
                    summaries=summaries,
                    open_tabs=open_tabs_from_tree(tree),
                    content=content_digest(tree, viewport),
                    # Read from the whole tree, not from the summaries: the
                    # summary list is budgeted and viewport-culled, and a
                    # password box that fell off the end of it is still a
                    # password box on the screen.
                    asks_for_credential=asks_for_a_credential(tree),
                )

            def focused_text_value_probe() -> str | None:
                """Value of the focused text field via the driver's AX tree."""
                current_pid = _current_pid()
                if current_pid is None:
                    return None
                try:
                    return _focused_text_value_from_tree(
                        client.ax_snapshot(
                            pid=current_pid,
                            max_depth=AX_MAX_DEPTH,
                            max_nodes=AX_MAX_NODES,
                        )
                    )
                except Exception:  # noqa: BLE001 - probe is best-effort perception
                    return None

            # Law 5.2: compose the driver's global kill-hotkey channel into
            # whatever kill-switch the caller configured (e.g. the CLI's SIGINT
            # catcher), OR-ing them. A dead hotkey RPC degrades to False with a
            # warning — the other channels still protect the user.
            kill_switch = self._config.kill_switch
            if self._config.enable_hotkey_killswitch:

                def hotkey_state() -> bool:
                    try:
                        return client.hotkey_state()
                    except Exception as exc:  # noqa: BLE001 - a dead channel must not kill the run
                        LOGGER.warning("driver kill-hotkey state unavailable: %s", exc)
                        return False

                kill_switch = (kill_switch or KillSwitch(monitor=None)).with_signal_predicate(
                    hotkey_state
                )
            # Phase 3: hierarchical planning — decompose the goal into ordered
            # sub-goals up front, and checkpoint each sub-goal transition so a
            # killed run can resume from where it stopped (SessionCheckpoint).
            plan: GoalPlan | None = None
            on_sub_goal_complete_cb: Callable[[GoalPlan], None] | None = None
            if self._config.enable_planning:
                from computeruse.orchestrator.planner import (
                    SessionCheckpoint,
                    decompose_goal,
                )

                plan = decompose_goal(self._config.goal, app=app, knowledge=knowledge)
                checkpoint_dir = self._config.store_dir / "checkpoints"

                def _on_sub_goal_complete(current_plan: GoalPlan) -> None:
                    # Persist the plan's progress so a later run can resume
                    # from the in-progress sub-goal instead of restarting.
                    # ``runner`` is bound by the time this callback fires (it
                    # is only ever invoked from inside ``runner.run()``), so
                    # the executed count is always real (H2: an earlier
                    # ``"runner" in locals()`` guard could never see the
                    # closure variable and always recorded 0).
                    steps_count = len(runner.executed_trajectory)
                    SessionCheckpoint(
                        session_id=current_plan.plan_id,
                        plan=current_plan,
                        completed_steps_count=steps_count,
                    ).save(checkpoint_dir)
                    if self._config.on_plan_progress is not None:
                        self._config.on_plan_progress(current_plan)

                on_sub_goal_complete_cb = _on_sub_goal_complete

            # MCP servers are other people's programs started as subprocesses,
            # so connecting them is a decision the caller makes explicitly. One
            # that fails to start is logged and skipped inside the registry:
            # a broken optional integration must not stop the run.
            mcp: McpRegistry | None = None
            if self._config.enable_mcp:
                configs = load_server_configs()
                if configs:
                    mcp = McpRegistry(configs)
                    mcp.start()
                    LOGGER.info(
                        "MCP: %d tool(s) from %d server(s)", len(mcp.tools), len(configs)
                    )
                else:
                    LOGGER.info(
                        "MCP requested but no servers configured in %s", DEFAULT_CONFIG_PATH
                    )

            def grant_authorizer(
                turn: AgentTurn, target_label: str | None
            ) -> GrantVerdict:
                """Does a standing grant cover this action — and spend it if so.

                The matching is pure; consuming is not, and it happens *here*,
                before the action runs. A count decremented after the fact is a
                count that a crash mid-action silently refunds, which is the
                wrong direction to be wrong in: under-reporting costs the user a
                use they already had, over-reporting hands out authority they
                never gave.
                """
                verdict = authorize(
                    turn.action,
                    sub_goal=turn.sub_goal,
                    target_label=target_label,
                    app=app,
                    grants=grant_store.grants(),
                    now=grant_now(),
                )
                if verdict.is_granted and verdict.grant_id is not None:
                    try:
                        grant_store.consume(verdict.grant_id)
                    except (KeyError, OSError) as exc:
                        # A grant we cannot record as spent is a grant we must
                        # not honour: an unbounded permission is not the
                        # permission the user wrote (Law 6.3, fail closed).
                        LOGGER.warning(
                            "capability grant %s could not be consumed (%s); "
                            "treating the action as unauthorised",
                            verdict.grant_id,
                            exc,
                        )
                        return GrantVerdict(
                            outcome="not_covered",
                            grant_id=verdict.grant_id,
                            reason=f"the grant could not be recorded as used: {exc}",
                        )
                    LOGGER.info("action authorised by %s", verdict.reason)
                return verdict

            runner = OodaRunner(
                provider=self._config.provider,
                execute_physical=client.send,
                kill_switch=kill_switch,
                guard=guarded(
                    self._config.autonomy_level,
                    authorize=grant_authorizer,
                    auto_approve=self._config.auto_approve,
                ),
                confirm_handler=self._config.confirm_handler,
                # One capture source, two consumers: ORIENT verification and
                # the multimodal OBSERVE screenshot are the same frame stream.
                sensor=(
                    sense
                    if (self._config.enable_visual_verification or self._config.enable_vision)
                    else None
                ),
                verify_enabled=self._config.enable_visual_verification,
                vision_enabled=self._config.enable_vision,
                set_of_marks_enabled=self._config.enable_set_of_marks,
                window_probe=window_probe,
                frontmost_probe=client.focused_window,
                on_working_app=_follow_app,
                ax_probe=ax_probe,
                # ADR-2 semantic postcondition: the focused field's AXValue
                # lets type_text/clipboard_paste be verified against what the
                # user actually asked to insert (skipped when not determinable).
                focused_text_value=focused_text_value_probe,
                # Law 3 RETRIEVE: Stage 1 scans the (cached) summary index with
                # the goal (scored matches — the runner's relevance gate reads
                # ``RelevanceMatch.score``); Stage 2 loads the chosen skill's
                # full definition.
                skill_scan=lambda q: tuple(skills_registry.search(q)),
                skill_loader=skills_registry.load,
                playbook_scan=playbook_registry.best,
                app=app,
                # The focus guard only makes sense for a run pinned to a named
                # app: when the app was merely discovered from whatever was
                # frontmost, "drift away from it" is not a failure state.
                app_is_pinned=self._config.app is not None,
                on_complete=on_complete,
                quiet_press=quiet_press if self._config.background_actuation else None,
                mcp=mcp,
                quiet_type=quiet_type if self._config.background_actuation else None,
                completion_check=self._config.completion_check,
                knowledge=knowledge,
                settle_max_polls=self._config.settle_max_polls,
                settle_interval_s=self._config.settle_interval_s,
                max_steps=self._config.max_steps,
                plan=plan,
                on_sub_goal_complete=on_sub_goal_complete_cb,
                run_id=run_id,
                trace=trace_sink,
                budget_guard=self._config.budget_guard,
            )
            state = runner.run(self._config.goal)

        return AgentResult(
            state=state,
            app=app,
            trajectory=runner.executed_trajectory,
            distilled=distilled,
            episodes=tuple(episodes_store.episodes()),
            skills=tuple(skills_registry.index()),
            knowledge=knowledge,
            skill=state.skill,
            run_id=run_id,
        )
