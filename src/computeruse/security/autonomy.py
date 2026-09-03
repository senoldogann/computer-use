"""Autonomy-level permission guard (Law 5.1).

The constitution defines four autonomy levels (0 Observer / 1 Supervised /
2 Guarded / 3 Full). This module turns that policy into a *pure* decision:
:func:`classify_risk` inspects a decision the model wants to take, and
:func:`decide_permission` maps a level + risk to one of
:class:`PermissionDecision`.

Why split the risk from the decision:
* Risk is about the *action itself* (is this destructive?), independent of how
  autonomous we're running right now.
* Permission is about the *policy* (given the current level, may this risk
  pass?). UI-stable, trivially testable, and shared by any orchestrator.

Per Law 6 these are pure functions over plain data — no OS influence and no
hidden state. The maps of which phrases count as destructive are the *policy*,
passed in explicitly (or defaulted) so callers can tailor per-app without
touching this file. The VALIDATE step in ``OodaRunner`` consumes a decision; a
non-:data:`~PermissionDecision.ALLOW` result raises a typed error there.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Final, cast

from computeruse.orchestrator.schemas import (
    AgentTurn,
    CallTool,
    ClipboardPaste,
    Finish,
    LoadSkill,
    PressHotkey,
    TypeText,
    Wait,
    WebFetch,
    WebSearch,
)
from computeruse.security.permissions import (
    PermissionConfirmationRequired,
    PermissionDecision,
    PermissionDeniedError,
)


class AutonomyLevel(Enum):
    """The four configurable autonomy levels from the constitution."""

    OBSERVER = 0   # Recommends actions and highlights regions, never touches.
    SUPERVISED = 1  # Proposes each action and waits for confirmation.
    GUARDED = 2    # Routine actions run; destructive ones ask first.
    FULL = 3       # Unattended, with safety boundaries + auto-fallback.


class Risk(Enum):
    """How destructive the proposed action is categorised to be."""

    NONE = "none"         # Ordinary navigation / benign.
    ROUTINE = "routine"   # Common but can change state (clicks in dialogs).
    DESTRUCTIVE = "destructive"  # Could delete, pay, install, or dispatch.


# Type-text commands that are clearly destructive only when they appear as
# their own clause, e.g. `rm -rf ~/` or `shutdown now`. Handled separately from
# whole-word intent markers so ordinary prose that merely *contains* the letters
# is never mis-flagged (see the ``confir"rm d"ialog`` false-positive that
# motivated the word-boundary matching below).
_TYPED_COMMANDS: frozenset[str] = frozenset({"rm", "dd", "mkfs", "shutdown", "reboot"})

# Whole-word *intent* markers. Matching is token-based, NOT substring based:
# the agent means "delete", "pay", "send" etc. — a verb, not random letters.
_DESTRUCTIVE_MARKERS: frozenset[str] = frozenset(
    {
        # English
        "delete",
        "remove",
        "uninstall",
        "pay",
        "checkout",
        "purchase",
        "send",
        "dispatch",
        "install",
        "sudo",
        "wipe",
        "erase",
        "format",
        "terminate",
        "overwrite",
        # Turkish
        "sil",
        "kaldır",
        "kaldir",
        "öde",
        "ode",
        "satınal",
        "satinal",
        "gönder",
        "gonder",
        "sıfırla",
        "sifirla",
        "formatla",
        "yoket",
        # French
        "supprimer",
        "supprime",
        "effacer",
        "efface",
        "payer",
        "paye",
        "acheter",
        "achete",
        "envoyer",
        "envoie",
        "desinstaller",
        "détruire",
        "detruire",
        # German (umlauts stripped duplicates included: users type both)
        "löschen",
        "lösche",
        "loeschen",
        "entfernen",
        "bezahlen",
        "zahlung",
        "kaufen",
        "senden",
        "installieren",
        "deinstallieren",
        "formatieren",
        "zerstören",
        "zerstoren",
        # Spanish
        "borrar",
        "eliminar",
        "pagar",
        "comprar",
        "enviar",
        "instalar",
        "desinstalar",
        "formatear",
        # Italian
        "cancellare",
        "eliminare",
        "pagare",
        "comprare",
        "inviare",
        "installare",
        "disinstallare",
        # Portuguese
        "apagar",
        "excluir",
        "destruir",
        # Dutch
        "verwijderen",
        "betalen",
        "kopen",
        "versturen",
        "installeren",
        "deïnstalleren",
        "vernietigen",
    }
)

# Phrases that are routine-but-stateful: worth a confirmation in guarded mode.
# Multilingual, mirroring the destructive markers: the user's locale must not
# determine whether the guard fires.
_ROUTINE_MARKERS: frozenset[str] = frozenset(
    {
        # English
        "ok",
        "confirm",
        "apply",
        "save",
        "close",
        # Turkish
        "kaydet",
        "uygula",
        "tamam",
        "kapat",
        # French
        "enregistrer",
        "sauvegarder",
        "fermer",
        "valider",
        # German
        "bestätigen",
        "anwenden",
        "speichern",
        "schließen",
        # Spanish
        "confirmar",
        "aplicar",
        "guardar",
        "cerrar",
        # Italian
        "confermare",
        "applicare",
        "salvare",
        "chiudere",
        # Portuguese
        "salvar",
        "fechar",
        # Dutch
        "bevestigen",
        "toepassen",
        "opslaan",
        "sluiten",
    }
)


@dataclass(frozen=True)
class AutonomyPolicy:
    """The safety policy governing classification (tunable, pure data)."""

    destructive_markers: frozenset[str] = _DESTRUCTIVE_MARKERS
    routine_markers: frozenset[str] = _ROUTINE_MARKERS
    typed_commands: frozenset[str] = _TYPED_COMMANDS

    def classify(self, turn: AgentTurn, target_label: str | None = None) -> Risk:
        """Classify a single model decision into a risk level (pure).

        Orchestrator-internal actions are always :attr:`Risk.NONE`. ``finish``,
        ``wait`` and ``load_skill`` never reach the physical layer — they cannot
        delete, pay, send or install anything — so the permission gate has
        nothing to stand between. Classifying them by their *prose* was a
        category error with a real cost: a model ending a run with the sub-goal
        "Confirm the URL has been placed in the address bar" matched the
        dialog-button marker "confirm", and a completed run stopped dead
        waiting for a human to approve the word "confirm" (observed in a live
        run). The markers describe UI controls the agent might press, not the
        English the model narrates in.

        Searching and fetching join that list for the same reason and were
        missing from it: they read, over the network, and press nothing. The
        omission cost a live run, which asked to fetch a page "to confirm its
        title and comment count" and stopped dead on the word "confirm" —
        exactly the failure the paragraph above describes, in the same place,
        two actions later. ``call_tool`` is deliberately *not* here: an MCP
        tool is someone else's program and can do anything a program can — and
        for a long time saying so was the whole of the defence. The tool name
        and its arguments were never read, so classification fell back to the
        model's own prose and ``CallTool(tool="bash", arguments={"command":
        "rm -rf /"})`` under the sub-goal "organize the folder" scored
        :attr:`Risk.NONE` and ran unattended. The call is now read the way a
        click is: the tool's *name* joins the intent words, its argument values
        are searched for commands and for destructive verbs, and a call that
        matches nothing at all still floors at :attr:`Risk.ROUTINE` rather than
        :attr:`Risk.NONE`, because "someone else's program" is the definition
        of routine-but-stateful.

        ``target_label`` is the accessibility title of the control actually
        under the pointer, and it is what makes the guard a safety mechanism
        rather than an honesty check on the model's narration. The markers name
        UI controls, so reading them off the model's ``sub_goal`` asked the
        model to declare its own risk: a click on a button titled "Delete
        account" described as "continue with the flow" classified as
        :attr:`Risk.NONE` and ran unattended. The screen does not get a vote on
        how it is described.
        """
        if isinstance(turn.action, (Finish, Wait, LoadSkill, WebSearch, WebFetch)):
            return Risk.NONE
        subject = turn.sub_goal.lower()
        if target_label:
            subject = f"{subject} {target_label}".lower()
        if isinstance(turn.action, PressHotkey):
            subject = f"{subject} {turn.action.key}".lower()
        if isinstance(turn.action, CallTool):
            # The tool's name is a short identifier the server chose
            # (``delete_file``, ``send_message``), never prose — token matching
            # it is exactly as safe as matching a control's accessibility title.
            subject = f"{subject} {turn.action.tool}".lower()

        words = _intent_words(subject)
        if words & self.destructive_markers:
            return Risk.DESTRUCTIVE
        if words & self.typed_commands:
            return Risk.DESTRUCTIVE
        if isinstance(turn.action, (TypeText, ClipboardPaste)) and _looks_like_a_command(
            turn.action.text, self.typed_commands
        ):
            return Risk.DESTRUCTIVE
        if isinstance(turn.action, CallTool):
            if _arguments_are_destructive(
                turn.action.arguments, self.typed_commands, self.destructive_markers
            ):
                return Risk.DESTRUCTIVE
            if words & self.routine_markers:
                return Risk.ROUTINE
            # Nothing matched, and that is not the same as nothing happening: a
            # tool this policy has never heard of is a third-party program with
            # side effects the orchestrator cannot see. Guarded mode asks about
            # it; full autonomy still runs it.
            return Risk.ROUTINE
        if words & self.routine_markers:
            return Risk.ROUTINE
        return Risk.NONE


def _intent_words(subject: str) -> set[str]:
    """Whole words of a phrase, punctuation folded (pure).

    Tokenised on whitespace so `rm` matches the *word* `rm`, never the letters
    inside `confi rm-dialog`, and normalised first so `delete-file`,
    `delete_file` and `delete.` are one marker.
    """
    normalized = re.sub(r"[^\w\-]+", " ", subject, flags=re.UNICODE)
    return set(normalized.replace("-", " ").replace("_", " ").split())


#: Longest text still short enough to be something a person types at a prompt.
#: Above this the payload is prose, and prose is where a command word appears
#: as a *subject* rather than as an instruction.
COMMAND_LENGTH_MAX: Final[int] = 200


def _looks_like_a_command(text: str, commands: frozenset[str]) -> bool:
    """Is this typed payload a command, or prose that mentions one (pure)?

    Folding the whole payload into the subject was the third instance of one
    mistake: the markers describe things being *done*, and a long piece of
    writing merely talks about them. Measured on a live run of "research the
    AI news and write me a summary in Notes", the agent produced a correct
    1,575-character summary whose first bullet reported an "automated shutdown"
    capability — and pasting that article into a note was classified as issuing
    a shutdown command, so an unattended run at full autonomy stopped to ask
    permission and then died waiting.

    A command lives at the start of a line, so a line beginning with one counts
    however long the payload is. Short text counts wherever the word falls,
    because `echo hi; rm -rf ~` is a command line whichever half you read.
    """
    lowered = text.lower()
    for line in lowered.splitlines():
        leading = _intent_words(line)
        first = line.strip().split()
        if first and _intent_words(first[0]) & commands:
            return True
        if len(line) <= COMMAND_LENGTH_MAX and leading & commands:
            return True
    return False


#: How deep to walk a tool's arguments looking for strings. MCP servers nest
#: their parameters a level or two ({"file": {"path": ...}}); nothing
#: legitimate hides a shell command eight levels down, and a bound means a
#: hostile server cannot make classification recurse forever.
ARGUMENT_WALK_MAX_DEPTH: Final[int] = 6


def _argument_strings(value: object, *, depth: int) -> tuple[str, ...]:
    """Every string reachable inside a tool's arguments (pure).

    Values arrive as ``dict[str, object]`` off the wire, so the payload that
    matters — the shell line, the SQL, the recipient — can be a bare string, an
    element of a list, or a field of a nested object. Reading only the top
    level would classify ``{"command": "rm -rf /"}`` and miss
    ``{"exec": {"argv": ["rm", "-rf", "/"]}}``, which is the same call.
    """
    if depth > ARGUMENT_WALK_MAX_DEPTH:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, dict):
        return tuple(
            found
            for item in cast(dict[str, object], value).values()
            for found in _argument_strings(item, depth=depth + 1)
        )
    if isinstance(value, (list, tuple)):
        return tuple(
            found
            for item in cast(Sequence[object], value)
            for found in _argument_strings(item, depth=depth + 1)
        )
    return ()


def _arguments_are_destructive(
    arguments: dict[str, object],
    commands: frozenset[str],
    markers: frozenset[str],
) -> bool:
    """Does a tool call's payload ask for something destructive (pure)?

    Two rules, because the payload carries two different kinds of danger and
    they need different tests:

    * A **shell command** is caught by :func:`_looks_like_a_command`, the same
      test typed text gets — ``{"command": "rm -rf /"}`` is a command line
      whether a person typed it or a model passed it as a parameter.
    * A **destructive verb** ("delete", "pay", "send") only counts inside a
      *short* value. An MCP tool that takes a document body will be handed
      prose that talks about deleting things, and flagging that is the same
      false positive that once stopped a full-autonomy run dead on the word
      "shutdown" appearing inside a news summary.
    """
    for text in _argument_strings(arguments, depth=0):
        if _looks_like_a_command(text, commands):
            return True
        if len(text) <= COMMAND_LENGTH_MAX and _intent_words(text.lower()) & markers:
            return True
    return False


def classify_risk(
    turn: AgentTurn,
    *,
    target_label: str | None = None,
    policy: AutonomyPolicy | None = None,
) -> Risk:
    """Pure: return the risk of a decision under the given (or default) policy.

    ``target_label`` is the accessibility title of the control the action
    targets, when the caller could determine one — see
    :meth:`AutonomyPolicy.classify` for why the model's prose alone is not a
    safe input to this decision.
    """
    if policy is None:
        policy = AutonomyPolicy()
    return policy.classify(turn, target_label)


def decide_permission(level: AutonomyLevel, risk: Risk) -> PermissionDecision:
    """Pure: map (level, risk) to an allow/confirm/block decision.

    Policy:
    * Level 0 -- observer: never act, always recommend = BLOCK at the driver.
    * Level 1 -- supervised: every action needs human confirmation.
    * Level 2 -- guarded: plain navigation auto-runs; routine-but-stateful
      actions (save/close/confirm dialog clicks) pause for confirmation;
      destructive actions ask.
    * Level 3 -- full: everything non-destructive runs autonomously;
      destructive actions still require confirmation.
    """
    # Full autonomy still requires confirmation for destructive operations.
    # Physical side effects must remain fail-closed even in unattended mode.
    if risk is Risk.DESTRUCTIVE:
        if level is AutonomyLevel.OBSERVER:
            return PermissionDecision.BLOCK
        return PermissionDecision.CONFIRM

    if level is AutonomyLevel.FULL:
        return PermissionDecision.ALLOW

    # Non-destructive path.
    if level is AutonomyLevel.OBSERVER:
        return PermissionDecision.BLOCK
    if level is AutonomyLevel.SUPERVISED:
        return PermissionDecision.CONFIRM
    # Guarded mode: the routine markers ("save", "close", "confirm", "ok" —
    # the multilingual ``_ROUTINE_MARKERS`` set) name state-changing dialog
    # actions that the policy treats as worth a human sign-off (M2: this
    # branch makes that list live policy instead of dead classification).
    if risk is Risk.ROUTINE:
        return PermissionDecision.CONFIRM
    return PermissionDecision.ALLOW


#: Re-exported from :mod:`computeruse.security.permissions` so importing them
#: from here keeps working; the definitions live in that leaf module to keep
#: this one free to depend on the orchestrator's action schemas.
__all__ = [
    "AutonomyLevel",
    "AutonomyPolicy",
    "PermissionConfirmationRequired",
    "PermissionDecision",
    "PermissionDeniedError",
    "Risk",
    "classify_risk",
    "decide_permission",
]
