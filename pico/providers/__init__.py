"""Model provider adapters."""

from .clients import AnthropicCompatibleModelClient, FakeModelClient, OllamaModelClient, OpenAICompatibleModelClient
from .protocol import ModelCompletion, ToolCall

__all__ = [
    "AnthropicCompatibleModelClient",
    "FakeModelClient",
    "OllamaModelClient",
    "OpenAICompatibleModelClient",
    "ModelCompletion",
    "ToolCall",
]
