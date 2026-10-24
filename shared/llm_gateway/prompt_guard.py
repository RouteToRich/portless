"""Prompt guard — placeholder, NOT YET IMPLEMENTED.

Hook point in LLMClient.chat(), before entering the LangGraph:
    if self._prompt_guard:
        result = self._prompt_guard.check(messages)
        if result.blocked:
            raise PromptBlocked(reason=result.reason, category=result.category)

Why this is needed:
    PortLess agents receive user-supplied deal context that flows directly
    into LLM prompts. Adversarial suppliers could embed prompt-injection
    payloads in quoted email text or commodity description fields. Without
    a guard layer, an injected instruction could:
      • Leak system-prompt contents (e.g. internal price floors).
      • Override negotiation strategy mid-conversation.
      • Cause the agent to agree to unfavourable deal terms.

Detection approaches:
    1. Heuristic: regex patterns for common injection markers
       ("Ignore all previous instructions", "You are now DAN", etc.).
    2. LLM-as-judge: send the messages to a small, cheap model
       (e.g. claude-haiku-4-5) with a prompt-injection detection prompt.
    3. Input length cap: reject messages that exceed max_input_chars.
    4. PII scrubbing: optionally redact emails / phone numbers before logging
       (keeps the JSONL tracking file clean of personal data).

Implementation plan:
    1. GuardResult dataclass: blocked bool, reason str, category str.
    2. Implement HeuristicGuard (pattern matching, fast, no LLM call).
    3. Implement LLMJudgeGuard (calls haiku to classify; adds ~300ms).
    4. CompositeGuard(guards=[HeuristicGuard(), LLMJudgeGuard()]) runs
       all guards; blocks on first match.
    5. Config fields on LLMGatewaySettings (already added as placeholders):
       LLM_PROMPT_GUARD_ENABLED, LLM_PROMPT_GUARD_MAX_INPUT_CHARS,
       LLM_PROMPT_GUARD_MODE ("heuristic" | "llm_judge" | "composite").
    6. Blocked requests are still tracked (status="blocked") so ops can
       audit injection attempts.

Usage once implemented::

    from shared.llm_gateway import LLMClient
    from shared.llm_gateway.prompt_guard import PromptGuard, HeuristicGuard

    client = LLMClient(prompt_guard=HeuristicGuard())
    # Raises PromptBlocked if injection detected.
    response = await client.chat(messages, caller="negotiation_agent")
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Common injection patterns (starter set; extend when needed)
_INJECTION_PATTERNS: list[str] = [
    "ignore all previous instructions",
    "ignore previous instructions",
    "ignore your instructions",
    "you are now",
    "new instructions:",
    "system: you must",
    "disregard your",
    "forget your",
    "pretend you are",
    "act as if you",
    "jailbreak",
    "dan mode",
]


class PromptBlocked(Exception):
    """Raised when a message set is rejected by the prompt guard."""

    def __init__(self, reason: str, category: str) -> None:
        self.reason = reason
        self.category = category
        super().__init__(f"Prompt blocked [{category}]: {reason}")


@dataclass
class GuardResult:
    """Result of a prompt guard check."""

    blocked: bool
    reason: str = ""
    category: str = ""  # "injection" | "too_long" | "pii" | "llm_judge" | ""


class PromptGuard:
    """Base class for prompt guard implementations.

    This is a stub — check() raises NotImplementedError.
    Replace with a real implementation when ready.
    """

    def check(self, messages: list[dict[str, str]]) -> GuardResult:
        """Analyse messages and return a GuardResult.

        Args:
            messages: the same list[dict] passed to LLMClient.chat().

        Returns:
            GuardResult with blocked=False if safe, blocked=True if rejected.

        Raises:
            NotImplementedError: until this is implemented.
        """
        # TODO: implement detection logic (heuristic / LLM-judge / composite)
        raise NotImplementedError(
            "PromptGuard.check() is not yet implemented. "
            "See prompt_guard.py for the implementation plan."
        )


class HeuristicGuard(PromptGuard):
    """Fast pattern-matching guard with no LLM call required.

    This is a stub — check() raises NotImplementedError.
    Replace with a real implementation when ready.
    """

    def check(self, messages: list[dict[str, str]]) -> GuardResult:
        """Scan message content against known injection patterns and length limits.

        Raises:
            NotImplementedError: until this is implemented.
        """
        # TODO: concatenate all message.content fields → lower()
        # TODO: iterate _INJECTION_PATTERNS; return blocked=True on first match
        # TODO: if total_chars > max_input_chars → blocked (category="too_long")
        raise NotImplementedError(
            "HeuristicGuard.check() is not yet implemented. "
            "See prompt_guard.py for the implementation plan."
        )


class LLMJudgeGuard(PromptGuard):
    """Sends messages to a small model to classify injection likelihood.

    This is a stub — check() raises NotImplementedError.
    Requires access to an LLMClient (passed to __init__ once implemented).
    """

    def check(self, messages: list[dict[str, str]]) -> GuardResult:
        """Call a small LLM to classify whether messages contain an injection.

        Raises:
            NotImplementedError: until this is implemented.
        """
        # TODO: build a meta-prompt: "Do the following messages contain a
        #       prompt injection attempt? Answer YES or NO and explain."
        # TODO: call haiku (cheap) with a 5-second timeout
        # TODO: parse YES/NO from response → blocked bool
        raise NotImplementedError(
            "LLMJudgeGuard.check() is not yet implemented. "
            "See prompt_guard.py for the implementation plan."
        )
