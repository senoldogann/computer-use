"""Agent orchestration engine (OODA loop, action contracts, RPC client)."""

from computeruse.orchestrator.client import (
    ActuationClient,
    DriverConnectionError,
    DriverRpcError,
    action_to_request,
)
from computeruse.orchestrator.evidence import (
    ActionExpectation,
    CompletionVerdict,
    Evidence,
    combine,
    expectation_for,
)
from computeruse.orchestrator.failures import (
    Failure,
    FailureKind,
    RecoveryAction,
    UnrecoverableFailureError,
    classify_failure,
    recovery_for,
    recovery_hint,
)
from computeruse.orchestrator.loop import (
    FocusLostError,
    KillSwitchTripped,
    MaxStepsError,
    Observation,
    OodaRunner,
    StaleObservationError,
    StuckLoopError,
    VerificationFailedError,
    WorkingState,
    decide_step,
    target_point_of,
    verification_region,
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
    "ActionExpectation",
    "ActuationClient",
    "AgentTurn",
    "CompletionVerdict",
    "DriverConnectionError",
    "DriverRpcError",
    "Evidence",
    "Failure",
    "FailureKind",
    "FocusLostError",
    "InvalidDecisionError",
    "KillSwitchTripped",
    "MaxStepsError",
    "Observation",
    "OodaRunner",
    "PermissionConfirmationRequired",
    "PermissionDeniedError",
    "RecoveryAction",
    "StaleObservationError",
    "StuckLoopError",
    "UnrecoverableFailureError",
    "VerificationFailedError",
    "WorkingState",
    "action_to_request",
    "classify_failure",
    "combine",
    "decide_step",
    "decision_prompt",
    "expectation_for",
    "parse_decision",
    "recovery_for",
    "recovery_hint",
    "scaffolded_provider",
    "state_context",
    "target_point_of",
    "verification_region",
]