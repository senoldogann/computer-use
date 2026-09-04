"""Authority delegated in advance, in bounded amounts (Law 5.1).

The guard asks a human about every destructive action, and the approval queue
makes "asked" survive the human not being there. Neither of them delegates
anything: at Level 3 the agent still stops at every deletion, every send, every
purchase, and waits. That is the autonomy ceiling, and removing the guard is
not how you lift it — an agent that decides for itself what it may destroy is
not more autonomous, it is unsupervised, and nobody leaves an unsupervised
agent running.

People do delegate authority, though, and they do it the same way everywhere:
in advance, narrowly, for a while, up to a limit. "You can sign off expenses
under fifty pounds this quarter" is a real grant of real authority, and it is
safe *because* of its bounds, not despite them. A :class:`CapabilityGrant` is
that shape:

* a **verb** — which family of destructive action (delete, send, pay, ...),
  matched across languages so a grant written in English covers a Turkish
  button;
* a **scope** — which application, and which controls within it;
* a **count** — how many times, decremented for real;
* an **expiry** — after which it is simply not there.

What a grant may do is deliberately tiny: turn one ``CONFIRM`` into one
``ALLOW``, at Level 2 or 3, for a ``DESTRUCTIVE`` action that matches it. It
can never turn a ``BLOCK`` into anything, never apply at Level 0 or 1 (those
levels exist in order to ask, so a grant there would be a bypass rather than a
delegation), and never widen what the classifier considered risky in the first
place. Everything a grant does not cover still stops and asks.

Per Law 6 the matching is pure — :func:`authorize` reads grants and a clock and
returns a verdict — and :class:`GrantStore` is the connector that persists them
and, crucially, records that one was spent.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import re
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Final, Literal, cast

from pydantic import BaseModel, Field

from computeruse.orchestrator.schemas import Action, CallTool, ClipboardPaste, TypeText
from computeruse.security.autonomy import (
    DESTRUCTIVE_FAMILIES,
    SHELL_FAMILY,
    AutonomyLevel,
    Risk,
    decide_permission,
    intent_words,
    is_command_payload,
)
from computeruse.security.permissions import PermissionDecision
from computeruse.slug import ascii_slug

LOGGER: Final = logging.getLogger(__name__)

GrantOutcome = Literal["granted", "not_covered", "expired", "exhausted"]

#: Every verb a grant may name. Derived from the classifier's own families, so
#: a grant can only delegate authority over something the classifier actually
#: recognises as destructive — writing a grant for a verb the guard never fires
#: on would produce a permission that silently covers nothing.
GRANTABLE_VERBS: Final[frozenset[str]] = frozenset(DESTRUCTIVE_FAMILIES) | {
    SHELL_FAMILY
}

#: Scope value meaning "any application". Spelled out rather than left as an
#: empty string so a grant that covers the whole machine says so on its face
#: when a person reads the list.
ANY: Final[str] = "*"

#: How much of a grant's note survives into its id.
NOTE_SLUG_MAX_CHARS: Final[int] = 32


class CapabilityGrant(BaseModel):
    """Bounded authority to take one family of destructive action (pure data)."""

    grant_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]*$")
    #: Which family of destructive action, from :data:`GRANTABLE_VERBS`.
    verb: str
    #: Application the grant applies within, or :data:`ANY`. An app-wide grant
    #: is already broad; a machine-wide one has to be typed deliberately.
    app: str
    #: Glob matched against the accessibility title of the control being acted
    #: on (``"Move to Trash"``, ``"*.tmp"``, or :data:`ANY`). This is the part
    #: that makes a grant *narrow*: it names the buttons, not the intent.
    target_pattern: str
    max_invocations: int = Field(ge=1)
    used: int = Field(default=0, ge=0)
    expires_at: datetime
    created_at: datetime
    #: Why the human granted it, in their words. Carried because a list of
    #: standing permissions nobody can explain is one nobody will audit.
    note: str

    @property
    def remaining(self) -> int:
        """How many uses are left on this grant."""
        return max(0, self.max_invocations - self.used)


class GrantVerdict(BaseModel):
    """Whether a live grant covers a proposed action, and why (pure data)."""

    outcome: GrantOutcome
    grant_id: str | None
    reason: str

    @property
    def is_granted(self) -> bool:
        return self.outcome == "granted"


def new_grant(
    *,
    verb: str,
    app: str,
    target_pattern: str,
    max_invocations: int,
    expires_at: datetime,
    note: str,
    now: datetime,
) -> CapabilityGrant:
    """Mint a grant, refusing a verb the classifier does not know (pure).

    A grant naming a verb outside :data:`GRANTABLE_VERBS` would be a permission
    that never matches anything — worse than useless, because the person who
    wrote it believes they have delegated something.
    """
    if verb not in GRANTABLE_VERBS:
        raise ValueError(
            f"{verb!r} is not a grantable verb; the classifier recognises "
            f"{sorted(GRANTABLE_VERBS)}"
        )
    stamp = now.strftime("%Y%m%dt%H%M%S")
    label = ascii_slug(f"{verb} {note}", max_chars=NOTE_SLUG_MAX_CHARS)
    return CapabilityGrant(
        grant_id=f"{stamp}-{label}" if label else stamp,
        verb=verb,
        app=app,
        target_pattern=target_pattern,
        max_invocations=max_invocations,
        expires_at=expires_at,
        created_at=now,
        note=note,
    )


def _grant_argument_strings(value: object, *, depth: int) -> tuple[str, ...]:
    """Every string reachable inside a tool's arguments (pure).

    Mirrors the guard's walk so grant matching agrees with classification:
    a nested ``{"exec": {"argv": ["rm", "-rf"]}}`` is a shell payload in both
    places, not just in the guard.
    """
    if depth > 6:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, dict):
        return tuple(
            found
            for item in cast(dict[str, object], value).values()
            for found in _grant_argument_strings(item, depth=depth + 1)
        )
    if isinstance(value, (list, tuple)):
        return tuple(
            found
            for item in cast(Sequence[object], value)
            for found in _grant_argument_strings(item, depth=depth + 1)
        )
    return ()


def action_verbs(action: Action, *, sub_goal: str, target_label: str | None) -> frozenset[str]:
    """Which destructive families this action belongs to (pure).

    Read from the same three places the classifier reads: the control's own
    title, the model's stated sub-goal, and — for a tool call — the tool name.
    A grant matches on the *family*, so "Sil" on a Turkish screen and "Delete"
    on an English one are one thing to authorise.

    Typed payloads that look like commands join the shell family, mirroring the
    classifier's separate rule for them: ``rm -rf`` is not a verb a button
    shows, and delegating it is a different decision from delegating deletion.
    """
    subject = sub_goal.lower()
    if target_label:
        subject = f"{subject} {target_label}".lower()
    if isinstance(action, CallTool):
        subject = f"{subject} {action.tool}".lower()
    words = intent_words(subject)
    found = {
        family for family, markers in DESTRUCTIVE_FAMILIES.items() if words & markers
    }
    if isinstance(action, (TypeText, ClipboardPaste, CallTool)):
        # The payload is inspected for command words the same way the guard
        # inspects it; a grant for "shell" is what covers those.
        if isinstance(action, (TypeText, ClipboardPaste)):
            payload = action.text
        else:
            payload = " ".join(_grant_argument_strings(action.arguments, depth=0))
        if is_command_payload(payload):
            found.add(SHELL_FAMILY)
        # Deep verb scan mirrors the guard: a nested delete/send/pay verb
        # must be visible to grants too, or a live grant would miss what the
        # guard flagged (fail-closed, but confusing).
        if isinstance(action, CallTool):
            nested = " ".join(_grant_argument_strings(action.arguments, depth=0)).lower()
            nested_words = intent_words(nested)
            for family, markers in DESTRUCTIVE_FAMILIES.items():
                if nested_words & markers:
                    found.add(family)
    return frozenset(found)


def scope_matches(grant: CapabilityGrant, *, app: str, target_label: str | None) -> bool:
    """Does this grant's scope cover where the action is happening (pure)?

    The target glob is matched case-insensitively against the control's
    accessibility title. A grant whose pattern is not :data:`ANY` and an action
    with *no* readable target do not match: a grant that named specific buttons
    must not fire on a click whose button nobody could identify.
    """
    if grant.app != ANY and grant.app.casefold() != app.casefold():
        return False
    if grant.target_pattern == ANY:
        return True
    if target_label is None:
        return False
    return fnmatch.fnmatch(target_label.casefold(), grant.target_pattern.casefold())


def authorize(
    action: Action,
    *,
    sub_goal: str,
    target_label: str | None,
    app: str,
    grants: tuple[CapabilityGrant, ...],
    now: datetime,
) -> GrantVerdict:
    """Does a live grant cover this action (pure)?

    The reason is as much the point as the outcome. A person who wrote a grant
    and then watched the agent stop anyway needs to know whether it expired,
    ran out, or never covered this button — three different fixes, and "not
    authorised" tells them none of them.
    """
    verbs = action_verbs(action, sub_goal=sub_goal, target_label=target_label)
    if not verbs:
        return GrantVerdict(
            outcome="not_covered",
            grant_id=None,
            reason="no destructive verb was identified, so no grant applies",
        )
    candidates = [
        grant
        for grant in grants
        if grant.verb in verbs and scope_matches(grant, app=app, target_label=target_label)
    ]
    if not candidates:
        return GrantVerdict(
            outcome="not_covered",
            grant_id=None,
            reason=(
                f"no grant covers {sorted(verbs)} on {target_label or 'an unnamed control'!r} "
                f"in {app}"
            ),
        )
    live = [grant for grant in candidates if grant.expires_at > now]
    if not live:
        newest = max(candidates, key=lambda grant: grant.expires_at)
        return GrantVerdict(
            outcome="expired",
            grant_id=newest.grant_id,
            reason=(
                f"grant {newest.grant_id} covers this but expired at "
                f"{newest.expires_at.isoformat(timespec='seconds')}"
            ),
        )
    usable = [grant for grant in live if grant.remaining > 0]
    if not usable:
        spent = live[0]
        return GrantVerdict(
            outcome="exhausted",
            grant_id=spent.grant_id,
            reason=(
                f"grant {spent.grant_id} covers this but all "
                f"{spent.max_invocations} of its uses are spent"
            ),
        )
    # The narrowest live grant wins, so a broad standing permission never
    # silently absorbs a use that a specific one was written for — and a
    # person revoking the specific one gets the behaviour they expect.
    chosen = min(usable, key=_breadth)
    return GrantVerdict(
        outcome="granted",
        grant_id=chosen.grant_id,
        reason=(
            f"grant {chosen.grant_id} ({chosen.verb} in {chosen.app}, "
            f"{chosen.remaining} of {chosen.max_invocations} left): {chosen.note}"
        ),
    )


def _breadth(grant: CapabilityGrant) -> tuple[int, int]:
    """How wide a grant is, for picking the narrowest match (pure)."""
    return (
        1 if grant.app == ANY else 0,
        1 if grant.target_pattern == ANY else 0,
    )


def spent(grant: CapabilityGrant) -> CapabilityGrant:
    """One use consumed (pure)."""
    return grant.model_copy(update={"used": grant.used + 1})


def active_grants(
    grants: tuple[CapabilityGrant, ...], now: datetime
) -> tuple[CapabilityGrant, ...]:
    """Grants that could still authorise something, soonest to expire (pure)."""
    live = [
        grant for grant in grants if grant.expires_at > now and grant.remaining > 0
    ]
    return tuple(sorted(live, key=lambda grant: grant.expires_at))


class GrantStore:
    """Capability grants on disk, one JSON file each (Law 6.1 connector)."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory

    def _safe_path(self, grant_id: str) -> Path:

        if not re.fullmatch(r"^[a-z0-9][a-z0-9._-]*$", grant_id):
            raise KeyError(f"invalid grant id {grant_id!r}")
        candidate = self._directory / f"{grant_id}.json"
        try:
            if candidate.resolve().parent != self._directory.resolve():
                raise KeyError(f"invalid grant id {grant_id!r}")
        except OSError as exc:
            raise KeyError(f"invalid grant id {grant_id!r}: {exc}") from exc
        return candidate

    @property
    def directory(self) -> Path:
        return self._directory

    def save(self, grant: CapabilityGrant) -> Path:
        self._directory.mkdir(parents=True, exist_ok=True)
        target = self._safe_path(grant.grant_id)
        target.write_text(grant.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return target

    def grants(self) -> tuple[CapabilityGrant, ...]:
        """Every grant on disk; unreadable files are skipped with a warning.

        A store that refused to list because one file was corrupt would hide
        every *other* standing permission — and an unreadable grant authorises
        nothing, so skipping it fails closed.
        """
        if not self._directory.is_dir():
            return ()
        found: list[CapabilityGrant] = []
        for path in sorted(self._directory.glob("*.json")):
            try:
                found.append(
                    CapabilityGrant.model_validate(
                        json.loads(path.read_text(encoding="utf-8"))
                    )
                )
            except (OSError, ValueError) as exc:
                LOGGER.warning("unreadable capability grant %s: %s", path, exc)
        return tuple(found)

    def consume(self, grant_id: str) -> CapabilityGrant:
        """Record that one use of a grant was spent, and persist it.

        Called *before* the action runs, not after. If the host crashes
        mid-action the grant is spent anyway, which is the fail-closed
        direction: a count that under-reports is a permission the user has
        already lost, while one that over-reports is authority they never gave.
        """
        path = self._safe_path(grant_id)
        if not path.is_file():
            raise KeyError(f"no capability grant {grant_id!r} in {self._directory}")
        grant = CapabilityGrant.model_validate(
            json.loads(path.read_text(encoding="utf-8"))
        )
        remaining = spent(grant)
        path.write_text(remaining.model_dump_json(indent=2) + "\n", encoding="utf-8")
        LOGGER.info(
            "capability grant %s used (%d of %d left)",
            grant_id,
            remaining.remaining,
            remaining.max_invocations,
        )
        return remaining

    def revoke(self, grant_id: str) -> None:
        """Delete a grant. Revoking one that is not there is an error.

        Silently succeeding would let a typo read as "revoked" while the real
        permission stayed live, which is the one mistake this operation must
        not make.
        """
        path = self._safe_path(grant_id)
        if not path.is_file():
            raise KeyError(f"no capability grant {grant_id!r} in {self._directory}")
        path.unlink()
        LOGGER.info("capability grant %s revoked", grant_id)


def decide_with_grant(
    level: AutonomyLevel, risk: Risk, verdict: GrantVerdict | None
) -> PermissionDecision:
    """The base policy, narrowed by a live capability grant (pure).

    Deliberately a *separate* function rather than a third argument to
    :func:`~computeruse.security.autonomy.decide_permission`. That one is the
    constitution's level/risk table and should stay readable as exactly that;
    this one is the single place where delegated authority can change an
    answer, so the whole of what a grant can do fits in one screen:

    * ``BLOCK`` and ``ALLOW`` pass through untouched. A grant is authority to
      do something the policy would have *asked* about — never permission to do
      something it forbade, and never a change to something already permitted.
    * Only :attr:`Risk.DESTRUCTIVE` is eligible. The routine markers (save,
      close, confirm) are not what anyone delegates in advance, and letting a
      grant cover them would make "delete" grants quietly broader than written.
    * Levels 0 and 1 are exempt. Observer never acts; Supervised exists in
      order to ask about every step, so honouring a grant there would not be
      delegation, it would be a bypass of the level the user selected.
    """
    base = decide_permission(level, risk)
    if base is not PermissionDecision.CONFIRM:
        return base
    if risk is not Risk.DESTRUCTIVE:
        return base
    if level in (AutonomyLevel.OBSERVER, AutonomyLevel.SUPERVISED):
        return base
    if verdict is not None and verdict.is_granted:
        return PermissionDecision.ALLOW
    return base
