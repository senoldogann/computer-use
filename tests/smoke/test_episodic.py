"""Tests for the episodic memory tier (Law 4.1) and its Law 3 integration."""

from __future__ import annotations

from pathlib import Path

import pytest

from computeruse.memory.episodic import (
    EpisodicStore,
    signature_from_trace,
    signature_of_episode,
)
from computeruse.memory.schemas import Episode
from computeruse.orchestrator.schemas import MouseClick, MouseMove, PressHotkey
from computeruse.skills.distiller import Trajectory, distill


def _steps() -> tuple[MouseClick, MouseMove, PressHotkey]:
    return (
        MouseClick(type="mouse_click", x=10, y=10),
        MouseMove(type="mouse_move", x=120, y=80),
        PressHotkey(type="press_hotkey", modifiers=["command"], key="s"),
    )


def _make_episode() -> Episode:
    return Episode(
        episode_id="run.0001",
        app="Numbers",
        description="export rows",
        steps=_steps(),
        outcome="success",
        retrospective="used File > Export",
        signature=signature_from_trace("Numbers", _steps()),
    )


def test_signature_shared_with_distiller() -> None:
    episode = _make_episode()
    # The memory tier and the distiller must agree on what makes a flow unique.
    trace = Trajectory(app="Numbers", description="x", steps=_steps())
    assert episode.signature == signature_of_episode(episode)
    assert episode.signature == distill(trace, known_signatures=set()).signature


def test_episodic_store_round_trip_preserves_retrospective(tmp_path: Path) -> None:
    store = EpisodicStore(tmp_path)
    store.record(_make_episode())

    loaded = store.episodes()
    assert len(loaded) == 1
    assert loaded[0].outcome == "success"
    assert loaded[0].retrospective == "used File > Export"
    assert loaded[0].steps == _steps()


def test_known_signatures_feeds_distiller_dedup(tmp_path: Path) -> None:
    store = EpisodicStore(tmp_path)
    store.record(_make_episode())

    # The SAME workflow runs again in a new session; the distiller must refuse
    # to re-distill it because episodic memory already saw the signature.
    trace = Trajectory(app="Numbers", description="repeat", steps=_steps())
    result = distill(trace, known_signatures=store.known_signatures())
    assert result.kind == "duplicate"


def test_record_refuses_clobber(tmp_path: Path) -> None:
    store = EpisodicStore(tmp_path)
    store.record(_make_episode())
    with pytest.raises(FileExistsError):
        store.record(_make_episode())


def test_signature_is_coordinate_agnostic() -> None:
    # Same flow, different coordinates => same identity (de-dup must survive
    # the natural drift of UI positions between runs).
    a = (
        MouseClick(type="mouse_click", x=10, y=10),
        MouseMove(type="mouse_move", x=120, y=80),
    )
    b = (
        MouseClick(type="mouse_click", x=999, y=999),
        MouseMove(type="mouse_move", x=575, y=333),
    )
    assert signature_from_trace("Numbers", a) == signature_from_trace("Numbers", b)