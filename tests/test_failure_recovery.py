import json

import pytest

from pico import Pico, SessionStore, WorkspaceContext


class FailingModelClient:
    model = "failing-test-model"
    supports_prompt_cache = False
    last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens, **kwargs):
        raise RuntimeError("provider unavailable")


def build_agent(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    return Pico(
        model_client=FailingModelClient(),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
    )


def test_model_failure_persists_terminal_state_trace_and_report(tmp_path):
    agent = build_agent(tmp_path)

    with pytest.raises(RuntimeError, match="Model request failed: provider unavailable"):
        agent.ask("Inspect the repository")

    task_state = json.loads(
        agent.run_store.task_state_path(agent.current_task_state).read_text(encoding="utf-8")
    )
    report = json.loads(
        agent.run_store.report_path(agent.current_task_state).read_text(encoding="utf-8")
    )
    trace_events = [
        json.loads(line)
        for line in agent.run_store.trace_path(agent.current_task_state).read_text(encoding="utf-8").splitlines()
    ]

    assert task_state["status"] == "failed"
    assert task_state["stop_reason"] == "model_error"
    assert task_state["failure_code"] == "model_error"
    assert task_state["failure_message"] == "Model request failed: provider unavailable"
    assert report["failure_code"] == "model_error"
    assert report["task_state"]["status"] == "failed"
    assert [event["event"] for event in trace_events][-2:] == ["checkpoint_created", "run_finished"]
    failed = next(event for event in trace_events if event["event"] == "run_failed")
    assert failed["failure_code"] == "model_error"
    assert agent.session["history"][-1]["content"] == "Model request failed: provider unavailable"
