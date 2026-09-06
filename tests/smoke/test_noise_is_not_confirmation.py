"""An animated region must not vouch for an action.

``verdict`` classifies a region that changed *wholesale* as NOISE: an
animation, a transition, a different view. It says something moved; it cannot
say which action moved it, because a spinner looks the same whether the click
landed or not.

The verification path used to read it as a confirmation, through
``Verification.changed`` — which merges CHANGED and NOISE and so removes the
choice ``verdict``'s own docstring promises the caller. Because ``combine``
lets any confirmation outrank a *direct* denial, a click the accessibility
tree explicitly denied came back verified on an animated screen.
"""

from __future__ import annotations

from computeruse.orchestrator.evidence import Evidence, combine, expectation_for
from computeruse.orchestrator.loop import OodaRunner
from computeruse.orchestrator.schemas import AgentTurn, Finish, MouseClick
from computeruse.vision.capture import ScreenCapture
from computeruse.vision.coordinates import Point, Rect, Size
from computeruse.vision.diff import ChangeKind, Verification, verdict

FLIPPED_REGION = (
    tuple((0.0,) * 48 for _ in range(48)),
    tuple((1.0,) * 48 for _ in range(48)),
)


def _noise_verification() -> Verification:
    before, after = FLIPPED_REGION
    return Verification(Rect(Point(0, 0), Size(48, 48)), verdict(before, after))


def test_a_region_flipped_end_to_end_is_noise_not_a_clean_change() -> None:
    """The fixture really is the case under discussion."""
    assert _noise_verification().verdict.kind is ChangeKind.NOISE


def test_noise_read_as_confirmation_would_outrank_a_direct_denial() -> None:
    """Why this matters: it is not a weak vote, it is an overriding one.

    Pinning the rule in ``combine`` that made the old reading dangerous, so a
    future change to the weighting cannot quietly re-open the hole.
    """
    assert (
        combine(
            direct=(Evidence.CONTRADICTED,), circumstantial=(Evidence.CONFIRMED,)
        )
        is Evidence.CONFIRMED
    )


def test_changed_still_merges_the_two_and_says_so() -> None:
    """The trap is left in place for its honest use, with a warning attached.

    ``changed`` answers "did the pixels move", which is a real question
    elsewhere. What it must never again be is the verification path's witness.
    """
    verification = _noise_verification()
    assert verification.changed
    assert "wrong" in (Verification.changed.__doc__ or "")


def _flat_capture(value: int, width: int = 96, height: int = 96) -> ScreenCapture:
    """A uniform BGRA frame — the two of them differ everywhere."""
    return ScreenCapture(
        display_id=0,
        width=width,
        height=height,
        scale=1.0,
        origin_x=0.0,
        origin_y=0.0,
        data=bytes([value, value, value, 255]) * (width * height),
    )


def test_the_verification_path_abstains_on_noise_instead_of_confirming() -> None:
    """The regression itself: an animated region votes INCONCLUSIVE.

    Abstaining is the honest answer — something moved, but nothing here can
    say this action moved it. It also cannot fail the action on its own: with
    pixels silent the circumstantial quorum needs another witness to agree, so
    the cost of giving up the confirmation is an *unverified* action, never a
    falsely failed one.
    """
    before, after = _flat_capture(0), _flat_capture(255)
    runner = OodaRunner(
        provider=lambda _s: AgentTurn(
            thought="t", sub_goal="s", action=Finish(type="finish", status="success", summary="x")
        ),
        execute_physical=lambda _action: None,
        sensor=lambda: after,
        verify_enabled=True,
        max_steps=1,
    )
    expectation = expectation_for(MouseClick(type="mouse_click", x=48, y=48))
    assert expectation.pixel == "region"
    verdict_evidence = runner._pixel_evidence(before, expectation)  # pyright: ignore[reportPrivateUsage]
    assert verdict_evidence is Evidence.INCONCLUSIVE, (
        f"an animated region reported {verdict_evidence.value}; as CONFIRMED it "
        "would outrank a direct denial and verify a click that never landed"
    )
