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
:class:`OpenAIBackend`
    OpenAI GPT-4.1 / o3 / o4-mini (requires ``openai`` package).
"""

from camel.llm.adapters.claude import ClaudeBackend
from camel.llm.adapters.gemini import GeminiBackend
from camel.llm.adapters.openai import OpenAIBackend

__all__ = [
    "ClaudeBackend",
    "GeminiBackend",
    "OpenAIBackend",
]
