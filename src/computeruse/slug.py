"""Identifier slugs for the stores that name files after human text.

The skill store and the semantic store both name a file after a slug of
whatever the run was about, and both validate that name against
``^[a-z0-9][a-z0-9._-]*$``. Building the slug with :meth:`str.isalnum` looks
right and is not: Python's ``isalnum`` is Unicode-aware, so it keeps ``ü``,
``ç`` and ``ğ``. Every run with a Turkish goal therefore did its work and then
died writing down what it had learned, because the id it built could not pass
the pattern that names its own file — observed on a live run that had already
pasted its summary into Notes.

Folding to ASCII rather than deleting the letters keeps the slug readable, and
the slug is a retrieval key, so readability is not cosmetic.
"""

from __future__ import annotations

import unicodedata
from typing import Final

#: Letters no decomposition reaches. Turkish dotless i is the one that matters
#: here; it has no combining form to strip, so without this it would vanish and
#: take the syllable with it ("ışık" -> "-s-k").
_FOLDED: Final[dict[str, str]] = {"ı": "i", "ß": "ss", "ø": "o", "đ": "d", "ł": "l"}


def ascii_slug(text: str, *, max_chars: int) -> str:
    """Lowercase ASCII slug of ``text``, or "" when nothing survives (pure).

    Guaranteed to satisfy ``^[a-z0-9][a-z0-9._-]*$`` when non-empty: separators
    are collapsed to single hyphens and trimmed from both ends, so the first
    character is always alphanumeric.
    """
    folded = "".join(_FOLDED.get(ch, ch) for ch in text.lower())
    decomposed = unicodedata.normalize("NFKD", folded)
    kept: list[str] = []
    for ch in decomposed:
        if unicodedata.combining(ch):
            continue
        kept.append(ch if ch.isascii() and ch.isalnum() else "-")
    slug = "".join(kept).strip("-")[:max_chars].strip("-")
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug
