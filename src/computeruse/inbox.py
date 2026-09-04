"""Claim-once task inbox for unattended sessions (Law 6 trigger tier).

An ``--autonomous`` session chooses its own work from memory. This module adds
one more source with different economics: a watched folder where the operator
drops task files. Each file is consumed exactly once — claimed atomically,
then archived — so unlike memory it can never starve the mission queue by
producing forever.

Three decisions carry the design:

* **Claim, don't lock.** ``propose`` renames the file into ``.processing/``
  the moment it is picked up. On POSIX a rename is atomic: the loser of a
  race gets ``FileNotFoundError`` and moves to the next file, and no lock
  file (a second thing to orphan, a second thing to sweep) is needed.
* **Parked is processed, not failed.** A run that parks writes its question
  to the approval queue and its mission waits as ``blocked``. Re-reading the
  file would ask the same question twice, so a parked task archives to
  ``.processed/`` exactly like a success — only a genuine failure (or an
  orphaned claim from a dead session) lands in ``.failed/``.
* **A writable-by-everyone folder is a remote shell, not autonomy.** Anyone
  who can write the inbox can command the agent, so a group- or
  world-writable directory refuses to run at all, loudly, before any file is
  read. File contents are screen-grade untrusted text: they pass the same
  control-character sanitiser as observed screen text, because a task file
  that forges the prompt's structure is a prompt injection with a head start.

Pure parsing throughout; the I/O shell (scan, claim, settle, sweep) is thin
and raises typed errors instead of returning ambiguous empties.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from computeruse.orchestrator.untrusted import sanitize_observed_text

LOGGER: Final = logging.getLogger(__name__)

#: Suffixes the inbox accepts. Everything else is ignored, not failed: the
#: folder is the operator's desk, and a screenshot or a lockfile on it is not
#: a broken task.
INBOX_SUFFIXES: Final[tuple[str, ...]] = (".txt", ".md", ".json")

#: Largest task file the inbox reads. A goal feeds a language model, so the
#: useful bound is what fits a prompt — and an unbounded read lets one stray
#: dump exhaust the context a run needs for the actual work.
MAX_INBOX_FILE_BYTES: Final[int] = 64 * 1024

#: Longest goal handed to a run. The archived file keeps the full text, but
#: the proposal itself must stay a proposal rather than a transcript pasted
#: into every prompt of the run.
MAX_GOAL_CHARS: Final[int] = 2000

#: Failure sidecars are capped for the same reason: an unbounded message
#: (a pathological traceback) must not become an unbounded file.
MAX_REASON_CHARS: Final[int] = 2000

#: Sibling directories of the inbox root. Dot-prefixed, so the single
#: dotfile rule keeps them (and their contents) out of every scan — the
#: agent's own archiving can never re-trigger itself.
PROCESSING_DIRNAME: Final[str] = ".processing"
PROCESSED_DIRNAME: Final[str] = ".processed"
FAILED_DIRNAME: Final[str] = ".failed"

#: What a failure note beside an archived file is called.
REASON_SUFFIX: Final[str] = ".reason.txt"


class InboxError(RuntimeError):
    """The inbox could not be used (missing folder, unreadable file)."""


class InboxRefusedError(InboxError):
    """The inbox folder is unsafe to read from (writable by group/others)."""


class InvalidInboxFileError(InboxError):
    """One task file could not become a goal; ``reason`` says why."""

    def __init__(self, *, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class InboxJsonGoal(BaseModel):
    """The strict shape of a ``.json`` task file.

    A Pydantic model, not a loose dict: a missing ``goal``, a non-string
    ``app``, or an unknown extra key fails loudly into ``.failed/`` rather
    than running as a goal nobody wrote down carefully.
    """

    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1)
    app: str | None = None


@dataclass(frozen=True)
class InboxFile:
    """One scannable task file (pure data)."""

    name: str
    suffix: str
    size_bytes: int


@dataclass(frozen=True)
class InboxTask:
    """A parsed goal, ready to propose (pure data)."""

    goal: str
    app: str | None
    source_name: str


@dataclass(frozen=True)
class ClaimedTask:
    """A task owned by this process: the file already sits in ``.processing/``."""

    task: InboxTask
    processing_path: Path


def is_group_or_world_writable(mode: int) -> bool:
    """Whether a permission mode lets anyone beyond the owner write (pure)."""
    return bool(mode & 0o022)


def check_inbox_writable(directory: Path) -> None:
    """Refuse an inbox anyone else can write to (I/O shell).

    Also refuses anything that is not a directory at all: without this a
    ``--watch`` pointing at a file passed the stat, then died later with a
    raw ``NotADirectoryError`` while creating ``.processing/`` — a typed
    boundary must not leak past itself.

    Raises :class:`InboxRefusedError` naming the path and its mode: whoever
    can write the folder can command the agent, and that fact must be said
    plainly, not hidden behind a generic permission error.
    """
    try:
        folder_stat = os.stat(directory)
    except OSError as exc:
        raise InboxError(f"inbox directory {directory} cannot be used: {exc}") from exc
    if not stat.S_ISDIR(folder_stat.st_mode):
        raise InboxError(
            f"inbox {directory} is not a directory "
            "(--watch needs a folder to claim task files from, not a file)"
        )
    mode = stat.S_IMODE(folder_stat.st_mode)
    if is_group_or_world_writable(mode):
        raise InboxRefusedError(
            f"inbox directory {directory} is writable by group/others "
            f"(mode {mode:04o}); anyone who can write it can command the "
            "agent — tighten the permissions or choose another folder"
        )


def scan_inbox(directory: Path) -> tuple[InboxFile, ...]:
    """List the claimable task files at the inbox root (I/O shell).

    Non-recursive, dotfiles skipped, symlinks refused, unknown suffixes and
    oversized files ignored (they are not broken tasks, just not tasks).
    Sorted by name so concurrent claimants drain the folder deterministically.
    A missing or unreadable folder raises :class:`InboxError` — the operator
    pointed at something that is not a folder, and silence would read as an
    empty night.
    """
    try:
        entries = sorted(directory.iterdir(), key=lambda entry: entry.name)
    except OSError as exc:
        raise InboxError(f"inbox directory {directory} cannot be read: {exc}") from exc
    found: list[InboxFile] = []
    for entry in entries:
        if entry.name.startswith("."):
            continue
        if entry.is_symlink() or not entry.is_file():
            continue
        if entry.suffix.lower() not in INBOX_SUFFIXES:
            continue
        try:
            size = entry.stat().st_size
        except OSError:
            # Vanished (or unreadable) between listing and stat: a concurrent
            # claimant won it, or it was never really there. Skip, don't fail.
            continue
        if size > MAX_INBOX_FILE_BYTES:
            LOGGER.warning(
                "inbox: ignoring %s (%d bytes exceeds %d)",
                entry.name,
                size,
                MAX_INBOX_FILE_BYTES,
            )
            continue
        found.append(InboxFile(name=entry.name, suffix=entry.suffix.lower(), size_bytes=size))
    return tuple(found)


def parse_claimed_file(path: Path) -> InboxTask:
    """Parse an already-claimed file into a goal (I/O shell over pure parse).

    Reading from the ``.processing/`` path (not the inbox root) is what makes
    the content stable: the rename already moved it out of everyone's reach,
    so what is parsed is what was claimed.
    """
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise InboxError(f"claimed inbox file {path} cannot be read: {exc}") from exc
    if len(raw) > MAX_INBOX_FILE_BYTES:
        raise InvalidInboxFileError(
            reason=f"{path.name} grew past {MAX_INBOX_FILE_BYTES} bytes before it could be read"
        )
    return _parse_bytes(raw, source_name=path.name)


def claim_next_task(directory: Path, *, now: float, pid: int) -> ClaimedTask | None:
    """Claim the next task file, or ``None`` when the folder holds none.

    The writability refusal runs first, before anything is created or moved:
    an unsafe folder must not even get its bookkeeping directories. Invalid
    files are archived to ``.failed/`` as they are met (with their reason),
    so one malformed file can never wedge the folder — and a lost atomic race
    (``FileNotFoundError``) steps to the next file instead of failing, which
    is the whole point of claiming by rename.
    """
    check_inbox_writable(directory)
    processing = _ensure_dir(directory / PROCESSING_DIRNAME)
    failed = _ensure_dir(directory / FAILED_DIRNAME)
    for entry in scan_inbox(directory):
        target = _claim_path(processing, entry.name, now=now, pid=pid)
        try:
            os.rename(directory / entry.name, target)
        except FileNotFoundError:
            continue
        try:
            task = parse_claimed_file(target)
        except InvalidInboxFileError as exc:
            # Said out loud, not just archived: the operator dropped a file
            # expecting work, and silence about where it went would read as
            # the agent ignoring them.
            LOGGER.warning(
                "inbox: %s is unusable, archived to .failed/ (%s)",
                entry.name,
                exc.reason,
            )
            settle_failed(target, failed_dir=failed, reason=exc.reason)
            continue
        # The proposal cites the name the operator dropped, not the claim
        # ticket the rename minted: "task file 'notes.txt'" is addressed to
        # a person, "1700000000_4242_notes.txt" is addressed to nobody.
        return ClaimedTask(
            task=replace(task, source_name=entry.name), processing_path=target
        )
    return None


def settle_processed(processing_path: Path, *, processed_dir: Path) -> Path:
    """Archive a finished (or parked) claim to ``.processed/`` (I/O shell)."""
    _ensure_dir(processed_dir)
    target = _unique_path(processed_dir, processing_path.name)
    os.rename(processing_path, target)
    return target


def settle_failed(processing_path: Path, *, failed_dir: Path, reason: str) -> Path:
    """Archive a failed claim to ``.failed/`` with its reason beside it."""
    _ensure_dir(failed_dir)
    target = _unique_path(failed_dir, processing_path.name)
    os.rename(processing_path, target)
    target.with_name(target.name + REASON_SUFFIX).write_text(
        reason[:MAX_REASON_CHARS], encoding="utf-8"
    )
    return target


def sweep_processing(directory: Path, *, reason: str) -> tuple[str, ...]:
    """Quarantine claims a suddenly-dead session left behind (I/O shell, run once).

    Only sudden death orphans: every orderly ending settles its own claim —
    success and parked runs archive to ``.processed/``, and even a
    kill-switch takeover or an exhausted budget passes through the runner's
    failure path into ``.failed/`` with its reason. A claim still sitting in
    ``.processing/`` at session start therefore belongs to a process that
    died without unwinding, and neither re-running it silently (repeating
    unknown work) nor dropping it silently (losing work without a trace) is
    honest. ``.failed/`` with the reason written down is the middle that
    keeps both promises: nothing is lost, and nothing runs twice.
    """
    processing = directory / PROCESSING_DIRNAME
    if not processing.is_dir():
        return ()
    failed = _ensure_dir(directory / FAILED_DIRNAME)
    moved: list[str] = []
    for child in sorted(processing.iterdir(), key=lambda entry: entry.name):
        if child.name.startswith(".") or child.is_symlink() or not child.is_file():
            continue
        settle_failed(child, failed_dir=failed, reason=reason)
        moved.append(child.name)
    return tuple(moved)


@dataclass(frozen=True)
class FailedInboxItem:
    """One quarantined task file and why it is there (pure data)."""

    name: str
    #: The archived reason, or ``None`` when its sidecar is missing or
    #: unreadable — the quarantine itself is the fact, the reason a bonus.
    reason: str | None


@dataclass(frozen=True)
class InboxCounts:
    """What a watched folder holds right now (pure data, no rendering).

    Counts, not contents: the report needs to know how much sits where, and
    only the failures need their whys. ``failed`` runs newest first, so the
    freshest unanswered question is the first line a person reads.
    """

    watch_dir: str
    processed: int
    failed: tuple[FailedInboxItem, ...]
    orphaned: int


def count_inbox(directory: Path) -> InboxCounts:
    """Count a watched folder's archives for the report (I/O shell).

    A missing or unreadable folder raises :class:`InboxError` rather than
    reporting zeros: zeros would claim the night was quiet while the operator
    mistyped the path. Dotfiles and reason sidecars are bookkeeping, never
    tasks, and are not counted.
    """
    if not directory.is_dir():
        raise InboxError(
            f"inbox {directory} is not a readable directory "
            "(--report --watch needs the folder a session watched)"
        )
    processed = _count_archive(directory / PROCESSED_DIRNAME)
    failed_dir = directory / FAILED_DIRNAME
    failed = _read_failures(failed_dir)
    orphaned = _count_archive(directory / PROCESSING_DIRNAME)
    return InboxCounts(
        watch_dir=str(directory),
        processed=processed,
        failed=failed,
        orphaned=orphaned,
    )


def _count_archive(archive: Path) -> int:
    """How many archived files sit in ``archive`` (I/O shell)."""
    if not archive.is_dir():
        return 0
    count = 0
    for child in archive.iterdir():
        if child.name.startswith(".") or child.is_symlink() or not child.is_file():
            continue
        if child.name.endswith(REASON_SUFFIX):
            continue
        count += 1
    return count


def _read_failures(failed_dir: Path) -> tuple[FailedInboxItem, ...]:
    """Quarantined files with their reasons, newest first (I/O shell)."""
    if not failed_dir.is_dir():
        return ()
    found: list[tuple[int, str, str | None]] = []
    for child in failed_dir.iterdir():
        if child.name.startswith(".") or child.is_symlink() or not child.is_file():
            continue
        if child.name.endswith(REASON_SUFFIX):
            continue
        try:
            mtime = child.stat().st_mtime_ns
        except OSError:
            mtime = 0
        sidecar = child.with_name(child.name + REASON_SUFFIX)
        try:
            reason = sidecar.read_text(encoding="utf-8").strip() or None
        except OSError:
            reason = None
        found.append((mtime, child.name, reason))
    found.sort(key=lambda item: (-item[0], item[1]))
    return tuple(FailedInboxItem(name=name, reason=reason) for _, name, reason in found)


def _parse_bytes(raw: bytes, *, source_name: str) -> InboxTask:
    """Bytes on disk to a goal (pure).

    The goal text is file-supplied and therefore untrusted: it passes the
    control-character sanitiser so a task file cannot forge the prompt's
    structure, and it is truncated so one file cannot eat the run's context.
    """
    suffix = Path(source_name).suffix.lower()
    if suffix == ".json":
        return _parse_json(raw, source_name=source_name)
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        raise InvalidInboxFileError(reason=f"{source_name} holds no goal text")
    return InboxTask(
        goal=_truncate(sanitize_observed_text(text)),
        app=None,
        source_name=source_name,
    )


def _parse_json(raw: bytes, *, source_name: str) -> InboxTask:
    """A ``.json`` task file to a goal (pure)."""
    try:
        payload: object = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise InvalidInboxFileError(
            reason=f"{source_name} is not valid JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise InvalidInboxFileError(
            reason=f"{source_name} must hold a JSON object with 'goal' (and optional 'app')"
        )
    try:
        parsed = InboxJsonGoal.model_validate(payload)
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else None
        detail = (
            f"{'.'.join(str(part) for part in first['loc'])}: {first['msg']}"
            if first is not None
            else "schema mismatch"
        )
        raise InvalidInboxFileError(
            reason=f"{source_name} does not match the task schema ({detail})"
        ) from exc
    goal = sanitize_observed_text(parsed.goal.strip())
    if not goal:
        raise InvalidInboxFileError(reason=f"{source_name} holds an empty goal")
    app = parsed.app.strip() if parsed.app is not None else None
    return InboxTask(
        goal=_truncate(goal),
        app=sanitize_observed_text(app) if app else None,
        source_name=source_name,
    )


def _truncate(text: str, *, limit: int = MAX_GOAL_CHARS) -> str:
    """Shorten a goal to what a run's prompts can carry (pure)."""
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def _ensure_dir(path: Path) -> Path:
    """Create a bookkeeping directory if needed, returning it (I/O shell)."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def _claim_path(processing: Path, name: str, *, now: float, pid: int) -> Path:
    """A collision-free claim name: timestamp, owner, original name (pure)."""
    base = f"{int(now)}_{pid}_{name}"
    return _unique_path(processing, base)


def _unique_path(directory: Path, name: str) -> Path:
    """``directory/name``, suffixed until it names nothing (I/O shell).

    A rename onto an existing file would silently replace it on POSIX —
    archiving must never delete, so a repeat name earns ``_1``, ``_2``.
    """
    candidate = directory / name
    counter = 0
    while candidate.exists():
        counter += 1
        candidate = directory / f"{Path(name).stem}_{counter}{Path(name).suffix}"
    return candidate
