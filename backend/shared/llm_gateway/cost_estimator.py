"""Cost estimator — placeholder, NOT YET IMPLEMENTED.

Hook point in LLMClient._node_handle_outcome() after a success:
    if self._cost_estimator:
        cost = self._cost_estimator.estimate(
            model=endpoint.model,
            prompt_tokens=state["response"].prompt_tokens,
            completion_tokens=state["response"].completion_tokens,
        )
        record.metadata["estimated_cost_usd"] = cost
        self._cost_estimator.record(caller=state["caller"], cost_usd=cost)

Why this is needed:
    PortLess runs negotiations on behalf of many sourcing ops simultaneously.
    Without per-call cost tracking, it is impossible to:
      • Show clients how much their AI usage costs per deal.
      • Enforce per-agent or per-client budget caps.
      • Optimise model selection (e.g. route cheap queries to Haiku).

Implementation plan:
    1. Maintain a static pricing table ($/1M tokens, input+output) per model.
    2. estimate(model, prompt_tokens, completion_tokens) → float (USD).
    3. record(caller, cost_usd) → persist to in-memory running total.
    4. get_spend(caller) → float (for budget-cap enforcement).
    5. Budget enforcement:
       check_budget(caller, estimated_cost) raises BudgetExceeded when
       cumulative + estimated > caller_budget_usd.
    6. Config fields on LLMGatewaySettings (already added as placeholders):
       LLM_COST_TRACKING_ENABLED, LLM_BUDGET_PER_CALLER_USD.
    7. Pricing source: pull from §9.1 of portless_master_specification.md;
       keep a model_pricing dict in this file so it can be updated without
       touching the rest of the gateway.

Pricing reference (as of 2026-06):
    Model                         Input $/1M  Output $/1M
    ─────────────────────────────────────────────────────
    claude-haiku-4-5              $1.00       $5.00
    claude-sonnet-4-6             $3.00       $15.00
    claude-opus-4-8               $5.00       $25.00
    claude-fable-5                $10.00      $50.00
    gpt-4o                        $5.00       $15.00
    gemini-1.5-pro                $3.50       $10.50
    qwen/qwen3-32b (Groq)         $0.29       $0.59
    qwen/qwen3.6-27b (Groq)       ~$0.10      ~$0.20  (estimated)
    openai/gpt-oss-safeguard-20b  ~$0.10      ~$0.20  (estimated)

Usage once implemented::

    from shared.llm_gateway import LLMClient, CostEstimator

    estimator = CostEstimator(budget_per_caller_usd=5.0)
    client = LLMClient(cost_estimator=estimator)
    response = await client.chat(messages, caller="negotiation_agent")
    print(f"Cost: ${estimator.get_spend('negotiation_agent'):.6f}")
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Pricing table: (input_per_1m_usd, output_per_1m_usd)
# Override by passing a custom pricing dict to CostEstimator.__init__
_DEFAULT_PRICING: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-opus-4-8": (5.00, 25.00),
    "claude-fable-5": (10.00, 50.00),
    "gpt-4o": (5.00, 15.00),
    "gemini-1.5-pro": (3.50, 10.50),
    "qwen/qwen3-32b": (0.29, 0.59),
    "qwen/qwen3.6-27b": (0.10, 0.20),
    "openai/gpt-oss-safeguard-20b": (0.10, 0.20),
}


class BudgetExceeded(Exception):
    """Raised when a caller's cumulative spend would exceed their budget."""

    def __init__(self, caller: str, spend: float, budget: float) -> None:
        self.caller = caller
        self.spend = spend
        self.budget = budget
        super().__init__(
            f"Budget exceeded for caller='{caller}': "
            f"${spend:.4f} spent / ${budget:.4f} limit"
        )


class CostEstimator:
    """Per-call cost calculator with optional budget enforcement.

    This is a stub — all methods raise NotImplementedError.
    Replace with a real implementation when ready.

    Expected constructor args (once implemented):
        pricing: dict[str, tuple[float, float]] | None = None  # custom table
        budget_per_caller_usd: float = 0.0  # 0 = no limit
    """

    def estimate(self, model: str, prompt_tokens: int, completion_tokens: int) -> float:
        """Return the USD cost for one call.

        Args:
            model: model name (must match a key in the pricing table).
            prompt_tokens: input token count from the LLM response.
            completion_tokens: output token count from the LLM response.

        Returns:
            Estimated cost in USD (0.0 if model not in pricing table).

        Raises:
            NotImplementedError: until this is implemented.
        """
        # TODO: look up (input_rate, output_rate) in pricing table
        # TODO: cost = (prompt_tokens/1e6)*input_rate + (completion_tokens/1e6)*output_rate
        raise NotImplementedError(
            "CostEstimator.estimate() is not yet implemented. "
            "See cost_estimator.py for the implementation plan."
        )

    def record(self, caller: str, cost_usd: float) -> None:
        """Add cost_usd to the running total for this caller.

        Call this after a successful estimate() in handle_outcome.
        """
        # TODO: accumulate in per-caller dict with threading.Lock
        raise NotImplementedError("CostEstimator.record() is not yet implemented.")

    def check_budget(self, caller: str, estimated_cost: float) -> None:
        """Raise BudgetExceeded if spending estimated_cost would exceed the limit.

        Call this BEFORE dispatching (in chat()) when budget enforcement is on.
        """
        # TODO: load caller budget (per-caller or global fallback)
        # TODO: if spend + estimated_cost > budget → raise BudgetExceeded
        raise NotImplementedError("CostEstimator.check_budget() is not yet implemented.")

    def get_spend(self, caller: str) -> float:
        """Return cumulative USD spend for a caller (for dashboards)."""
        # TODO: read from per-caller total dict
        raise NotImplementedError("CostEstimator.get_spend() is not yet implemented.")

    def get_all_spend(self) -> dict[str, float]:
        """Return {caller: total_usd} for all callers."""
        # TODO: return a copy of the per-caller dict
        raise NotImplementedError("CostEstimator.get_all_spend() is not yet implemented.")
