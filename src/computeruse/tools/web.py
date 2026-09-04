"""Page reading, over the standard library only.

This project carries exactly one runtime dependency, and the reason shows up
here: an agent that drives a real desktop is already asking a lot of the
machine it runs on, and every extra package is another thing that can fail to
install, pin badly, or pull a transitive surprise onto a user's laptop. The
OpenAI transport is hand-rolled over ``urllib`` for the same reason, so these
follow it rather than reaching for ``httpx`` and ``trafilatura``.

Web search used to go through a local SearXNG instance. That dependency is
gone: requiring Docker or a service on 127.0.0.1:8888 turned every lookup
into a connection failure, and the failure looped against the browser. Search
now resolves through a connected MCP search tool (Tavily, Exa, Brave) via the
``web_search`` bridge in the loop, or — when none is configured — through the
real browser on screen. This module keeps only the page reader (``fetch_page``,
exposed as ``web_fetch``), which has nothing to do with search.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Final

LOGGER: Final = logging.getLogger(__name__)

MAX_PAGE_CHARS: Final[int] = 20_000
#: Below this many characters, an extract is not a short page — it is a page
#: whose text never arrived. Measured against real sites: Wikipedia yields
#: 20,000 characters and Hacker News 3,793, while Reddit yields 6, because its
#: markup is a shell that JavaScript fills in later. Returning those 6
#: characters as though they were the article is the worst possible answer, so
#: the floor exists to turn a silent wrong result into a usable one.
MIN_PAGE_CHARS: Final[int] = 200
REQUEST_TIMEOUT_SECONDS: Final[float] = 20.0

#: Identify honestly. An agent that hides what it is gets treated as a scraper,
#: and rightly so.
USER_AGENT: Final[str] = "computeruse/1.0 (+https://github.com/senoldogann/computer-use)"


class WebError(RuntimeError):
    """A web tool could not answer.

    Raised rather than returning an empty result: "the fetch found nothing"
    and "the fetch never ran" lead the agent to opposite next moves, and
    collapsing them hides the difference at exactly the moment it matters.
    """


def fetch_page(url: str, *, max_chars: int = MAX_PAGE_CHARS) -> str:
    """Fetch a page and return its readable text.

    Deliberately a plain extraction rather than a readability heuristic: the
    caller is a language model that can ignore navigation chrome, and a
    dependency that guesses which ``<div>`` is the article is a large amount of
    machinery to install for a judgement the model already makes.
    """
    if not _is_http_url(url):
        raise WebError(f"refusing to fetch a non-HTTP(S) URL: {url!r}")
    if not _is_fetchable_url(url):
        raise WebError(f"refusing to fetch an internal-only URL: {url!r}")
    html = _get(url)
    text = html_to_text(html)
    if len(text) < MIN_PAGE_CHARS:
        raise WebError(
            f"{url} returned almost no readable text ({len(text)} characters) — its "
            "content is built by JavaScript that this fetch does not run. Open the "
            "page on screen and read it there instead; that is what the browser is for."
        )
    return text[:max_chars]


def html_to_text(html: str) -> str:
    """Strip markup, scripts and styles down to readable text (pure).

    Script and style bodies are removed *before* tags, or their contents
    survive as a wall of JavaScript that would otherwise dominate the extract
    and crowd out the page's actual words.
    """
    without_code = re.sub(
        r"<(script|style|noscript)\b[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE
    )
    # Block-level boundaries become newlines so paragraphs survive as
    # paragraphs; everything else collapses to spaces.
    with_breaks = re.sub(
        r"</(p|div|section|article|li|tr|h[1-6]|br)\s*>", "\n", without_code, flags=re.IGNORECASE
    )
    stripped = re.sub(r"<[^>]+>", " ", with_breaks)
    unescaped = _unescape(stripped)
    lines = [_collapse(line) for line in unescaped.splitlines()]
    return "\n".join(line for line in lines if line)


def _get(url: str) -> str:
    """One GET, decoded as text, with typed failures and transient retry."""
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            context = ssl.create_default_context()
            with urllib.request.urlopen(
                request, timeout=REQUEST_TIMEOUT_SECONDS, context=context
            ) as response:
                raw = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
            return raw.decode(charset, errors="replace")
        except urllib.error.HTTPError as exc:
            # Retry transient server errors, not client errors.
            if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(0.5 * (2**attempt))
                last_error = exc
                continue
            raise WebError(f"HTTP {exc.code} from {url}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(0.5 * (2**attempt))
                continue
            raise WebError(f"could not reach {url}: {exc}") from exc
    assert last_error is not None
    raise WebError(f"could not reach {url}: {last_error}") from last_error


def _is_blocked_host(host: str) -> bool:
    """True when a hostname must never be fetched (SSRF guard, pure)."""

    lowered = host.lower().strip().rstrip(".")
    if lowered in ("localhost", "metadata.google.internal"):
        return True
    try:
        addr = ipaddress.ip_address(lowered)
    except ValueError:
        return False
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
    )


def _is_http_url(url: str) -> bool:
    """Only http(s) to public hosts. ``file:`` would turn a web tool into a
    filesystem read the user never authorised; loopback/link-local/private
    ranges and cloud metadata would turn it into internal network access (pure)."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return False
    return parsed.scheme in ('http', 'https') and bool(parsed.netloc)


def _is_fetchable_url(url: str) -> bool:
    """Can fetch_page retrieve this URL (SSRF guard, pure).

    _is_http_url answers scheme only; this answers safety: loopback /
    private / link-local / reserved hosts and cloud metadata are refused
    for model-supplied page fetches.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return False
    host = parsed.hostname or ''
    if not host:
        return False
    return not _is_blocked_host(host)


def _collapse(text: str) -> str:
    """Whitespace runs to single spaces (pure)."""
    return re.sub(r"\s+", " ", text).strip()


def _unescape(text: str) -> str:
    """Resolve HTML entities (pure)."""
    from html import unescape

    return unescape(text)
