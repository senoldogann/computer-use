"""The permission verdict and its two typed refusals (Law 5.1).

A deliberate leaf: this module imports nothing from ``computeruse``, which is
the whole reason it exists. ``orchestrator.loop`` needs these three names, and
``security.autonomy`` needs ``orchestrator.schemas`` to classify an action —
so holding them in ``autonomy`` made the two packages import each other. The
cycle stayed invisible while every entry point happened to import the
orchestrator first, and ``import computeruse.security.autonomy`` on its own
raised ImportError.

The split also reads correctly: the *verdict* and the refusals are pure policy
vocabulary, independent of how any particular action is classified.
"""

from __future__ import annotations

from enum import Enum


class PermissionDecision(Enum):
    """The guard's answer to "may this action run right now?"."""

    ALLOW = "allow"
    CONFIRM = "confirm"    # Human must approve first (Law 5.1: Pa).
    BLOCK = "block"        # Denied outright, even at higher autonomy (safety).


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
