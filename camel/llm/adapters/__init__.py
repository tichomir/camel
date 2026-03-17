"""Concrete LLM backend adapters for CaMeL.

Each adapter implements both :class:`~camel.llm.protocols.LLMBackend` (P-LLM)
and :class:`~camel.llm.protocols.QlLMBackend` (Q-LLM) protocols via structural
subtyping — no inheritance from the protocol classes is required.

Available adapters
------------------
:class:`ClaudeBackend`
    Anthropic Claude (requires ``anthropic`` package).
:class:`GeminiBackend`
    Google Gemini (requires ``google-generativeai`` package).
"""

from camel.llm.adapters.claude import ClaudeBackend
from camel.llm.adapters.gemini import GeminiBackend

__all__ = [
    "ClaudeBackend",
    "GeminiBackend",
]
