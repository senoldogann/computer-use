"""Run budgets: the ceilings a long autonomous run must not quietly blow past.

``--max-steps`` bounds how many *decisions* a run may take, which is the wrong
unit for the two things that actually run out. A run can sit inside its step
budget for forty minutes because each turn waits on a slow page, and a run
against an expensive model can spend more than the task was worth in fifteen
steps. Wall-clock time and model spend need their own ceilings.

The verdict is a pure function of a budget and the usage so far, so the policy
is testable without a clock, a model, or a run — and the imperative half is one
closure in the composition root that reads the counters it already keeps.

Exceeding a budget is a *clean* ending: :class:`BudgetExceededError` is raised
between steps, never mid-action, and the run finalises like any other abnormal
ending — the failure episode and the trace are on disk before it propagates.
"""

from __future__ import annotations

from dataclasses import dataclass


class BudgetExceededError(RuntimeError):
    """A run hit its wall-clock, token, or cost ceiling and stopped.

    Not a failure of the agent: the run was doing what it was told and ran out
    of the allowance it was given. Carries the reason as its message so the
    caller can print which ceiling it was and by how much.
    """


@dataclass(frozen=True)
class RunBudget:
    """Ceilings for one run; ``None`` means "no ceiling" (pure data)."""

    deadline_seconds: float | None = None
    max_tokens: int | None = None
    max_cost_usd: float | None = None

    @property
    def is_unset(self) -> bool:
        """True when no ceiling is configured at all."""
        return (
            self.deadline_seconds is None
            and self.max_tokens is None
            and self.max_cost_usd is None
        )


@dataclass(frozen=True)
class RunUsage:
    """What the run has consumed so far (pure data)."""

    elapsed_seconds: float
    total_tokens: int
    cost_usd: float


def budget_verdict(budget: RunBudget, usage: RunUsage) -> str | None:
    """The reason a run must stop, or ``None`` while it may continue (pure).

    Checked cheapest-ceiling-first only for readability; all three are
    independent, and the first one exceeded is the one reported — a run that
    blew two ceilings at once is stopped either way, and naming one clearly
    beats naming both vaguely.
    """
    deadline = budget.deadline_seconds
    if deadline is not None and usage.elapsed_seconds >= deadline:
        return (
            f"time budget exhausted: {usage.elapsed_seconds:.0f}s elapsed of "
            f"{deadline:.0f}s allowed"
        )
    max_tokens = budget.max_tokens
    if max_tokens is not None and usage.total_tokens >= max_tokens:
        return (
            f"token budget exhausted: {usage.total_tokens} tokens used of "
            f"{max_tokens} allowed"
        )
    max_cost = budget.max_cost_usd
    if max_cost is not None and usage.cost_usd >= max_cost:
        return (
            f"cost budget exhausted: ${usage.cost_usd:.4f} spent of "
            f"${max_cost:.4f} allowed"
        )
    return None
