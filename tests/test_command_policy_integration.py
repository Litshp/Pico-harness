from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext


def test_dangerous_shell_command_is_rejected_before_execution(tmp_path):
    workspace = WorkspaceContext.build(tmp_path)
    agent = Pico(
        model_client=FakeModelClient([]),
        workspace=workspace,
        session_store=SessionStore(tmp_path / ".pico" / "sessions"),
        approval_policy="auto",
        sandbox_mode="disabled",
    )

    result = agent.execute_tool("run_shell", {"command": "sudo rm -rf /", "timeout": 20})

    assert "safety policy" in result.content
    assert result.metadata["security_event_type"] == "dangerous_command"
    assert result.metadata["tool_error_code"] == "invalid_arguments"
