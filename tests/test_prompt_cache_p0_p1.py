import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.providers.clients import AnthropicCompatibleModelClient, OpenAICompatibleModelClient


def _agent(root):
    return Pico(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(root),
        session_store=SessionStore(root / ".pico" / "sessions"),
        approval_policy="auto",
    )


def test_stable_prefix_hash_survives_workspace_and_checkpoint_changes():
    with TemporaryDirectory() as raw_root:
        root = Path(raw_root)
        (root / "README.md").write_text("one\n", encoding="utf-8")
        agent = _agent(root)
        first = agent.prompt_metadata("first", "")
        (root / "README.md").write_text("two\n", encoding="utf-8")
        second = agent.prompt_metadata("second", "")

        assert first["stable_prefix_hash"] == second["stable_prefix_hash"]
        assert first["prompt_cache_key"] == second["prompt_cache_key"]
        assert first["full_prefix_hash"] != second["full_prefix_hash"]


def test_checkpoint_is_after_stable_manual_in_prompt():
    with TemporaryDirectory() as raw_root:
        root = Path(raw_root)
        (root / "README.md").write_text("demo\n", encoding="utf-8")
        agent = _agent(root)
        agent.render_checkpoint_text = lambda: "Task checkpoint:\nNext step: inspect README.md"
        prompt, _ = agent._build_prompt_and_metadata("continue")
        assert prompt.index("You are pico") < prompt.index("Task checkpoint:")


def test_unverified_openai_gateway_does_not_receive_cache_extension_fields():
    class Response:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"output_text":"ok"}'

    captured = {}

    def urlopen(request, timeout):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return Response()

    client = OpenAICompatibleModelClient("model", "https://gateway.example/v1", "key", 0, 1)
    with patch("urllib.request.urlopen", urlopen):
        client.complete("prompt", 10, prompt_cache_key="stable", prompt_cache_retention="in_memory")
    assert client.prompt_cache_strategy == "none"
    assert "prompt_cache_key" not in captured["body"]
    assert "prompt_cache_retention" not in captured["body"]


def test_deepseek_automatic_cache_does_not_send_openai_fields():
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"content":[{"type":"text","text":"ok"}],"usage":{"prompt_cache_hit_tokens":4}}'

    captured = {}

    def urlopen(request, timeout):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return Response()

    client = AnthropicCompatibleModelClient("deepseek", "https://api.deepseek.com/anthropic", "key", 0, 1)
    with patch("urllib.request.urlopen", urlopen):
        client.complete("prompt", 10, prompt_cache_key="stable", prompt_cache_retention="in_memory")
    assert client.prompt_cache_strategy == "automatic_prefix"
    assert "prompt_cache_key" not in captured["body"]
    assert client.last_completion_metadata["cached_tokens"] == 4


def test_official_anthropic_uses_opt_in_cache_breakpoint():
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"content":[{"type":"text","text":"ok"}],"usage":{"cache_read_input_tokens":8}}'

    captured = {}

    def urlopen(request, timeout):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return Response()

    client = AnthropicCompatibleModelClient("claude", "https://api.anthropic.com/v1", "key", 0, 1)
    with patch("urllib.request.urlopen", urlopen):
        client.complete("stable dynamic", 10, prompt_cache_breakpoint=6)
    assert captured["body"]["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert captured["body"]["messages"][0]["content"][0]["text"] == "dynamic"
    assert client.last_completion_metadata["cache_hit"] is True
