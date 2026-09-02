"""Run ceilings: wall clock, tokens, and spend.

--max-steps bounds decisions, which is the wrong unit for the two things that
actually run out. These tests pin the policy (pure), the loop's stop point
(between steps, never mid-action), and the guarantee that matters most: a run
stopped by its budget has already written its artifacts.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from computeruse.agent import Agent, AgentConfig
from computeruse.cli import parse_args, resolve_cost_price
from computeruse.orchestrator.budget import (
    BudgetExceededError,
    RunBudget,
    RunUsage,
    budget_verdict,
)
from computeruse.orchestrator.loop import OodaRunner, WorkingState
from computeruse.orchestrator.schemas import AgentTurn, MouseClick
from computeruse.providers.openai import (
    DEFAULT_MODEL,
    MODEL_PRICES,
    ModelCallStats,
    OpenAIError,
    call_cost_usd,
    price_for,
)
from computeruse.security.autonomy import AutonomyLevel
from tests.smoke.conftest import SIMULATED_SETTLE, SOCKET_PATH

_NO_LIMITS = RunBudget()


def _usage(*, seconds: float = 0.0, tokens: int = 0, cost: float = 0.0) -> RunUsage:
    return RunUsage(elapsed_seconds=seconds, total_tokens=tokens, cost_usd=cost)


def test_an_unset_budget_never_stops_a_run() -> None:
    assert _NO_LIMITS.is_unset
    assert budget_verdict(_NO_LIMITS, _usage(seconds=1e6, tokens=10**9, cost=1e6)) is None


def test_each_ceiling_stops_the_run_and_names_itself() -> None:
    assert "time budget" in (
        budget_verdict(RunBudget(deadline_seconds=30), _usage(seconds=30)) or ""
    )
    assert "token budget" in (
        budget_verdict(RunBudget(max_tokens=1000), _usage(tokens=1000)) or ""
    )
    assert "cost budget" in (
        budget_verdict(RunBudget(max_cost_usd=0.5), _usage(cost=0.5)) or ""
    )


def test_usage_below_a_ceiling_passes() -> None:
    budget = RunBudget(deadline_seconds=30, max_tokens=1000, max_cost_usd=0.5)
    assert budget_verdict(budget, _usage(seconds=29.9, tokens=999, cost=0.49)) is None


def test_cost_is_computed_from_published_prices() -> None:
    price = price_for("gpt-5.6-terra")
    stats = ModelCallStats(
        total_tokens=1_500_000,
        prompt_tokens=1_000_000,
        completion_tokens=500_000,
        elapsed_s=1.0,
    )
    # 1M input at $2 + 0.5M output at $12.
    assert call_cost_usd(price, stats) == pytest.approx(2.0 + 6.0)


def test_an_unpriced_model_refuses_to_invent_a_number() -> None:
    with pytest.raises(OpenAIError, match="no published price"):
        price_for("some-model-nobody-published")


def test_cost_ceiling_needs_a_priced_transport() -> None:
    """A budget enforced against a guessed price is worse than no budget."""
    priced = parse_args(["--goal", "x", "--model", "openai", "--max-cost", "1.0"])
    assert resolve_cost_price(priced) == MODEL_PRICES[DEFAULT_MODEL]

    custom = parse_args(
        ["--goal", "x", "--model", "my_mod:my_model", "--max-cost", "1.0"]
    )
    with pytest.raises(OpenAIError, match="use --max-tokens"):
        resolve_cost_price(custom)

    no_ceiling = parse_args(["--goal", "x", "--model", "my_mod:my_model"])
    assert resolve_cost_price(no_ceiling) is None


def test_the_guard_stops_the_loop_between_steps_not_mid_action() -> None:
    """The action already in flight always completes; the next one never starts."""
    executed: list[object] = []

    def provider(state: WorkingState) -> AgentTurn:
        return AgentTurn(
            thought="t",
            sub_goal="s",
            action=MouseClick(type="mouse_click", x=10 + state.step_index, y=10),
        )

    def guard() -> None:
        if len(executed) >= 2:
            raise BudgetExceededError("token budget exhausted: 900 tokens used of 800")

    runner = OodaRunner(
        provider=provider,
        execute_physical=executed.append,
        budget_guard=guard,
        max_steps=20,
    )
    with pytest.raises(BudgetExceededError, match="token budget"):
        runner.run(goal="spin")
    assert len(executed) == 2, "no half-executed action, and no extra one either"


def test_a_budget_stop_leaves_the_artifacts_behind(tmp_path: Path) -> None:
    """"Clean exit" means the episode and the trace are already on disk."""
    calls: list[int] = []

    def provider(state: WorkingState) -> AgentTurn:
        calls.append(state.step_index)
        return AgentTurn(
            thought="t",
            sub_goal="s",
            action=MouseClick(type="mouse_click", x=20 + state.step_index, y=20),
        )

    def guard() -> None:
        if len(calls) >= 2:
            raise BudgetExceededError("time budget exhausted: 61s elapsed of 60s allowed")

    trace_dir = tmp_path / "traces"
    store_dir = tmp_path / "store"
    config = AgentConfig(
        goal="work until the money runs out",
        app="Safari",
        provider=provider,
        socket_path=str(SOCKET_PATH),
        store_dir=store_dir,
        autonomy_level=AutonomyLevel.FULL,
        enable_visual_verification=False,
        enable_vision=False,
        max_steps=20,
        trace_dir=trace_dir,
        budget_guard=guard,
        **SIMULATED_SETTLE,
    )
    with pytest.raises(BudgetExceededError):
        Agent(config).run()

    episodes = sorted((store_dir / "episodes").glob("*.json"))
    assert len(episodes) == 1
    episode = json.loads(episodes[0].read_text())
    assert episode["outcome"] == "failure"
    assert "budget" in (episode["retrospective"] or "")
    traces = list(trace_dir.glob("*/steps.jsonl"))
    assert len(traces) == 1
    assert traces[0].read_text().splitlines(), "the steps it did take are recorded"


def test_the_cli_exposes_all_three_ceilings() -> None:
    args = parse_args(
        [
            "--goal",
            "x",
            "--deadline-seconds",
            "90",
            "--max-tokens",
            "50000",
            "--max-cost",
            "0.25",
        ]
    )
    assert isinstance(args, argparse.Namespace)
    assert args.deadline_seconds == 90.0
    assert args.max_tokens == 50000
    assert args.max_cost == 0.25
