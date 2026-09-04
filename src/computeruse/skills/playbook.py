"""Procedural guidance from external SKILL.md repositories (Law 3 / Law 4).

Discovers and ranks on-disk ``SKILL.md`` playbooks (from ``~/.agents/skills`` and
``./.agents/skills``) without retaining heavy Markdown bodies in memory (Stage 1
index). Provides lightweight prompt guidance framed strictly as hints.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from computeruse.skills.registry import content_tokens
from computeruse.skills.schemas import UNINFORMATIVE_WORDS

LOGGER: Final = logging.getLogger(__name__)

# Maximum bytes to read from the head of a SKILL.md file. Frontmatter is always
# at the top; reading beyond this would pull arbitrary bodies into memory.
MAX_FRONTMATTER_BYTES: Final[int] = 8192

# Maximum nesting depth from the discovery root (prevents runaway traversal).
MAX_PLAYBOOK_DISCOVERY_DEPTH: Final[int] = 5

# Hard ceiling on discovered playbooks across all roots.
MAX_PLAYBOOK_FILES: Final[int] = 200

# Canonical root directories where external agent skills are installed.
DEFAULT_PLAYBOOK_ROOTS: Final[tuple[Path, ...]] = (
    Path(".agents/skills"),
    Path.home() / ".agents/skills",
)


@dataclass(frozen=True)
class PlaybookSummary:
    """Stage-1 index entry for an external SKILL.md playbook.

    Contains only metadata; the full body is never retained in memory.
    """

    name: str
    description: str
    tags: tuple[str, ...]
    source_path: Path


@dataclass(frozen=True)
class PlaybookMatch:
    """A playbook scored against a query by relevance."""

    playbook: PlaybookSummary
    score: int


def _parse_yaml_frontmatter_lines(lines: list[str]) -> dict[str, str]:
    """Parse YAML key-value pairs from frontmatter lines (pure).

    Supports normal scalars and YAML block scalars (folded '>' and literal '|'
    with chomping modifiers '-', '+').
    """
    data: dict[str, str] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        match = re.match(r"^([a-zA-Z0-9_-]+)\s*:\s*(.*)$", line)
        if not match:
            i += 1
            continue
        key = match.group(1).lower()
        val = match.group(2).strip()

        # Handle YAML block scalar indicators: >, >-, >+, |, |-, |+
        if val in (">", ">-", ">+", "|", "|-", "|+"):
            block_type = val[0]  # '>' (folded) or '|' (literal)
            chomp = val[1:]  # '-', '+', or ''
            block_lines: list[str] = []
            i += 1
            base_indent: int | None = None
            while i < len(lines):
                bline = lines[i]
                if not bline.strip():
                    block_lines.append("")
                    i += 1
                    continue
                indent = len(bline) - len(bline.lstrip(" "))
                if base_indent is None:
                    if indent == 0:
                        break
                    base_indent = indent
                elif indent < base_indent:
                    break
                block_lines.append(bline[base_indent:])
                i += 1

            if block_type == ">":
                # Folded scalar: lines within paragraphs are space-separated,
                # blank lines separate paragraphs.
                paragraphs: list[str] = []
                current_para: list[str] = []
                for bl in block_lines:
                    if not bl.strip():
                        if current_para:
                            paragraphs.append(" ".join(current_para))
                            current_para = []
                    else:
                        current_para.append(bl.strip())
                if current_para:
                    paragraphs.append(" ".join(current_para))
                content = "\n\n".join(paragraphs)
            else:
                # Literal scalar: preserves newlines verbatim.
                content = "\n".join(block_lines)

            if chomp == "-":
                content = content.strip()
            elif chomp == "+":
                pass
            else:
                content = content.strip()

            data[key] = content
            continue

        # Normal scalar: strip optional enclosing quotes.
        if (val.startswith('"') and val.endswith('"')) or (
            val.startswith("'") and val.endswith("'")
        ):
            val = val[1:-1]
        data[key] = val
        i += 1

    return data


def parse_skill_markdown(text: str, source_path: Path) -> PlaybookSummary | None:
    """Extract a PlaybookSummary from SKILL.md frontmatter (pure).

    Returns None if no frontmatter is found or if description is missing.
    Logs and ignores any requested ``allowed-tools`` (a markdown document
    cannot grant itself tool permissions).
    """
    lines = text.splitlines()
    idx = 0
    while idx < len(lines) and not lines[idx].strip():
        idx += 1
    if idx >= len(lines) or lines[idx].strip() != "---":
        return None
    idx += 1
    fm_lines: list[str] = []
    while idx < len(lines) and lines[idx].strip() not in ("---", "..."):
        fm_lines.append(lines[idx])
        idx += 1

    data = _parse_yaml_frontmatter_lines(fm_lines)

    # Name defaults to frontmatter name or directory name.
    raw_name = data.get("name", "").strip()
    name = raw_name or source_path.parent.name

    description = data.get("description", "").strip()
    if not description:
        return None

    # Allowed tools: parsed and logged for auditability, strictly ignored.
    if "allowed-tools" in data:
        LOGGER.info(
            "playbook %s requests allowed-tools (ignored): %s",
            name,
            data["allowed-tools"],
        )

    # Tags: parsed if present as [a, b] or comma-delimited.
    raw_tags = data.get("tags", "")
    tags: tuple[str, ...] = ()
    if raw_tags:
        cleaned = raw_tags.strip("[]")
        tags = tuple(
            t.strip(" '\"")
            for t in cleaned.split(",")
            if t.strip(" '\"")
        )

    return PlaybookSummary(
        name=name,
        description=description,
        tags=tags,
        source_path=source_path,
    )


def discover_playbooks(
    roots: Iterable[Path] | None = None,
) -> tuple[PlaybookSummary, ...]:
    """Discover SKILL.md playbooks under the given root directories (I/O shell).

    Refuses symlinks (following inbox.py pattern), enforces a max depth of 5,
    caps discovered files at 200, and reads at most 8 KB per file.
    Deterministic ordering by name and source path.
    """
    search_roots = roots if roots is not None else DEFAULT_PLAYBOOK_ROOTS
    found: list[PlaybookSummary] = []
    seen_paths: set[Path] = set()

    for root in search_roots:
        if not root.is_dir():
            continue
        try:
            entries = sorted(root.rglob("SKILL.md"), key=lambda p: str(p))
        except OSError as exc:
            LOGGER.warning("failed to list playbook root %s: %s", root, exc)
            continue

        for p in entries:
            if len(found) >= MAX_PLAYBOOK_FILES:
                LOGGER.warning(
                    "playbook discovery hit ceiling of %d files; stopping scan",
                    MAX_PLAYBOOK_FILES,
                )
                break

            # Symlink guard: refuse symlinked files and files under symlinked dirs
            if p.is_symlink() or not p.is_file():
                continue
            try:
                if any(parent.is_symlink() for parent in p.parents if parent != root):
                    continue
            except OSError:
                continue

            # Depth limit
            try:
                depth = len(p.relative_to(root).parts) - 1
                if depth > MAX_PLAYBOOK_DISCOVERY_DEPTH:
                    continue
            except ValueError:
                continue

            try:
                resolved = p.resolve()
            except OSError:
                continue
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)

            try:
                raw = p.read_bytes()[:MAX_FRONTMATTER_BYTES]
            except OSError as exc:
                LOGGER.warning("failed to read playbook %s: %s", p, exc)
                continue

            summary = parse_skill_markdown(
                raw.decode("utf-8", errors="replace"),
                source_path=p,
            )
            if summary is not None:
                found.append(summary)

    found.sort(key=lambda s: (s.name, str(s.source_path)))
    return tuple(found)


def search_playbooks(
    playbooks: Iterable[PlaybookSummary],
    query: str,
    *,
    min_score: int = 1,
) -> list[PlaybookMatch]:
    """Rank playbook summaries against a query (pure).

    Reuses registry.py scoring conventions: uninformative word filter, app/name
    token bonus (+2), tag bonus (+1), description content token match (+1),
    and deterministic (-score, name) sorting.
    """
    tokens = {
        token
        for token in query.lower().split()
        if token and token not in UNINFORMATIVE_WORDS
    }
    matches: list[PlaybookMatch] = []
    for pb in playbooks:
        score = 0
        description_tokens = content_tokens(pb.description)
        name_tokens = {pb.name.lower(), *pb.name.lower().split("-")}
        for token in tokens:
            if token in name_tokens:
                score += 2
            if any(token in tag.lower() for tag in pb.tags):
                score += 1
            if token in description_tokens:
                score += 1
        if score >= min_score:
            matches.append(PlaybookMatch(playbook=pb, score=score))

    matches.sort(key=lambda m: (-m.score, m.playbook.name))
    return matches


def best_playbook(
    playbooks: Iterable[PlaybookSummary],
    query: str,
    *,
    min_score: int = 1,
) -> PlaybookSummary | None:
    """Return the single highest-scoring playbook for a query, or None (pure)."""
    matches = search_playbooks(playbooks, query, min_score=min_score)
    return matches[0].playbook if matches else None


class PlaybookRegistry:
    """Imperative shell over on-disk SKILL.md repositories (Law 6 connector)."""

    def __init__(self, roots: tuple[Path, ...] | None = None) -> None:
        self._roots = roots if roots is not None else DEFAULT_PLAYBOOK_ROOTS
        self._index_cache: tuple[PlaybookSummary, ...] | None = None

    def index(self) -> list[PlaybookSummary]:
        if self._index_cache is None:
            self._index_cache = discover_playbooks(self._roots)
        return list(self._index_cache)

    def search(self, query: str, *, min_score: int = 1) -> list[PlaybookMatch]:
        return search_playbooks(self.index(), query, min_score=min_score)

    def best(self, query: str, *, min_score: int = 1) -> PlaybookSummary | None:
        return best_playbook(self.index(), query, min_score=min_score)
