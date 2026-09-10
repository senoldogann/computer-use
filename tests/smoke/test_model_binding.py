"""Decide/audit transport split (--audit-model).

The completion auditor re-reads one screen against one claim — it must be
allowed to ride a cheaper transport than the decide turns. These tests pin
the routing: each role calls its own transport and never the other's, and
the default (no flag) keeps one shared transport.
"""

from __future__ import annotations

import pytest

from computeruse.cli import load_model_binding, parse_args
from computeruse.orchestrator.loop import WorkingState
from computeruse.orchestrator.prompts import InvalidDecisionError

DECIDE_CALLS: list[str] = []
AUDIT_CALLS: list[str] = []


def fake_decide(prompt: str) -> str:
    DECIDE_CALLS.append(prompt)
    return "not json at all"


def fake_audit(prompt: str) -> str:
    AUDIT_CALLS.append(prompt)
    return '{"satisfied": true, "evidence": "saw it on screen"}'


@pytest.fixture(autouse=True)
def _clean_calls() -> object:
    DECIDE_CALLS.clear()
    AUDIT_CALLS.clear()
    yield None


def _binding(audit_spec: str | None = None):  # type: ignore[no-untyped-def]
    kwargs: dict[str, object] = {}
    if audit_spec is not None:
        kwargs["audit_spec"] = audit_spec
    return load_model_binding(
        "tests.smoke.test_model_binding:fake_decide",
        app="Safari",
        **kwargs,  # type: ignore[arg-type]
    )


def test_decide_turns_use_the_decide_transport() -> None:
    binding = _binding(audit_spec="tests.smoke.test_model_binding:fake_audit")
    with pytest.raises(InvalidDecisionError):
        binding.provider(WorkingState(goal="x"))
    # One ask plus the scaffold's bounded corrective retries, all decide.
    assert len(DECIDE_CALLS) == 3
    assert AUDIT_CALLS == []


def test_audits_use_the_audit_transport() -> None:
    binding = _binding(audit_spec="tests.smoke.test_model_binding:fake_audit")
    verdict = binding.completion_check(WorkingState(goal="x"), "done")
    assert verdict.satisfied is True
    assert len(AUDIT_CALLS) == 1
    assert DECIDE_CALLS == []


def test_default_shares_one_transport_for_both_roles() -> None:
    binding = _binding()
    # The shared transport is the decide fake, whose reply is not a verdict:
    # the failure itself proves the audit rode the decide transport.
    with pytest.raises(InvalidDecisionError):
        binding.completion_check(WorkingState(goal="x"), "done")
    assert len(AUDIT_CALLS) == 0
    assert len(DECIDE_CALLS) == 1


def test_audit_model_flag_parses_and_defaults_to_none() -> None:
    args = parse_args(["--goal", "x", "--model", "codex"])
    assert args.audit_model is None
    args = parse_args(
        ["--goal", "x", "--model", "codex", "--audit-model", "opencode:free/model"]
    )
    assert args.audit_model == "opencode:free/model"
