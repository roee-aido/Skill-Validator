"""Agent loop with per-turn budget and stop-condition checks.

A "brain" decides which tools to call; this loop executes the calls through tools.call_tool
(with every guard), charges usage after each turn, and stops on the configured limits.

- MockBrain (mock_model.py) yields scripted tool calls and simulated usage. Phases 0-6.
- The real brain (Phase 7) will use the Anthropic SDK Tool Runner and call the same
  `charge` and limit checks from its per-turn hook. It is not implemented yet.
"""

import json
import time
from dataclasses import dataclass, field

from .config import AgentConfig
from .tools import ToolContext, ToolDenied, ToolError, call_tool


class BudgetExceeded(Exception):
    pass


class LimitReached(Exception):
    pass


class ModelFailure(Exception):
    """The model service failed or refused (Phase 7). Maps to on_model_error_or_refusal."""


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class Meter:
    """Tracks one task run and the review's cumulative spend (D8: never reset)."""

    cfg: AgentConfig
    review: object  # ReviewSession
    started: float = field(default_factory=time.monotonic)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def cost_of(self, u: Usage) -> float:
        p = self.cfg.model.pricing
        write = p.cache_write_per_mtok or p.input_per_mtok
        read = p.cache_read_per_mtok or p.input_per_mtok
        return (
            u.input_tokens * p.input_per_mtok
            + u.cache_creation_input_tokens * write
            + u.cache_read_input_tokens * read
            + u.output_tokens * p.output_per_mtok
        ) / 1e6

    def elapsed_minutes(self) -> float:
        return (time.monotonic() - self.started) / 60

    def charge(self, u: Usage) -> None:
        cost = self.cost_of(u)
        all_input = u.input_tokens + u.cache_creation_input_tokens + u.cache_read_input_tokens
        self.input_tokens += all_input
        self.output_tokens += u.output_tokens
        self.cost_usd += cost
        r = self.review
        r.spent_tokens_total += all_input + u.output_tokens
        r.spent_cost_total += cost
        self.check()

    def check(self) -> None:
        r, lim = self.review, self.cfg.limits.per_task
        minutes_total = r.spent_minutes_total + self.elapsed_minutes()
        if r.spent_tokens_total >= r.budget_tokens_ceiling:
            raise BudgetExceeded("tokens ceiling of the review reached")
        if r.spent_cost_total >= r.budget_cost_ceiling:
            raise BudgetExceeded("cost ceiling of the review reached")
        if minutes_total >= r.budget_minutes_ceiling:
            raise BudgetExceeded("processing time ceiling of the review reached")
        # Per-task limits end the task (retry, then "not checked"), not the whole review.
        if self.input_tokens + self.output_tokens >= lim.tokens or self.cost_usd >= lim.cost_usd:
            raise LimitReached("per-task budget reached")
        if self.elapsed_minutes() >= lim.processing_minutes:
            raise LimitReached("per-task processing time reached")


def run_agent_task(ctx: ToolContext, brain, cfg: AgentConfig, meter: Meter, system_prompt: str, task_prompt: str) -> None:
    """Drive one task to completion or raise BudgetExceeded / LimitReached / ModelFailure."""
    if hasattr(brain, "run"):  # the real model drives its own loop with the same checks
        brain.run(ctx, cfg, meter, system_prompt, task_prompt)
        return
    stop = cfg.stop_conditions
    conversation = [system_prompt, task_prompt]
    steps = brain.steps(ctx)
    results = None
    turns = tool_calls = 0
    while True:
        try:
            calls = next(steps) if results is None else steps.send(results)
        except StopIteration:
            return
        turns += 1
        if turns > stop.max_agent_turns_per_task:
            raise LimitReached(f"more than {stop.max_agent_turns_per_task} turns")
        meter.charge(brain.usage_for_turn(conversation, calls))
        results = []
        for name, args in calls:
            tool_calls += 1
            if tool_calls > stop.max_tool_calls_per_task:
                raise LimitReached(f"more than {stop.max_tool_calls_per_task} tool calls")
            try:
                results.append({"ok": True, "result": call_tool(ctx, name, args)})
            except (ToolDenied, ToolError) as e:
                results.append({"ok": False, "error": str(e)})
        conversation.append(json.dumps(calls, ensure_ascii=False))
        conversation.append(json.dumps(results, ensure_ascii=False, default=str))
