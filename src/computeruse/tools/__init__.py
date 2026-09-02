"""Capabilities the agent uses without touching the host's input devices.

Everything here reaches the world without a cursor move or a focus change, so
it can run while the user works and needs none of the verification machinery
that exists because a synthetic click may land on the wrong window.
"""

from computeruse.tools.web import SearchResult, WebError, fetch_page, search_web

__all__ = ["SearchResult", "WebError", "fetch_page", "search_web"]
