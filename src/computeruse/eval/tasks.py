"""The self-evaluation battery: fixed offline tasks over real contracts.

"Ajan daha iyi oluyor mu?" has no answer today — the only signal is the
per-skill uses/wins counter: binary, coarse, blind. This battery is the
replacement instrument: twelve tasks in the four categories the agent has
actually bled on (grounding, permission, recovery, report), each exercising
the real classifier over a fixture rather than a mock of it.

Three load-bearing properties, all deliberate:

* **Offline and non-destructive.** No driver socket, no model, no host
  effect — every check runs against pure functions with fixture input, so
  the battery holds in CI on a machine with no display and no consent.
  Nothing here clicks, types, sends or deletes anything.
* **Pinned to field failures, not invented ones.** The dropdown prose, the
  Notes summary, the truncation note, the centre-aiming roundtrip — each is
  a regression of something that already broke a live run once, with the
  test that would have caught it.
* **Checks are pure; running them is not.** Each ``check`` takes nothing
  and returns a :class:`CheckOutcome`. The runner in ``runner.py`` executes
  them and contains their exceptions, so one rotten task fails itself
  instead of blinding the other eleven.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from computeruse.agent import AX_BLINDNESS_THRESHOLD, ax_left_us_blind
from computeruse.eval.score import ALL_CATEGORIES, EvalCategory, join_usage
from computeruse.memory.schemas import Episode
from computeruse.orchestrator.failures import (
    Failure,
    FailureKind,
    RecoveryAction,
    classify_failure,
    recovery_for,
    recovery_hint,
)
from computeruse.orchestrator.loop import CredentialEntryRefused
from computeruse.orchestrator.mission import (
    mission_blocked,
    mission_started,
    new_mission,
)
from computeruse.orchestrator.report import (
    UsageRecord,
    period_ending,
)
from computeruse.orchestrator.report import (
    summarize as summarize_report,
)
from computeruse.orchestrator.schemas import (
    Action,
    AgentTurn,
    ClipboardPaste,
    MouseClick,
    TypeText,
)
from computeruse.security.approvals import approval_request_for
from computeruse.security.autonomy import (
    AutonomyLevel,
    Risk,
    classify_risk,
    decide_permission,
)
from computeruse.security.grants import new_grant, spent
from computeruse.vision.ax import (
    AXElement,
    RecognizedLine,
    asks_for_a_credential,
    interactive_summaries,
    recognized_summaries,
)
from computeruse.vision.som import parse_ax_elements_to_marks

#: Which battery this is. Bumped when tasks are added or redefined, so a
#: stored record never silently means something its reader does not know.
BATTERY_VERSION: Final[str] = "1"

_NOW: Final[datetime] = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


class CheckOutcome(BaseModel):
    """What one battery check found (pure data)."""

    model_config = ConfigDict(frozen=True)

    passed: bool
    #: Fraction of the check's sub-assertions that held — the scorer's own
    #: partial scale (Engel 3), independent of EpisodeOutcome's binary.
    score: float = Field(ge=0.0, le=1.0)
    steps: int = Field(ge=0)
    detail: str = Field(min_length=1)


@dataclass(frozen=True)
class BatteryTask:
    """One battery item: what it proves and the pure check that proves it."""

    task_id: str
    category: EvalCategory
    title: str
    #: Why this task exists — the field failure it pins, in one or two
    #: lines. A task nobody can explain is a task nobody will keep green.
    rationale: str
    check: Callable[[], CheckOutcome]


def _outcome(held: int, total: int, detail: str) -> CheckOutcome:
    """Build an outcome from counted sub-assertions (pure)."""
    return CheckOutcome(
        passed=held == total,
        score=(held / total) if total else 0.0,
        steps=total,
        detail=detail,
    )


def _turn(sub_goal: str, action: Action) -> AgentTurn:
    """A minimal decision aimed at the permission guard (pure)."""
    return AgentTurn(thought="battery probe", sub_goal=sub_goal, action=action)


def _fixture_window() -> AXElement:
    """Two buttons at known rects — the grounding fixture (pure)."""
    return AXElement(
        role="Window",
        title="Demo",
        x=0.0,
        y=0.0,
        width=800.0,
        height=600.0,
        children=(
            AXElement(role="Button", title="Reload", x=232.0, y=68.0, width=44.0, height=24.0),
            AXElement(role="Button", title="Cancel", x=300.0, y=309.0, width=52.0, height=18.0),
        ),
    )


# --- grounding ---------------------------------------------------------------


def _check_mark_roundtrip() -> CheckOutcome:
    """Summaries parse back to the exact fixture rects (pure).

    ``element_summary`` reports centres and ``parse_ax_elements_to_marks``
    rebuilds boxes around them; a drift in either half silently moves every
    mark half an element off target, as it already did once (L8).
    """
    marks = parse_ax_elements_to_marks(interactive_summaries(_fixture_window()))
    by_role_title = {(m.role, m.label.split('"')[1]): m for m in marks if '"' in m.label}
    reload = by_role_title.get(("Button", "Reload"))
    cancel = by_role_title.get(("Button", "Cancel"))
    held = 0
    if reload is not None and (reload.rect.origin.x, reload.rect.origin.y) == (232.0, 68.0):
        held += 1
    if cancel is not None and (cancel.rect.origin.x, cancel.rect.origin.y) == (300.0, 309.0):
        held += 1
    return _outcome(held, 2, f"reload+cancel marks round-trip ({held}/2 rects exact)")


def _check_ocr_matches_ax_shape() -> CheckOutcome:
    """An OCR line lands on the identical box an AX element would (pure).

    The whole point of rendering recognised lines into the element summary
    shape: nothing downstream changes, so the equivalence itself is the
    contract worth pinning.
    """
    (summary,) = recognized_summaries(
        (RecognizedLine(text="Sign in", confidence=0.98, x=380.0, y=309.0, width=64.0, height=18.0),)
    )
    (mark,) = parse_ax_elements_to_marks((summary,))
    held = 0
    if summary == 'Text "Sign in" at (412,318) 64x18':
        held += 1
    if (mark.rect.origin.x, mark.rect.origin.y) == (380.0, 309.0) and mark.role == "Text":
        held += 1
    return _outcome(held, 2, f"OCR line renders and parses as an AX-shaped mark ({held}/2)")


def _check_blindness_gate() -> CheckOutcome:
    """The OCR fallback fires exactly when AX went blind (pure).

    Firing on a rich frame wastes a Vision pass and buries real elements
    under duplicate readings; missing an empty one leaves the model with no
    indexed target at all. The truncation note must not count as an
    element — it means the tree had *more* to say.
    """
    rich = tuple(f'Button "b{i}" at ({i},10) 20x10' for i in range(AX_BLINDNESS_THRESHOLD + 1))
    held = 0
    if not ax_left_us_blind(rich, threshold=AX_BLINDNESS_THRESHOLD):
        held += 1
    if ax_left_us_blind((), threshold=AX_BLINDNESS_THRESHOLD):
        held += 1
    truncated = ("(AX grounding truncated at 64 elements — page content may be missing)",)
    if ax_left_us_blind(truncated, threshold=AX_BLINDNESS_THRESHOLD):
        held += 1
    return _outcome(held, 3, f"blindness gate: rich passes, empty+truncated fall back ({held}/3)")


# --- permission --------------------------------------------------------------


def _check_destructive_parks() -> CheckOutcome:
    """A destructive click is CONFIRM even at full autonomy (pure).

    Level 3 still asks about deletions — that is the line between guarded
    autonomy and a leak, and the battery holds it from the guard side.
    """
    turn = _turn(
        "empty the trash",
        MouseClick(type="mouse_click", x=10, y=10),
    )
    risk = classify_risk(turn, target_label="Move to Trash")
    held = 0
    if risk is Risk.DESTRUCTIVE:
        held += 1
    if decide_permission(AutonomyLevel.FULL, risk).name == "CONFIRM":
        held += 1
    return _outcome(held, 2, f"trash click classifies {risk.name} and parks at L3 ({held}/2)")


def _check_dropdown_stays_benign() -> CheckOutcome:
    """Opening a drop-down is not destruction (pure).

    The false-positive that taught the lesson: ``intent_words`` splits
    "Drop-down", so a naive "drop" marker turned every menu in every app
    into a confirmation — and a guard that fires at everything trains its
    reader to approve without looking.
    """
    turn = _turn(
        "open the Drop-down menu",
        MouseClick(type="mouse_click", x=10, y=10),
    )
    risk = classify_risk(turn, target_label="Drop-down")
    return _outcome(
        1 if risk is not Risk.DESTRUCTIVE else 0,
        1,
        f"drop-down click classifies {risk.name}",
    )


_PROSE_ARTICLE: Final[str] = (
    "Güncel Yapay Zekâ Haberleri Özeti\n\n• OpenAI otomatik kapatma "
    "(automated shutdown) yetenekleri geliştiriyor. "
    + "Ayrıntılar sürüyor. " * 40
)


def _check_prose_paste_stays_benign() -> CheckOutcome:
    """Prose about a command is not the command; a command still is (pure).

    Both directions matter: the exemption that saved the live Notes run
    ("write a summary" pasting an article mentioning shutdown) must hold,
    and the day it starts exempting ``rm -rf`` the guard is decorative.
    """
    prose = _turn(
        "özeti nota yapıştır",
        ClipboardPaste(type="clipboard_paste", text=_PROSE_ARTICLE),
    )
    command = _turn(
        "type it",
        ClipboardPaste(type="clipboard_paste", text="echo hi; rm -rf ~"),
    )
    held = 0
    if classify_risk(prose) is Risk.NONE:
        held += 1
    if classify_risk(command) is Risk.DESTRUCTIVE:
        held += 1
    return _outcome(held, 2, f"prose exempt, command caught ({held}/2)")


def _check_credential_detected() -> CheckOutcome:
    """A password box on screen reads as asking for a credential (pure).

    The signal the fail-closed typing boundary stands on: presence, not
    focus — focus can move between the snapshot and the keystroke.
    """
    login = AXElement(
        role="Window",
        title="Login",
        x=0.0,
        y=0.0,
        width=400.0,
        height=300.0,
        children=(
            AXElement(role="SecureTextField", title="Password", x=10.0, y=10.0, width=100.0, height=20.0),
        ),
    )
    plain = AXElement(role="Window", title="Reader", x=0.0, y=0.0, width=400.0, height=300.0)
    held = 0
    if asks_for_a_credential(login):
        held += 1
    if not asks_for_a_credential(plain):
        held += 1
    return _outcome(held, 2, f"secure field detected, plain window clear ({held}/2)")


# --- recovery ----------------------------------------------------------------


def _check_ladder_rungs() -> CheckOutcome:
    """Repeat counts land on retry → alternate → replan → abort (pure).

    The ladder is finite on purpose — one obstacle must never consume a
    whole run — so the exact rung boundaries are the contract.
    """
    expected = (
        (1, RecoveryAction.RETRY),
        (2, RecoveryAction.ALTERNATE),
        (3, RecoveryAction.REPLAN),
        (4, RecoveryAction.ABORT),
    )
    held = sum(1 for streak, rung in expected if recovery_for(streak) is rung)
    return _outcome(held, len(expected), f"ladder rungs 1/2/3/4 hold ({held}/{len(expected)})")


def _check_credential_failure_kind() -> CheckOutcome:
    """A refused credential classifies CREDENTIALS and says never-type (pure).

    Not a failure of skill but a boundary: no retry, no alternate route,
    no approval makes typing acceptable, and the hint must say exactly that.
    """
    failure = classify_failure(
        CredentialEntryRefused("a password field is on screen"),
        TypeText(type="type_text", text="s3cret", wpm=40),
    )
    held = 0
    if failure.kind is FailureKind.CREDENTIALS:
        held += 1
    if "never type" in recovery_hint(failure, 2):
        held += 1
    return _outcome(held, 2, f"credential refusal classifies and instructs ({held}/2)")


def _check_hint_escalates() -> CheckOutcome:
    """Repeated failure insists on a new method, then ends the run (pure).

    A second identical failure that accepts "adjusted coordinates" burns
    the step budget relanding the same miss; the fourth must stop spending
    a physical host on it.
    """
    failure = Failure(
        kind=FailureKind.COORDINATE,
        message="CoordinateOutOfBoundsError: x=9999",
        action_type="mouse_click",
        target="(312,6)",
    )
    held = 0
    if "Do NOT repeat" in recovery_hint(failure, 2):
        held += 1
    if "unrecoverable" in recovery_hint(failure, 4):
        held += 1
    return _outcome(held, 2, f"hint escalates alternate→abort ({held}/2)")


# --- report ------------------------------------------------------------------


def _report_fixtures() -> tuple[tuple[Episode, ...], tuple[UsageRecord, ...]]:
    """One success, one failure, and what the two runs cost (pure)."""
    episodes = (
        Episode(
            episode_id="safari.ok",
            app="Safari",
            description="open the front page",
            steps=(MouseClick(type="mouse_click", x=1, y=1),),
            outcome="success",
            signature="aa",
            run_id="run-1",
            recorded_at=_NOW,
        ),
        Episode(
            episode_id="safari.lost",
            app="Safari",
            description="find the export button",
            steps=(MouseClick(type="mouse_click", x=2, y=2),),
            outcome="failure",
            retrospective="the button never appeared",
            signature="bb",
            run_id="run-2",
            recorded_at=_NOW,
        ),
    )
    usage = (
        UsageRecord(
            run_id="run-1", goal="g", app="Safari", outcome="success",
            steps=4, total_tokens=100, cost_usd=0.01,
            elapsed_seconds=42.0, recorded_at=_NOW,
        ),
        UsageRecord(
            run_id="run-2", goal="g", app="Safari", outcome="failure",
            steps=9, total_tokens=200, cost_usd=0.02,
            elapsed_seconds=90.0, recorded_at=_NOW,
        ),
    )
    return episodes, usage


def _check_report_counts() -> CheckOutcome:
    """A known store summarizes to known counts (pure).

    The report is the precondition for delegation — nobody writes the
    second grant without having read what the first one did — so its
    counting is battery-grade, not eyeballed. Open items (a parked
    mission, an unanswered question, a spent grant) must surface even
    though they fall outside no window at all.
    """
    episodes, usage = _report_fixtures()
    parked_turn = _turn(
        "send the invoice",
        MouseClick(type="mouse_click", x=9, y=9),
    )
    parked = approval_request_for(
        parked_turn,
        goal="invoice the client",
        mission_id="m-1",
        target_label="Send",
        risk="destructive",
        now=_NOW,
    )
    opened = mission_started(
        new_mission(goal="invoice the client", app="Mail", plan=None, now=_NOW),
        _NOW,
    )
    blocked = mission_blocked(
        opened, plan=None, reason="waiting on Send", approval_id=parked.request_id, now=_NOW
    )
    grant = spent(
        new_grant(
            verb="send",
            app="Mail",
            target_pattern="Send",
            max_invocations=3,
            expires_at=_NOW + timedelta(hours=24),
            note="nightly invoices",
            now=_NOW,
        )
    )
    report = summarize_report(
        episodes=episodes,
        usage=usage,
        missions=(blocked,),
        approvals=(parked,),
        grants=(grant,),
        period=period_ending(_NOW, hours=24.0),
    )
    held = 0
    if report.succeeded == 1 and report.failed == 1:
        held += 1
    if report.total_tokens == 300:
        held += 1
    if report.total_cost_usd == 0.03:
        held += 1
    if len(report.blocked) == 1 and len(report.waiting) == 1:
        held += 1
    if len(report.grants_used) == 1:
        held += 1
    if not report.is_quiet:
        held += 1
    return _outcome(held, 6, f"known store: 1 ok / 1 lost / 300 tokens / 1 parked / 1 used ({held}/6)")


def _check_usage_join() -> CheckOutcome:
    """Episodes join to usage by run_id; legacy episodes join to none (pure).

    The Engel-1 contract at battery level: the join key must connect what
    happened to what it cost without breaking on history recorded before
    the key existed.
    """
    episodes, usage = _report_fixtures()
    legacy = Episode(
        episode_id="safari.old",
        app="Safari",
        description="an old run",
        steps=(),
        outcome="success",
        signature="cc",
        recorded_at=_NOW,
    )
    joined = join_usage(episodes + (legacy,), usage)
    held = 0
    if joined["safari.ok"] is not None and joined["safari.ok"].total_tokens == 100:
        held += 1
    if joined["safari.old"] is None:
        held += 1
    return _outcome(held, 2, f"run join hits, legacy episode joins to none ({held}/2)")


# --- the battery ---------------------------------------------------------------


TASK_BATTERY: Final[tuple[BatteryTask, ...]] = (
    BatteryTask(
        task_id="grounding.mark-roundtrip",
        category="grounding",
        title="AX summaries parse back to exact rects",
        rationale="Centre-reporting plus box-rebuild is the whole mark contract; drift in either half moves every click.",
        check=_check_mark_roundtrip,
    ),
    BatteryTask(
        task_id="grounding.ocr-shape",
        category="grounding",
        title="OCR lines render as AX-shaped marks",
        rationale="OCR needs no downstream changes only while the shape equivalence holds — pin the equivalence.",
        check=_check_ocr_matches_ax_shape,
    ),
    BatteryTask(
        task_id="grounding.blindness-gate",
        category="grounding",
        title="OCR fallback fires only when AX is blind",
        rationale="Firing on rich frames buries real elements; missing empty ones leaves the model targetless.",
        check=_check_blindness_gate,
    ),
    BatteryTask(
        task_id="permission.destructive-parks",
        category="permission",
        title="Destructive click parks even at full autonomy",
        rationale="Level 3 asking about deletions is the line between autonomy and a leak.",
        check=_check_destructive_parks,
    ),
    BatteryTask(
        task_id="permission.dropdown-benign",
        category="permission",
        title="Drop-down menus are not destruction",
        rationale="Pins the false-positive fix: a guard firing at everything trains approval without looking.",
        check=_check_dropdown_stays_benign,
    ),
    BatteryTask(
        task_id="permission.prose-benign",
        category="permission",
        title="Prose about commands stays benign, commands stay caught",
        rationale="Pins both directions of the Notes-summary fix — exemption and its limit.",
        check=_check_prose_paste_stays_benign,
    ),
    BatteryTask(
        task_id="permission.credential-detected",
        category="permission",
        title="Password boxes read as credential screens",
        rationale="The presence signal the fail-closed typing boundary stands on.",
        check=_check_credential_detected,
    ),
    BatteryTask(
        task_id="recovery.ladder-rungs",
        category="recovery",
        title="Repeat counts climb retry→alternate→replan→abort",
        rationale="A finite ladder is what stops one obstacle consuming a whole run; its rungs are the contract.",
        check=_check_ladder_rungs,
    ),
    BatteryTask(
        task_id="recovery.credential-kind",
        category="recovery",
        title="Refused credentials classify CREDENTIALS with never-type guidance",
        rationale="A boundary, not a skill failure: the hint must stop the model, not reroute it.",
        check=_check_credential_failure_kind,
    ),
    BatteryTask(
        task_id="recovery.hint-escalates",
        category="recovery",
        title="Hints demand a new method, then end the run",
        rationale="Pins the fix for hints that argued with their own diagnosis and retries that never changed method.",
        check=_check_hint_escalates,
    ),
    BatteryTask(
        task_id="report.counts",
        category="report",
        title="A known store summarizes to known counts",
        rationale="The report is the precondition for delegation; its counting is battery-grade.",
        check=_check_report_counts,
    ),
    BatteryTask(
        task_id="report.usage-join",
        category="report",
        title="Episodes join to usage by run_id",
        rationale="The Engel-1 join: what happened must meet what it cost, without breaking legacy history.",
        check=_check_usage_join,
    ),
)


def task_ids(tasks: tuple[BatteryTask, ...]) -> tuple[str, ...]:
    """The battery's task ids, in order (pure)."""
    return tuple(task.task_id for task in tasks)


def tasks_in(
    tasks: tuple[BatteryTask, ...], categories: tuple[str, ...]
) -> tuple[BatteryTask, ...]:
    """The battery filtered to named categories, in battery order (pure).

    Unknown names fail loudly: silently running a subset the caller did not
    ask for would score a different battery than the one reported.
    """
    unknown = [name for name in categories if name not in ALL_CATEGORIES]
    if unknown:
        raise ValueError(
            f"unknown eval categories: {', '.join(unknown)} "
            f"(choose from {', '.join(ALL_CATEGORIES)})"
        )
    wanted = set(categories)
    return tuple(task for task in tasks if task.category in wanted)
