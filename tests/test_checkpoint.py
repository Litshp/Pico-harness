from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.checkpoint import (
    CHECKPOINT_FULL_VALID_STATUS,
    CHECKPOINT_NONE_STATUS,
    CHECKPOINT_SCHEMA_MISMATCH_STATUS,
    CHECKPOINT_SCHEMA_VERSION,
    current_runtime_identity,
    evaluate_resume_state,
)


def build_agent(tmp_path, outputs=None, **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".pico" / "sessions")
    return Pico(
        model_client=FakeModelClient(outputs or []),
        workspace=workspace,
        session_store=store,
        approval_policy=kwargs.pop("approval_policy", "auto"),
        **kwargs,
    )


def test_current_runtime_identity_captures_execution_contract(tmp_path):
    agent = build_agent(tmp_path, max_steps=9, max_new_tokens=1024, read_only=True)

    identity = current_runtime_identity(agent)

    assert identity["session_id"] == agent.session["id"]
    assert identity["cwd"] == str(tmp_path)
    assert identity["read_only"] is True
    assert identity["max_steps"] == 9
    assert identity["max_new_tokens"] == 1024
    assert identity["workspace_fingerprint"] == agent.workspace.fingerprint()
    assert identity["tool_signature"] == agent.tool_signature()


def test_evaluate_resume_state_distinguishes_no_checkpoint_full_valid_and_schema_mismatch(tmp_path):
    agent = build_agent(tmp_path)

    assert evaluate_resume_state(agent)["status"] == CHECKPOINT_NONE_STATUS

    identity = current_runtime_identity(agent)
    agent.session["checkpoints"] = {
        "current_id": "ckpt_valid",
        "items": {
            "ckpt_valid": {
                "checkpoint_id": "ckpt_valid",
                "schema_version": CHECKPOINT_SCHEMA_VERSION,
                "key_files": [],
                "runtime_identity": identity,
            }
        },
    }
    assert evaluate_resume_state(agent)["status"] == CHECKPOINT_FULL_VALID_STATUS

    agent.session["checkpoints"]["items"]["ckpt_valid"]["schema_version"] = "old"
    assert evaluate_resume_state(agent)["status"] == CHECKPOINT_SCHEMA_MISMATCH_STATUS


def test_checkpoint_contains_structured_resume_facts_and_context_selection(tmp_path):
    agent = build_agent(tmp_path, ["<final>done</final>"])
    agent.memory.remember_file("README.md")
    agent.record({"role": "tool", "name": "run_shell", "args": {"command": "pytest -q"}, "content": "passed", "created_at": "1"})
    agent.current_context_selection = type("Selection", (), {"to_metadata": lambda self: {"source": "jev", "tool_names": ["run_shell"]}})()

    task_state = agent.current_task_state
    if task_state is None:
        from pico.task_state import TaskState

        task_state = TaskState.create(task_id="task_test", user_request="run tests", run_id="run_test")
    checkpoint = agent.create_checkpoint(task_state, "run tests", trigger="test")

    assert checkpoint["constraints"]["approval_policy"] == "auto"
    assert checkpoint["modified_files"] == ["README.md"]
    assert checkpoint["tests_run"] == ["pytest -q"]
    assert checkpoint["history_version"] == 1
    assert checkpoint["history_digest"]
    assert checkpoint["context_selection"]["source"] == "jev"


def test_resume_rejects_history_digest_drift(tmp_path):
    agent = build_agent(tmp_path)
    identity = current_runtime_identity(agent)
    agent.session["history"] = [{"role": "user", "content": "original"}]
    from pico.checkpoint import history_digest

    agent.session["checkpoints"] = {
        "current_id": "ckpt_history",
        "items": {
            "ckpt_history": {
                "checkpoint_id": "ckpt_history",
                "schema_version": CHECKPOINT_SCHEMA_VERSION,
                "key_files": [],
                "runtime_identity": identity,
                "history_version": 1,
                "history_digest": history_digest(agent.session["history"]),
            }
        },
    }
    agent.session["history"][0]["content"] = "changed"

    state = evaluate_resume_state(agent)
    assert state["status"] != CHECKPOINT_FULL_VALID_STATUS
    assert "history_digest" in state["runtime_identity_mismatch_fields"]
