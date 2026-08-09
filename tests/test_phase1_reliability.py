import hashlib
import json
from unittest.mock import patch

import pytest

from pico import ModelCompletion, Pico, SessionStore, ToolCall, WorkspaceContext
from pico.evaluation.phase1 import ToolProtocolOverride, _phase1_benchmark, _row_metrics
from pico.providers.clients import AnthropicCompatibleModelClient, OpenAICompatibleModelClient
from pico.providers.protocol import tool_definitions
from pico.tools import build_tool_registry


def _build_agent(tmp_path, model_client):
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path, repo_root_override=tmp_path)
    return Pico(
        model_client=model_client,
        workspace=workspace,
        session_store=SessionStore(tmp_path / ".pico" / "sessions"),
        approval_policy="auto",
    )


def test_tool_definitions_convert_runtime_schema_to_strict_json_schema(tmp_path):
    class Agent:
        root = tmp_path
        depth = 1
        max_depth = 1

        @staticmethod
        def path(raw):
            return (tmp_path / raw).resolve()

        @staticmethod
        def shell_env():
            return {}

    definitions = tool_definitions(build_tool_registry(Agent()))
    read_file = next(tool for tool in definitions if tool["name"] == "read_file")

    assert read_file["parameters"]["additionalProperties"] is False
    assert read_file["parameters"]["properties"]["start"] == {"type": "integer"}
    assert set(read_file["parameters"]["required"]) == {"path", "start", "end"}

    patch_v2 = next(tool for tool in definitions if tool["name"] == "patch_file_v2")
    expected_hashes = patch_v2["parameters"]["properties"]["expected_hashes"]
    assert expected_hashes["type"] == "array"
    assert expected_hashes["items"] == {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "sha256": {"type": "string"},
        },
        "required": ["path", "sha256"],
        "additionalProperties": False,
    }


def test_agent_loop_accepts_native_tool_call_and_keeps_text_fallback(tmp_path):
    (tmp_path / "sample.txt").write_text("alpha\n", encoding="utf-8")

    class NativeClient:
        supports_prompt_cache = False
        supports_native_tools = True
        last_completion_metadata = {}

        def __init__(self):
            self.calls = []

        def complete(self, prompt, max_new_tokens, **kwargs):
            self.calls.append((prompt, kwargs.get("tools", [])))
            if len(self.calls) == 1:
                return ModelCompletion(
                    tool_calls=(ToolCall("read_file", {"path": "sample.txt", "start": 1, "end": 1}, "call_1"),)
                )
            return "Done after native tool."

    client = NativeClient()
    agent = _build_agent(tmp_path, client)

    answer = agent.ask("Inspect sample.txt")

    assert answer == "Done after native tool."
    assert client.calls[0][1]
    assert "structured tools provided by the model API" in client.calls[0][0]
    assert agent.current_task_state.tool_steps == 1
    tool_event = next(item for item in agent.session["history"] if item["role"] == "tool")
    assert tool_event["name"] == "read_file"


def test_openai_client_sends_strict_tools_and_extracts_function_call():
    captured = {}

    class Response:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "output": [
                        {
                            "type": "function_call",
                            "call_id": "call_1",
                            "name": "read_file",
                            "arguments": '{"path":"README.md","start":1,"end":10}',
                        }
                    ],
                    "usage": {"input_tokens": 12, "output_tokens": 4},
                }
            ).encode("utf-8")

    def urlopen(request, timeout):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return Response()

    client = OpenAICompatibleModelClient("gpt-test", "https://api.openai.com/v1", "sk-test", 0, 30)
    tools = [
        {
            "name": "read_file",
            "description": "Read a file.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"}},
                "required": ["path", "start", "end"],
                "additionalProperties": False,
            },
        }
    ]

    with patch("urllib.request.urlopen", urlopen):
        result = client.complete("read", 64, tools=tools)

    assert isinstance(result, ModelCompletion)
    assert result.tool_calls[0].arguments["path"] == "README.md"
    assert captured["body"]["tools"][0]["strict"] is True


def test_anthropic_client_sends_tools_and_extracts_tool_use():
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_1",
                            "name": "read_file",
                            "input": {"path": "README.md", "start": 1, "end": 10},
                        }
                    ]
                }
            ).encode("utf-8")

    def urlopen(request, timeout):
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return Response()

    client = AnthropicCompatibleModelClient("claude-test", "https://api.anthropic.com/v1", "sk-test", 0, 30)
    tools = [
        {
            "name": "read_file",
            "description": "Read a file.",
            "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
        }
    ]

    with patch("urllib.request.urlopen", urlopen):
        result = client.complete("read", 64, tools=tools)

    assert result.tool_calls[0].call_id == "toolu_1"
    assert captured["body"]["tools"][0]["input_schema"]["type"] == "object"


def test_patch_file_v2_applies_diff_returns_preview_and_verifies(tmp_path):
    path = tmp_path / "sample.py"
    path.write_text("VALUE = 'old'\n", encoding="utf-8")
    expected_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    agent = _build_agent(tmp_path, type("Client", (), {"supports_prompt_cache": False, "supports_native_tools": False})())
    patch_text = """--- a/sample.py
+++ b/sample.py
@@ -1 +1 @@
-VALUE = 'old'
+VALUE = 'new'
"""

    result = agent.run_tool(
        "patch_file_v2",
        {"patch": patch_text, "expected_hashes": {"sample.py": expected_hash}, "verify": True},
    )

    assert path.read_text(encoding="utf-8") == "VALUE = 'new'\n"
    assert "verification_exit_code: 0" in result
    assert "workspace_diff:" in result
    assert "+VALUE = 'new'" in result
    assert agent._last_tool_result_metadata["tool_status"] == "ok"
    assert agent._last_tool_result_metadata["affected_paths"] == ["sample.py"]


def test_patch_file_v2_rejects_hash_mismatch_before_writing(tmp_path):
    path = tmp_path / "sample.py"
    path.write_text("VALUE = 'old'\n", encoding="utf-8")
    agent = _build_agent(tmp_path, type("Client", (), {"supports_prompt_cache": False, "supports_native_tools": False})())
    patch_text = """--- a/sample.py
+++ b/sample.py
@@ -1 +1 @@
-VALUE = 'old'
+VALUE = 'new'
"""

    result = agent.run_tool(
        "patch_file_v2",
        {"patch": patch_text, "expected_hashes": {"sample.py": "stale"}, "verify": True},
    )

    assert "file hash mismatch" in result
    assert path.read_text(encoding="utf-8") == "VALUE = 'old'\n"
    assert agent._last_tool_result_metadata["tool_status"] == "rejected"


def test_patch_file_v2_marks_failed_verification_as_partial_success(tmp_path):
    path = tmp_path / "sample.py"
    path.write_text("VALUE = 1\n", encoding="utf-8")
    agent = _build_agent(tmp_path, type("Client", (), {"supports_prompt_cache": False, "supports_native_tools": False})())
    patch_text = """--- a/sample.py
+++ b/sample.py
@@ -1 +1 @@
-VALUE = 1
+def broken(:
"""

    result = agent.run_tool(
        "patch_file_v2",
        {"patch": patch_text, "expected_hashes": {}, "verify": True},
    )

    assert "verification_exit_code: 1" in result
    assert agent._last_tool_result_metadata["tool_status"] == "partial_success"
    assert agent._last_tool_result_metadata["tool_error_code"] == "verification_failed"


def test_legacy_write_result_stays_stable_while_agent_history_receives_diff(tmp_path):
    class Client:
        supports_prompt_cache = False
        supports_native_tools = False
        last_completion_metadata = {}

        def __init__(self):
            self.outputs = [
                '<tool>{"name":"write_file","args":{"path":"note.txt","content":"new text\\n"}}</tool>',
                "Done.",
            ]

        def complete(self, prompt, max_new_tokens, **kwargs):
            return self.outputs.pop(0)

    direct_agent = _build_agent(tmp_path / "direct", Client())
    assert direct_agent.run_tool("write_file", {"path": "note.txt", "content": "new text\n"}) == "wrote note.txt (9 chars)"

    loop_agent = _build_agent(tmp_path / "loop", Client())
    assert loop_agent.ask("Create note.txt") == "Done."
    tool_event = next(item for item in loop_agent.session["history"] if item["role"] == "tool")
    assert tool_event["content"].startswith("wrote note.txt (9 chars)")
    assert "workspace_diff:" in tool_event["content"]
    assert "+new text" in tool_event["content"]


def test_tool_protocol_override_disables_tools_without_hiding_other_capabilities():
    class Client:
        supports_prompt_cache = True
        supports_native_tools = True
        last_completion_metadata = {"input_tokens": 3}

        def __init__(self):
            self.kwargs = None

        def complete(self, prompt, max_new_tokens, **kwargs):
            self.kwargs = kwargs
            self.last_completion_metadata = {"input_tokens": 5}
            return "done"

    client = Client()
    wrapper = ToolProtocolOverride(client, native_tools=False)
    result = wrapper.complete("prompt", 10, tools=[{"name": "read_file"}], prompt_cache_key="abc")

    assert result == "done"
    assert wrapper.supports_prompt_cache is True
    assert wrapper.supports_native_tools is False
    assert "tools" not in client.kwargs
    assert client.kwargs["prompt_cache_key"] == "abc"
    assert wrapper.last_completion_metadata == {"input_tokens": 5}


def test_phase1_benchmark_adds_patch_v2_without_mutating_source(tmp_path):
    source_path = tmp_path / "benchmark.json"
    output_path = tmp_path / "derived" / "benchmark.json"
    source = {
        "schema_version": 1,
        "tasks": [
            {"id": "edit", "allowed_tools": ["read_file", "patch_file"]},
            {"id": "read", "allowed_tools": ["read_file"]},
        ],
    }
    source_path.write_text(json.dumps(source), encoding="utf-8")

    assert _phase1_benchmark(source_path, output_path) == output_path

    derived = json.loads(output_path.read_text(encoding="utf-8"))
    persisted_source = json.loads(source_path.read_text(encoding="utf-8"))
    assert derived["tasks"][0]["allowed_tools"] == ["read_file", "patch_file", "patch_file_v2"]
    assert derived["tasks"][1]["allowed_tools"] == ["read_file"]
    assert persisted_source == source


def test_phase1_row_metrics_aggregate_protocol_and_failure_rates():
    summary = _row_metrics(
        [
            {
                "passed": True,
                "verifier_passed": True,
                "within_budget": True,
                "tool_steps": 2,
                "attempts": 3,
                "malformed_response_count": 0,
                "native_tool_call_count": 2,
                "patch_failure_count": 0,
                "verification_failure_count": 0,
            },
            {
                "passed": False,
                "verifier_passed": False,
                "within_budget": False,
                "tool_steps": 4,
                "attempts": 5,
                "malformed_response_count": 2,
                "native_tool_call_count": 1,
                "patch_failure_count": 1,
                "verification_failure_count": 1,
            },
        ]
    )

    assert summary["pass_rate"] == 0.5
    assert summary["verifier_pass_rate"] == 0.5
    assert summary["within_budget_rate"] == 0.5
    assert summary["avg_tool_steps"] == 3
    assert summary["avg_attempts"] == 4
    assert summary["malformed_tool_rate"] == pytest.approx(0.25)
    assert summary["native_tool_call_count"] == 3
    assert summary["patch_failure_count"] == 1
    assert summary["verification_failure_count"] == 1
