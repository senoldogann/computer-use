"""Web search and page reading — the capabilities that need no cursor.

These are the only actions that reach outside the machine, so the tests are
about the two things that go wrong there: a page's markup drowning its text,
and a failure being mistaken for an empty answer.
"""

from __future__ import annotations

import json

import pytest

from computeruse.orchestrator.loop import _route_for
from computeruse.orchestrator.schemas import WebFetch, WebSearch
from computeruse.tools.web import (
    SearchResult,
    WebError,
    _is_http_url,
    html_to_text,
    search_web,
)


def test_web_tools_never_reach_the_driver() -> None:
    """They touch no input device, so no coordinate gate or focus guard applies."""
    assert _route_for(WebSearch(type="web_search", query="x")) == "internal_tool"
    assert _route_for(WebFetch(type="web_fetch", url="https://example.com")) == "internal_tool"


def test_scripts_and_styles_never_reach_the_model() -> None:
    """Their bodies survive naive tag-stripping and would drown the page.

    A minified bundle inlined in <script> is often larger than the article
    around it, so removing the elements before the tags is the difference
    between an extract and a wall of JavaScript.
    """
    html = (
        "<html><head><style>a{color:red}</style>"
        "<script>var x=1;function f(){return 2}</script></head>"
        "<body><h1>Headline</h1><p>First &amp; second.</p><div>Third</div></body></html>"
    )
    text = html_to_text(html)
    assert text == "Headline\nFirst & second.\nThird"
    assert "color:red" not in text and "var x" not in text


def test_block_boundaries_survive_as_line_breaks() -> None:
    """Paragraphs run together are far harder to read than paragraphs."""
    assert html_to_text("<p>one</p><p>two</p>") == "one\ntwo"
    assert html_to_text("<li>a</li><li>b</li>") == "a\nb"


def test_only_http_urls_are_fetchable() -> None:
    """A web tool that follows file: is a filesystem read nobody authorised."""
    assert _is_http_url("https://example.com/x") is True
    assert _is_http_url("http://127.0.0.1:8888/search") is True
    assert _is_http_url("file:///etc/passwd") is False
    assert _is_http_url("ftp://example.com") is False
    assert _is_http_url("not a url") is False


def test_an_unreachable_search_raises_rather_than_returning_nothing() -> None:
    """"Found nothing" and "never ran" lead to opposite next moves.

    Collapsing them into an empty list hides the difference exactly where it
    decides whether the agent should rephrase or fall back to the browser.
    """
    with pytest.raises(WebError) as caught:
        search_web("anything", limit=1)
    # The message names the address, because the usual cause is that nothing
    # is listening there yet.
    assert "SearXNG" in str(caught.value)


def test_empty_queries_are_refused_before_any_request() -> None:
    with pytest.raises(WebError):
        search_web("   ")


def test_a_result_renders_as_one_compact_line() -> None:
    hit = SearchResult(title="Title", url="https://example.com", snippet="A snippet")
    assert hit.render() == "Title <https://example.com> — A snippet"
    bare = SearchResult(title="Title", url="https://example.com", snippet="")
    assert bare.render() == "Title <https://example.com>"


def test_search_results_parse_from_a_searxng_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """The parser must survive the entries SearXNG actually returns."""
    body = json.dumps(
        {
            "results": [
                {"title": "First", "url": "https://a.example", "content": "  spaced   out  "},
                {"url": "https://b.example"},          # no title: falls back to the URL
                {"title": "No URL"},                    # unusable, skipped
                "not an object",                        # skipped
            ]
        }
    )
    monkeypatch.setattr("computeruse.tools.web._get", lambda _url: body)
    hits = search_web("q", limit=8)
    assert [h.url for h in hits] == ["https://a.example", "https://b.example"]
    assert hits[0].snippet == "spaced out"
    assert hits[1].title == "https://b.example"
