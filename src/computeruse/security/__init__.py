"""Autonomy governance and emergency control (Law 5)."""

from computeruse.security.autonomy import (
    AutonomyLevel,
    AutonomyPolicy,
    Risk,
    classify_risk,
    decide_permission,
)
from computeruse.security.killswitch import (
    CursorSample,
    KillSwitch,
    MouseShakeMonitor,
    is_mouse_shake,
)
from computeruse.security.permissions import (
    PermissionConfirmationRequired,
    PermissionDecision,
    PermissionDeniedError,
)

__all__ = [
    "AutonomyLevel",
    "AutonomyPolicy",
    "CursorSample",
    "KillSwitch",
    "MouseShakeMonitor",
    "PermissionConfirmationRequired",
    "PermissionDecision",
    "PermissionDeniedError",
    "Risk",
    "classify_risk",
    "decide_permission",
    "is_mouse_shake",
]