"""LLM abstraction layer for AI-Ready remediation workflows.

Architecture:
  LLMProvider  — abstract base class for LLM backends (Groq, OpenAI, Anthropic, Ollama, Bedrock)
  LLMGateway   — provider-agnostic gateway that routes calls to the active provider
  providers    — concrete implementations (groq.py, openai.py, anthropic.py, bedrock.py, ...)

Burr actions depend on LLMGateway, never on a specific provider.
This allows swapping LLM backends without touching workflow logic.
"""

from ai_ready.llm.base import LLMProvider, LLMResponse, LLMMessage
from ai_ready.llm.gateway import LLMGateway

__all__ = ["LLMProvider", "LLMResponse", "LLMMessage", "LLMGateway"]

# Bedrock provider is imported lazily to avoid requiring boto3 at install time.
# To use: from ai_ready.llm.bedrock import BedrockProvider
