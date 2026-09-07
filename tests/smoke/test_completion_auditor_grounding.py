"""Regression tests for Completion Auditor grounding and anti-hallucination.

Tests that:
1. Completion prompt includes strict grounding checks against observed_trail and tool_history.
2. An agent typing a plausible report with non-existent files (e.g. driver.rs, actuation.py)
   is treated as an assertion requiring corroboration, not self-verifying proof.
3. parse_completion properly enforces boolean verdict and non-empty evidence.
4. An auditor given ungrounded/contradictory claims rejects finish.
"""

from __future__ import annotations

import json

from computeruse.orchestrator.loop import WorkingState
from computeruse.orchestrator.prompts import (
    COMPLETION_AUDIT_CONTRACT,
    completion_auditor,
    completion_prompt,
)


def test_completion_audit_contract_contains_grounding_requirements() -> None:
    """The contract must explicitly require cross-checking external claims and reject hallucinations."""
    assert "Grounding Check" in COMPLETION_AUDIT_CONTRACT
    assert "observed_trail" in COMPLETION_AUDIT_CONTRACT
    assert "Actor Claims & Typed Text are NOT Evidence" in COMPLETION_AUDIT_CONTRACT
    assert "driver.rs or actuation.py" in COMPLETION_AUDIT_CONTRACT


def test_completion_prompt_embeds_observed_trail_and_tool_history() -> None:
    """The prompt must pass machine-read trail to the auditor for cross-checking."""
    state = WorkingState(
        goal="GitHub reposunu incele ve fiziksel kontrol dosyalarını TextEdit'e yaz.",
        active_window="TextEdit — Untitled",
        ui_elements=("TextEdit text area: driver.rs, actuation.py",),
        observed_trail=(
            "Safari — senoldogann/computer-use: driver/src/main.rs, driver/src/backend.rs, driver/src/quartz.rs",
        ),
        tool_history=("git ls-files: driver/src/main.rs, driver/src/backend.rs, driver/src/quartz.rs",),
    )
    prompt = completion_prompt(state, "TextEdit contains report on driver.rs and actuation.py", app="TextEdit")

    assert "driver/src/main.rs" in prompt
    assert "driver/src/backend.rs" in prompt
    assert "Text observed on screen EARLIER" in prompt
    assert "External tool results observed in this run" in prompt
    assert "Agent's completion claim: TextEdit contains report on driver.rs and actuation.py" in prompt


def test_auditor_rejects_hallucinated_file_names_when_not_grounded() -> None:
    """When the auditor model evaluates ungrounded claims, it emits satisfied: false."""
    state = WorkingState(
        goal="GitHub reposunu incele ve 3 fiziksel kontrol dosyasını TextEdit'e yaz. Gördüğün gerçek içeriğe dayan.",
        active_window="TextEdit — Untitled",
        ui_elements=("TextEdit: Dosyalar driver.rs ve actuation.py",),
        observed_trail=(
            "Safari — senoldogann/computer-use: driver/src/main.rs, driver/src/backend.rs, driver/src/quartz.rs",
        ),
    )

    def mock_strict_auditor(prompt: str, _screenshot: str | None = None) -> str:
        # Strict auditor verifies that files mentioned in claim/TextEdit exist in observed_trail
        if "driver.rs" in prompt and "driver.rs" not in state.observed_trail[0]:
            return json.dumps({
                "satisfied": False,
                "evidence": "Report claims 'driver.rs' and 'actuation.py', but observed repository contains 'driver/src/main.rs' and 'driver/src/backend.rs'. Claim is an ungrounded hallucination."
            })
        return json.dumps({"satisfied": True, "evidence": "All files match observed repository."})

    audit_fn = completion_auditor(mock_strict_auditor, app="TextEdit")
    verdict = audit_fn(state, "TextEdit contains report on driver.rs and actuation.py")

    assert verdict.satisfied is False
    assert "ungrounded hallucination" in verdict.evidence


def test_auditor_accepts_when_reported_files_match_observed_evidence() -> None:
    """When the report in TextEdit matches observed files in observed_trail, it emits satisfied: true."""
    state = WorkingState(
        goal="GitHub reposunu incele ve 3 fiziksel kontrol dosyasını TextEdit'e yaz. Gördüğün gerçek içeriğe dayan.",
        active_window="TextEdit — Untitled",
        ui_elements=("TextEdit: Dosyalar driver/src/main.rs, driver/src/backend.rs, driver/src/quartz.rs",),
        observed_trail=(
            "Safari — senoldogann/computer-use: driver/src/main.rs, driver/src/backend.rs, driver/src/quartz.rs",
        ),
    )

    def mock_strict_auditor(prompt: str, _screenshot: str | None = None) -> str:
        return json.dumps({
            "satisfied": True,
            "evidence": "TextEdit visible report correctly lists driver/src/main.rs, backend.rs, and quartz.rs, matching earlier observed repository content."
        })

    audit_fn = completion_auditor(mock_strict_auditor, app="TextEdit")
    verdict = audit_fn(state, "TextEdit contains report on main.rs, backend.rs, and quartz.rs")

    assert verdict.satisfied is True
    assert "correctly lists" in verdict.evidence
