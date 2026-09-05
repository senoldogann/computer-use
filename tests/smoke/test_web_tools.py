"""Web tools — page reading plus the web_search MCP bridge.

SearXNG is gone: no local service, no Docker requirement, no URL to configure.
``web_search`` now bridges to a connected MCP search tool (Tavily, Exa, Brave)
or answers with browser instructions, so these tests pin the two things that
go wrong there: a page's markup drowning its text, and a search looping
against a missing service instead of reaching the browser.
"""

from __future__ import annotations

import pytest

from computeruse.mcp import McpRegistry, ToolCallOutcome
from computeruse.mcp.protocol import McpTool
from computeruse.orchestrator.loop import (
    NO_MCP_SEARCH_FALLBACK,
    OodaRunner,
    WorkingState,
    _is_mcp_search_tool,
    _mcp_search_candidates,
    _route_for,
    _search_arguments_for,
)
from computeruse.orchestrator.prompts import ACTION_CONTRACT, decision_prompt
from computeruse.orchestrator.schemas import AgentTurn, WebFetch, WebSearch
from computeruse.tools.web import (
    MIN_PAGE_CHARS,
    WebError,
    _is_fetchable_url,
    _is_http_url,
    html_to_text,
)


def _turn(action: object) -> AgentTurn:
    return AgentTurn.model_validate({"thought": "", "sub_goal": "", "action": action})


def _runner(mcp: McpRegistry | None = None) -> OodaRunner:
    return OodaRunner(
        provider=lambda state: _turn(WebFetch(type="web_fetch", url="https://example.com")),
        execute_physical=lambda _action: None,
        mcp=mcp,
        max_steps=3,
    )


def _search_tool(
    server: str = "tavily",
    name: str = "search",
    description: str = "Web search",
    schema: dict[str, object] | None = None,
) -> McpTool:
    return McpTool(
        server=server,
        name=name,
        description=description,
        input_schema=schema
        if schema is not None
        else {"properties": {"query": {"type": "string"}}, "required": ["query"]},
    )


def _registry_with(
    tools: list[McpTool],
    results: dict[str, ToolCallOutcome] | None = None,
    record: list[tuple[str, dict[str, object]]] | None = None,
) -> McpRegistry:
    registry = McpRegistry(())
    for tool in tools:
        registry._tools[tool.qualified_name] = tool
    outcomes = results or {}

    def fake_call(
        qualified_name: str,
        arguments: dict[str, object],
        *,
        timeout: float = 30.0,
    ) -> ToolCallOutcome:
        if record is not None:
            record.append((qualified_name, dict(arguments)))
        if qualified_name in outcomes:
            return outcomes[qualified_name]
        return ToolCallOutcome(text="result for " + str(arguments), failed=False)

    registry.call = fake_call  # type: ignore[method-assign]
    return registry


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
    assert _is_http_url("http://127.0.0.1:9/search") is True
    assert _is_http_url("file:///etc/passwd") is False
    assert _is_http_url("ftp://example.com") is False
    assert _is_http_url("not a url") is False


def test_a_javascript_shell_fails_loudly_instead_of_returning_nothing() -> None:
    """Six characters returned as an article is the worst possible answer.

    Measured against real sites: Wikipedia yields 20,000 characters and Hacker
    News 3,793, while Reddit yields 6 — its markup is a shell that JavaScript
    fills in later. The agent has a browser and eyes; it only needs to be told
    that this page requires them.
    """
    shell = "<html><head><title>Reddit</title></head><body><div id='root'></div></body></html>"
    with pytest.raises(WebError) as caught:
        _fetch_with(shell, "https://www.reddit.com/r/swift/")
    message = str(caught.value)
    assert "JavaScript" in message
    # It must say what to do instead, not merely that it failed.
    assert "read it there" in message or "on screen" in message


def test_a_server_rendered_page_is_returned_whole() -> None:
    article = "<html><body><p>" + ("word " * 200) + "</p></body></html>"
    text = _fetch_with(article, "https://example.com/article")
    assert len(text) > MIN_PAGE_CHARS


def _fetch_with(html: str, url: str) -> str:
    """fetch_page against a fixed body, so the test needs no network."""
    from computeruse.tools import web

    original = web._get
    web._get = lambda _u: html  # type: ignore[assignment]
    try:
        return web.fetch_page(url)
    finally:
        web._get = original  # type: ignore[assignment]


# --- web_search MCP bridge ----------------------------------------------------


def test_web_search_without_mcp_returns_browser_fallback_not_an_error() -> None:
    """No local service to fail against: the answer names the browser.

    This is the reported field failure — Google results on screen, the model
    emitting web_search, the loop dialling 127.0.0.1:8888 until the budget
    ran out. There is nothing to dial any more.
    """
    runner = _runner(mcp=None)
    answer = runner._run_tool(WebSearch(type="web_search", query="latest AI news"))
    assert NO_MCP_SEARCH_FALLBACK in answer
    assert "Google Chrome" in answer
    assert "web_search ile tekrarlamay" in answer


def test_web_search_without_mcp_blocks_repetition() -> None:
    """Consecutive searches without an MCP tool must not loop forever."""
    runner = _runner(mcp=None)
    action = WebSearch(type="web_search", query="same query")
    runner._run_tool(action)
    runner._run_tool(action)
    blocked = runner._run_tool(action)
    assert "engellendi" in blocked
    assert "tekrarlayamazsınız" in blocked


def test_web_search_bridges_query_to_the_mcp_search_tool() -> None:
    """With Tavily/Exa/Brave connected, the query is forwarded automatically."""
    seen: list[tuple[str, dict[str, object]]] = []
    registry = _registry_with(
        [_search_tool(server="tavily")],
        {"tavily.search": ToolCallOutcome(text="three hits", failed=False)},
        record=seen,
    )
    runner = _runner(mcp=registry)
    answer = runner._run_tool(WebSearch(type="web_search", query="latest AI news"))
    assert "tavily.search" in answer
    assert "three hits" in answer
    assert seen == [("tavily.search", {"query": "latest AI news"})]
    assert runner._consecutive_search_misses == 0


def test_web_search_tries_the_next_mcp_tool_when_the_first_fails() -> None:
    """One broken server must not stop the next search tool answering."""
    registry = _registry_with(
        [_search_tool(server="tavily"), _search_tool(server="brave", name="web_search")],
        {
            "tavily.search": ToolCallOutcome(text="quota exceeded", failed=True),
            "brave.web_search": ToolCallOutcome(text="two hits", failed=False),
        },
    )
    runner = _runner(mcp=registry)
    answer = runner._run_tool(WebSearch(type="web_search", query="q"))
    assert "brave.web_search" in answer
    assert "two hits" in answer
    assert runner._consecutive_search_misses == 0


def test_web_search_falls_back_to_the_browser_when_every_mcp_tool_fails() -> None:
    """All servers down is guidance to the browser, not a retry of web_search."""
    registry = _registry_with(
        [_search_tool(server="tavily")],
        {"tavily.search": ToolCallOutcome(text="boom", failed=True)},
    )
    runner = _runner(mcp=registry)
    answer = runner._run_tool(WebSearch(type="web_search", query="q"))
    assert "Google Chrome" in answer
    assert "tekrarlamay" in answer
    assert runner._consecutive_search_misses == 1


def test_empty_search_queries_are_refused_without_looping() -> None:
    runner = _runner(mcp=None)
    answer = runner._run_tool(WebSearch(type="web_search", query="   "))
    assert "non-empty" in answer
    assert runner._consecutive_search_misses == 1


def test_mcp_search_candidates_prefers_named_engines() -> None:
    """Tavily > Exa > Brave > generic search; non-search tools never match."""
    registry = _registry_with(
        [
            _search_tool(server="misc", name="web_search", description="search the web"),
            _search_tool(server="brave", name="web_search"),
            _search_tool(server="files", name="read", description="read a file"),
            _search_tool(server="exa", name="search"),
            _search_tool(server="tavily", name="search"),
        ]
    )
    names = [tool.qualified_name for tool in _mcp_search_candidates(registry)]
    assert names == [
        "tavily.search",
        "exa.search",
        "brave.web_search",
        "misc.web_search",
    ]
    assert _mcp_search_candidates(None) == ()
    assert _is_mcp_search_tool(_search_tool(server="tavily")) is True
    assert (
        _is_mcp_search_tool(_search_tool(server="files", name="read", description="read a file"))
        is False
    )


def test_search_arguments_follow_the_tool_schema() -> None:
    """Servers spell the query field differently; the bridge reads the schema."""
    q_tool = _search_tool(schema={"properties": {"q": {"type": "string"}}})
    assert _search_arguments_for(q_tool, "hello") == {"q": "hello"}
    custom = _search_tool(
        schema={"properties": {"prompt": {"type": "string"}}, "required": ["prompt"]}
    )
    assert _search_arguments_for(custom, "hello") == {"prompt": "hello"}
    bare = _search_tool(schema={})
    assert _search_arguments_for(bare, "hello") == {"query": "hello"}


def test_prompt_no_longer_calls_browser_search_fragile() -> None:
    """The old guidance steered the model away from the browser it needed."""
    assert "dozen fragile steps" not in ACTION_CONTRACT
    prompt = decision_prompt(WorkingState(goal="x"), app="Google Chrome")
    assert "If an MCP search tool (Tavily, Exa, Brave) is available" in prompt
    assert "open Google Chrome, use Cmd+L" in prompt


def test_the_ssrf_guard_reads_every_spelling_the_kernel_accepts() -> None:
    """A shorthand for 127.0.0.1 must not walk past the guard that blocks it.

    ``ipaddress`` only parses the canonical dotted quad, so the guard used to
    see ``127.1`` raise ValueError, conclude "not an address", and allow it —
    while the kernel resolved it to loopback and the fetch read a local server.
    Measured that way against a real server: ``127.0.0.1`` refused, ``127.1``
    fetched. Every spelling below reaches the same host.
    """
    for spelling in ("127.0.0.1", "127.1", "2130706433", "0x7f.0.0.1", "017700000001"):
        assert not _is_fetchable_url(f"http://{spelling}:8080/"), spelling
    assert not _is_fetchable_url("http://[::1]:8080/")
    assert not _is_fetchable_url("http://169.254.169.254/")
    assert not _is_fetchable_url("http://10.0.0.5/")


def test_a_name_that_does_not_resolve_is_left_to_the_fetch() -> None:
    """Refusing an unresolvable name would make every read need a resolver.

    The fetch fails on its own with a truthful "could not reach"; refusing here
    would report "internal-only" about a host that is nothing of the kind.
    """
    assert _is_fetchable_url("https://not-a-real-host.invalid/page")
