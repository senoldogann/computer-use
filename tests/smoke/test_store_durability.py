"""The JSON stores must survive an interrupted write and a corrupt neighbour.

Two failures that compound: a run killed mid-write leaves a truncated file,
and a scan that raises on the first unreadable file turns that one casualty
into an agent that cannot start at all.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from computeruse.atomic import write_atomic
from computeruse.skills.registry import SkillRegistry
from computeruse.skills.schemas import SkillDefinition


def _definition(skill_id: str) -> SkillDefinition:
    return SkillDefinition(
        skill_id=skill_id,
        description="open the notes app and write a line",
        app="Notes",
        tags=("notes",),
        steps=("click the new-note button -> mouse_click:button=left",),
        signature=skill_id.split(".")[-1],
    )


def test_the_write_replaces_rather_than_truncates(tmp_path: Path) -> None:
    """The file is swapped in whole, never opened and rewritten in place.

    A hardlink pins the original inode. ``write_text`` truncates and refills
    *that* inode, so a reader holding it — or a crash mid-write — sees the
    half-written result; ``os.replace`` publishes a new inode in one step, so
    the old one keeps its complete previous content. That difference is the
    whole point: an interrupted run must not leave a truncated store behind.
    """
    target = tmp_path / "skill.json"
    write_atomic(target, '{"first": true}\n')
    pinned = tmp_path / "pinned.json"
    os.link(target, pinned)

    write_atomic(target, '{"second": true}\n')

    assert json.loads(target.read_text(encoding="utf-8")) == {"second": True}
    assert json.loads(pinned.read_text(encoding="utf-8")) == {"first": True}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["pinned.json", "skill.json"]


def test_the_index_skips_a_corrupt_skill_instead_of_refusing_to_open(
    tmp_path: Path,
) -> None:
    """One truncated file must not cost the agent every other skill.

    ``load`` stays loud — there the caller named that skill. ``index`` runs
    before every retrieval, so raising here meant a single casualty stopped
    the agent from starting.
    """
    store = tmp_path / "skills"
    registry = SkillRegistry(store)
    registry.save(_definition("notes.aaaaaaaaaaaaaaaa"))
    registry.save(_definition("notes.bbbbbbbbbbbbbbbb"))

    # A write interrupted halfway: valid prefix, no closing brace.
    (store / "notes.cccccccccccccccc.json").write_text(
        '{"skill_id": "notes.cccccccccccccccc", "desc', encoding="utf-8"
    )

    surviving = {summary.skill_id for summary in SkillRegistry(store).index()}
    assert surviving == {"notes.aaaaaaaaaaaaaaaa", "notes.bbbbbbbbbbbbbbbb"}


def test_loading_a_named_corrupt_skill_still_raises(tmp_path: Path) -> None:
    """The tolerant scan must not soften the single-skill read (Law 6.3)."""
    store = tmp_path / "skills"
    store.mkdir(parents=True)
    (store / "notes.dddddddddddddddd.json").write_text('{"broken', encoding="utf-8")

    with pytest.raises(json.JSONDecodeError):
        SkillRegistry(store).load("notes.dddddddddddddddd")
