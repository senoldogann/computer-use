"""Shared subprocess bridge for subscription-CLI model transports (Law 6 shell).

computeruse's `--model` seam speaks ``str -> str``; ChatGPT-Codex, Claude and
OpenCode subscriptions speak headless CLI. This module owns everything the
three transports share and nothing they don't: binary discovery, bounded
process execution with group-kill on timeout, API-key env scrubbing, temp
image files, and JSONL stream parsing. Per-CLI argv shapes and output parsing
live in the transport modules — a flag that differs per CLI must not become
a branch in shared code.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final

LOGGER: Final = logging.getLogger(__name__)


class CliBridgeError(RuntimeError):
    """A subscription-CLI model call failed; carries the CLI's own reason."""


@dataclass(frozen=True)
class CliResult:
    """What a finished child process left behind (pure data)."""

    returncode: int
    stdout: str
    stderr: str
    elapsed_s: float


def find_binary(name: str) -> str:
    """Resolve a CLI binary or fail with an install hint (no guessing).

    A missing binary is a setup problem, not a transient one: retrying it
    would burn OODA turns on an answer that cannot change.
    """
    path = shutil.which(name)
    if path is None:
        raise CliBridgeError(
            f"the {name!r} CLI is not on PATH, so the {name} subscription "
            f"cannot be used; install it and sign in ({name} login), then retry"
        )
    return path


def scrubbed_env(scrub_keys: tuple[str, ...]) -> dict[str, str]:
    """Child environment with API-key variables removed (pure).

    Precedence matters: ``ANTHROPIC_API_KEY`` outranks a Claude subscription
    login, and an OpenAI API key outranks a ChatGPT login. A key exported
    months ago for an unrelated script would silently move billing from the
    subscription the operator asked for to metered API — so the child that
    must bill the subscription never inherits the keys. Long-lived
    subscription tokens (``CLAUDE_CODE_OAUTH_TOKEN``) are NOT scrubbed: they
    *are* the subscription.
    """
    return {
        key: value for key, value in os.environ.items() if key not in scrub_keys
    }


def run_cli(
    argv: list[str],
    stdin_text: str | None,
    timeout_seconds: float,
    *,
    scrub_keys: tuple[str, ...],
    cwd: str | None,
) -> CliResult:
    """Run one headless CLI call, bounded in time (shell).

    ``argv`` is a list, never a shell string: the prompt travels via stdin or
    argv without a shell ever parsing it. On timeout the whole process group
    is killed — an agent CLI spawns sandbox helpers, and killing only the
    parent orphans them holding the machine. Anything the child says on
    stderr is kept (bounded) because a quota refusal or a missing login
    arrives there, and the operator needs the CLI's own words.
    """
    started = time.perf_counter()
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE if stdin_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            cwd=cwd,
            env=scrubbed_env(scrub_keys),
        )
    except OSError as exc:
        raise CliBridgeError(
            f"could not start {argv[0]!r}: {exc}; reinstall or fix PATH"
        ) from exc
    try:
        stdout, stderr = process.communicate(
            input=stdin_text, timeout=timeout_seconds
        )
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass
        process.wait()
        raise CliBridgeError(
            f"{argv[0]!r} did not answer within {timeout_seconds:.0f}s and "
            "was killed; the subscription turn it started may still count "
            "against quota"
        ) from exc
    elapsed_s = time.perf_counter() - started
    return CliResult(
        returncode=process.returncode,
        stdout=stdout or "",
        stderr=(stderr or "")[-2000:],
        elapsed_s=elapsed_s,
    )


def usage_int(value: object) -> int:
    """A token count that tolerates proxies sending strings (pure).

    Shared by every subscription transport: usage blocks disagree on types
    (ints here, numeric strings there), and each transport re-implementing
    the coercion is how three copies drift. Booleans are never counts.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, str):
        try:
            return max(0, int(value))
        except ValueError:
            return 0
    return 0


def iter_jsonl(stdout: str, *, source: str) -> Iterator[dict[str, object]]:
    """Yield parsed JSON objects from a JSONL stream (pure).

    Blank lines are skipped; a non-blank line that is not a JSON object is
    terminal — silently skipping it could skip the final answer itself, and
    a stream that cannot be understood is exactly what nothing should be
    inferred from.
    """
    for lineno, line in enumerate(stdout.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CliBridgeError(
                f"{source} emitted a non-JSON line {lineno}: "
                f"{line[:200]} ({exc})"
            ) from exc
        if not isinstance(value, dict):
            raise CliBridgeError(
                f"{source} emitted a non-object JSON line {lineno}: {line[:200]}"
            )
        yield value


class TempPng:
    """A base64 screenshot staged as a 0600 temp file (shell).

    Agent CLIs attach images by path, not by bytes. The file lives only for
    the call: screenshots show the operator's screen, so it is created
    owner-only and removed afterwards, whatever happens.
    """

    def __init__(self, image_b64: str, *, source: str) -> None:
        self._source = source
        try:
            raw = base64.b64decode(image_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise CliBridgeError(
                f"{source}: cannot stage the screenshot, it is not valid "
                f"base64 ({exc})"
            ) from exc
        if not raw.startswith(b"\x89PNG"):
            raise CliBridgeError(
                f"{source}: staged screenshots must be PNG, refusing to hand "
                "an unexpected format to another program"
            )
        with tempfile.NamedTemporaryFile(
            suffix=".png", prefix="computeruse-frame-", delete=False
        ) as handle:
            handle.write(raw)
            staged = handle.name
        Path(staged).chmod(0o600)
        self._path = staged

    @property
    def path(self) -> str:
        return self._path

    def close(self) -> None:
        try:
            Path(self._path).unlink(missing_ok=True)
        except OSError as exc:
            LOGGER.warning("could not remove staged screenshot: %s", exc)

    def __enter__(self) -> str:
        return self._path

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.close()


Runner = Callable[
    [list[str], str | None, float],
    CliResult,
]
"""Injectable process runner (argv, stdin, timeout) for tests.

The ``scrub_keys``/``cwd`` policy belongs to each transport, so the seam
takes only what varies per call — mirroring ``http_open`` in the OpenAI
transport.
"""
