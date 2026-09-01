"""Agent orchestration engine (OODA loop, action contracts, RPC client)."""

from computeruse.orchestrator.client import (
    ActuationClient,
    DriverConnectionError,
    DriverRpcError,
    action_to_request,
)
from computeruse.orchestrator.loop import (
    KillSwitchTripped,
    OodaRunner,
    VisualVerificationFailedError,
    WorkingState,
    decide_step,
    target_point_of,
    verification_region,
    visual_failure_diagnostics,
)
from computeruse.orchestrator.prompts import (
    ACTION_CONTRACT,
    InvalidDecisionError,
    decision_prompt,
    parse_decision,
    scaffolded_provider,
    state_context,
)
from computeruse.orchestrator.schemas import AgentTurn
from computeruse.security.autonomy import (
    PermissionConfirmationRequired,
    PermissionDeniedError,
)

__all__ = [
    "ACTION_CONTRACT",
    "ActuationClient",
    "AgentTurn",
    "DriverConnectionError",
    "DriverRpcError",
    "InvalidDecisionError",
    "KillSwitchTripped",
    "OodaRunner",
    "PermissionConfirmationRequired",
    "PermissionDeniedError",
    "VisualVerificationFailedError",
    "WorkingState",
    "action_to_request",
    "decide_step",
    "decision_prompt",
    "parse_decision",
    "scaffolded_provider",
    "state_context",
    "target_point_of",
    "verification_region",
    "visual_failure_diagnostics",
]