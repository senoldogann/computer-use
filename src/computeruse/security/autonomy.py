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

from dataclasses import dataclass
from enum import Enum

from computeruse.orchestrator.schemas import (
    AgentTurn,
    ClipboardPaste,
    PressHotkey,
    TypeText,
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


class PermissionDecision(Enum):
    """The guard's answer to "may this action run right now?"."""

    ALLOW = "allow"
    CONFIRM = "confirm"    # Human must approve first (Law 5.1: Pa).
    BLOCK = "block"        # Denied outright, even at higher autonomy (safety).


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

    def classify(self, turn: AgentTurn) -> Risk:
        """Classify a single model decision into a risk level (pure)."""
        subject = turn.sub_goal.lower()
        if isinstance(turn.action, PressHotkey):
            subject = f"{subject} {turn.action.key}".lower()
        elif isinstance(turn.action, (TypeText, ClipboardPaste)):
            subject = f"{subject} {turn.action.text}".lower()

        # Normalize punctuation before tokenizing so `delete-file`, `delete_file`,
        # and `delete.` are treated as the same intent marker.
        import re

        normalized = re.sub(r"[^\w\-]+", " ", subject, flags=re.UNICODE)
        words = set(normalized.replace("-", " ").replace("_", " ").split())
        # Tokenise on whitespace so `rm` matches the *word* `rm`, never the
        # letters inside `confi rm-dialog`. This is the difference between a
        # deliberate `rm -rf ~` and prose that merely contains the sequence.
        if words & self.destructive_markers:
            return Risk.DESTRUCTIVE
        if words & self.typed_commands:
            return Risk.DESTRUCTIVE
        if words & self.routine_markers:
            return Risk.ROUTINE
        return Risk.NONE


def classify_risk(turn: AgentTurn, policy: AutonomyPolicy | None = None) -> Risk:
    """Pure: return the risk of a decision under the given (or default) policy."""
    if policy is None:
        policy = AutonomyPolicy()
    return policy.classify(turn)


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


class PermissionDeniedError(PermissionError):
    """An action was blocked by the autonomy guard (Law 5).

    Raised by ``OodaRunner`` at the VALIDATE step when the guard returns
    ``BLOCK``. Distinct from a generic failure so callers can tell "the model
    did the wrong thing" from "the security policy stopped a dangerous move".
    """


class PermissionConfirmationRequired(PermissionError):
    """A guarded/destructive action needs human approval before it can run.

    At autonomy levels 1-3 a destructive (or supervised) action must not touch
    the physical host until a human confirms. This indicates *paused waiting
    for input*, not a policy violation.
    """