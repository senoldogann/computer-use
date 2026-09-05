"""Durable file replacement for the JSON stores (Law 4: memory must survive).

A plain ``write_text`` opens the file, truncates it, and only then writes. A
run interrupted in that window — Ctrl-C, the kill switch, a budget stop, a
crash — leaves a truncated JSON document behind, and every later read of that
store fails on it. The stores are exactly what the agent is supposed to carry
across runs, so losing one to a keystroke is losing the point.

Writing to a temporary sibling and renaming closes the window: ``os.replace``
is atomic on POSIX, so a reader sees either the previous content or the new
one and never a splice of the two.
"""

from __future__ import annotations

import os
from pathlib import Path


def write_atomic(path: Path, text: str) -> None:
    """Replace ``path``'s contents with ``text`` atomically (I/O shell).

    The temporary file is a hidden sibling so a directory scan that lists
    ``*.json`` never sees a half-written one, and it is removed on failure so
    a raising write cannot leave litter behind for the next run to puzzle over.
    """
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
