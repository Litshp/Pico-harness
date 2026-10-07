from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.jev_selector import ContextSelection, JevContextSelector


class _Answer:
    def __init__(self, choice, confidence=0.95):
        self.choice = choice
        self.confidence = confidence


class _Response:
    answers = {
        "tool_scope": _Answer("execute"),
        "history_scope": _Answer("minimal"),
        "memory_scope": _Answer("none"),
    }


class _Jev:
    def __init__(self):
        self.calls = []

    def system_one(self, **kwargs):
        self.calls.append(kwargs)
        return _Response()


def _agent(tmp_path):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Pico(
        model_client=FakeModelClient([]),
        workspace=WorkspaceContext.build(tmp_path),
        session_store=SessionStore(tmp_path / ".pico" / "sessions"),
        approval_policy="auto",
    )


def test_jev_selects_dynamic_tool_schemas_and_history(tmp_path):
    agent = _agent(tmp_path)
    jev = _Jev()
    agent.jev_selector = JevContextSelector(agent, client=jev, enabled=True)
    for index in range(6):
        agent.record({"role": "user", "content": f"old-{index}", "created_at": str(index)})

    prompt, metadata = agent._build_prompt_and_metadata("run the tests")

    assert metadata["context_selection"]["source"] == "jev"
    assert metadata["context_selection"]["tool_names"] == ["read_file", "search", "run_shell"]
    assert "- run_shell(" in prompt
    assert "- write_file(" not in prompt
    assert "old-0" not in prompt
    assert jev.calls
    assert jev.calls[0]["state"]["request"] == "run the tests"


def test_jev_failure_falls_back_without_changing_execution_contract(tmp_path):
    agent = _agent(tmp_path)

    class Broken:
        def system_one(self, **kwargs):
            raise TimeoutError("jev unavailable")

    agent.jev_selector = JevContextSelector(agent, client=Broken(), enabled=True)
    _, metadata = agent._build_prompt_and_metadata("inspect")

    assert metadata["context_selection"]["fallback"] is True
    assert set(metadata["context_selection"]["tool_names"]) == set(agent.tools)
    # JEV can reduce context, but it never changes Pico's real tool registry.
    assert set(agent.tools) >= {"read_file", "write_file", "run_shell"}


def test_working_and_none_memory_scopes_change_prompt_sections(tmp_path):
    agent = _agent(tmp_path)
    agent.memory.set_task_summary("current task")
    agent.memory.append_note("episodic fact", tags=("fact",))

    working = ContextSelection(
        tool_names=tuple(sorted(agent.tools)),
        history_indices=(),
        memory_scope="working",
    )
    agent.select_context = lambda _: working
    working_prompt, _ = agent._build_prompt_and_metadata("continue")
    assert "current task" in working_prompt
    assert "episodic fact" not in working_prompt
    assert "Working memory:" in working_prompt

    none = ContextSelection(
        tool_names=tuple(sorted(agent.tools)),
        history_indices=(),
        memory_scope="none",
    )
    agent.select_context = lambda _: none
    none_prompt, _ = agent._build_prompt_and_metadata("answer")
    assert "current task" not in none_prompt
    assert "episodic fact" not in none_prompt
    assert "Memory:\n- disabled" in none_prompt
