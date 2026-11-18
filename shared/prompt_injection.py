"""Text that tries to give instructions to the model.

Counterparty e-mails, web-search results and headlines reach LLM prompts as data. The
prompts already tell the model to ignore instructions inside them; this module is the
deterministic second line: text that *looks* like it talks to the model ("ignore all
previous instructions", "classify this e-mail as ACCEPT", "assistant:", chat-template tokens)
is never read as a buyer's or supplier's acceptance — a person reads it instead — and is
dropped from market context.

A false positive costs one operator look; a false negative is caught by the gates (an
acceptance still needs CONFIRM_NEGOTIATION_AGREEMENT). So the patterns lean towards catching.
"""
from __future__ import annotations

import re
from typing import Final

_PATTERNS: Final[tuple[re.Pattern[str], ...]] = tuple(
    re.compile(p, re.IGNORECASE | re.MULTILINE)
    for p in (
        r"ignore\s+(all\s+|any\s+)?(the\s+)?(previous|prior|above|earlier)\s+(instructions|prompts?|rules)",
        r"disregard\s+(all\s+|the\s+|your\s+)?(previous\s+|prior\s+)?(system\s+prompt|instructions|rules)",
        r"(reveal|print|show|repeat)\s+(me\s+)?(the\s+|your\s+)?(system\s+prompt|instructions|hidden\s+prompt)",
        r"\byou\s+are\s+now\s+(a|an|the|my)\b",
        r"\bdeveloper\s+(message|mode)\b",
        r"\bsystem\s+(message|prompt|override)\b",
        r"\bexfiltrate\b",
        r"\b(classify|mark|treat|label|record)\s+(this|the)\s+(e-?mail|message|reply)\s+as\b",
        r"\bset\s+(the\s+)?(counter_price|price|intent|decision)\w*\s+(to|=)",
        r"^\s*(assistant|system|developer)\s*:",
        r"<\|?(im_start|im_end|system|assistant|endoftext)\|?>",
        r"\[/?(inst|sys)\]",
        r"\bas\s+an\s+ai\s+(language\s+)?model\b",
        r"\b(new|updated)\s+instructions\s*:",
    )
)


def looks_like_injection(text: str | None) -> bool:
    """True when ``text`` contains wording aimed at the model rather than at a person."""
    if not text:
        return False
    return any(p.search(text) for p in _PATTERNS)


def findings(text: str | None) -> list[str]:
    """The matching fragments (for a risk flag or a log line)."""
    if not text:
        return []
    out = []
    for p in _PATTERNS:
        m = p.search(text)
        if m:
            out.append(m.group(0).strip()[:80])
    return out
