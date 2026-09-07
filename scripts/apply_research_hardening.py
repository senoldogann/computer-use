"""Apply the audited research-evidence fix to the branch checkout.

This is an audit harness, not production runtime code. It exists because the
GitHub connector exposes whole-file writes rather than patch hunks; the branch
CI applies these small, asserted replacements and commits only after the
regression tests pass.
"""

from __future__ import annotations

from pathlib import Path


def replace_once(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise SystemExit(f"expected exactly one patch marker in {path}, found {text.count(old)}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def main() -> None:
    loop = Path("src/computeruse/orchestrator/loop.py")
    replace_once(
        loop,
        """                    observed_trail=_extend_trail_entry(
                        state.observed_trail,
                        title=_tool_trail_title(outcome.action),
                        text=answer,
                        max_entries=TRAIL_MAX_ENTRIES,
                    ),""",
        """                    observed_trail=_extend_trail_entry(
                        state.observed_trail,
                        # A tool name is not an evidence identity. Research commonly
                        # calls the same search tool with several different queries;
                        # keying by tool name made each answer overwrite the previous
                        # one in the actor's working trail. The step makes each
                        # machine-observed answer independently addressable while the
                        # bounded trail still caps context growth.
                        title=f"{_tool_trail_title(outcome.action)} @ step {outcome.step_index}",
                        text=answer,
                        max_entries=TRAIL_MAX_ENTRIES,
                    ),""",
    )
    replace_once(
        loop,
        "                    tool_history=_extend_tool_history(state.tool_history, answer),",
        """                    tool_history=_extend_tool_history(
                        state.tool_history,
                        # Preserve provenance for the completion auditor. A raw
                        # answer cannot tell it whether evidence came from a search,
                        # a fetched page, or another MCP tool.
                        f"{_tool_trail_title(outcome.action)}: {answer}",
                    ),""",
    )

    prompts = Path("src/computeruse/orchestrator/prompts.py")
    replace_once(
        prompts,
        """    \"   - Do NOT click browser chrome (tab bar, address bar, toolbar) when you mean a page element.\\n\"
    \"\\n\"
    \"7. READING WEB TEXT — NEVER ZOOM TO READ:\\n\"""",
        """    \"   - Do NOT click browser chrome (tab bar, address bar, toolbar) when you mean a page element.\\n\"
    \"\\n\"
    \"   RESEARCH EVIDENCE DISCIPLINE:\\n\"
    \"   - SEARCH RESULTS ARE DISCOVERY, NOT SOURCE VERIFICATION. A search-result title or snippet\\n\"
    \"     tells you which source to inspect; it does not license details that are absent from that\\n\"
    \"     snippet. For research, current-events, comparison, or summarization goals, open or fetch\\n\"
    \"     the source you actually rely on and read the relevant source content before using its claim.\\n\"
    \"   - When the goal asks for N 'most important', 'top', or 'best' items, collect more candidates\\n\"
    \"     than the requested final count when feasible, then deduplicate and select using evidence\\n\"
    \"     such as recency, source quality, and impact. The first N search hits are not automatically\\n\"
    \"     the top N.\\n\"
    \"   - Keep provenance attached to every selected claim: source title/domain (and URL when useful)\\n\"
    \"     must remain identifiable when you synthesize several sources. Never merge a snippet from\\n\"
    \"     one result with details from another and present the combination as one sourced fact.\\n\"
    \"\\n\"
    \"7. READING WEB TEXT — NEVER ZOOM TO READ:\\n\"""",
    )
    replace_once(
        prompts,
        """    \"- Live, time-varying values returned by tools (asset prices, exchange rates, timestamps,\\n\"""",
        """    \"- For research goals, distinguish discovery evidence from source verification. A tool-history\\n\"
    \"  entry labelled as a search call proves only what that returned result actually contains —\\n\"
    \"  typically candidate titles, URLs and snippets. If a final claim adds details beyond that\\n\"
    \"  evidence, require a fetched/opened source or machine-observed page content supporting them.\\n\"
    \"  For multi-source goals, also verify that the requested number of distinct sources is evidenced\\n\"
    \"  and that selected claims retain identifiable provenance.\\n\"
    \"- Live, time-varying values returned by tools (asset prices, exchange rates, timestamps,\\n\"""",
    )


if __name__ == "__main__":
    main()
