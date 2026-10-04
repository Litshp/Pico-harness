from unittest.mock import patch

from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.sandbox import SeatbeltExecutor


def test_run_shell_uses_configured_sandbox_executor(tmp_path):
    workspace = WorkspaceContext.build(tmp_path)
    executor = SeatbeltExecutor(tmp_path, runner=lambda *args, **kwargs: type(
        "Result", (), {"returncode": 0, "stdout": "sandboxed\n", "stderr": ""}
    )(), sandbox_exec="sandbox-exec")
    with patch("pico.runtime.build_sandbox_executor", return_value=executor):
        agent = Pico(
            model_client=FakeModelClient([]),
            workspace=workspace,
            session_store=SessionStore(tmp_path / ".pico" / "sessions"),
            approval_policy="auto",
            sandbox_mode="seatbelt",
        )

    with patch("pico.tools.subprocess.run") as unsandboxed:
        result = agent.run_tool("run_shell", {"command": "echo ignored", "timeout": 20})

    assert "sandboxed" in result
    unsandboxed.assert_not_called()


def test_cli_exposes_sandbox_mode(tmp_path):
    from pico import cli

    args = cli.build_arg_parser().parse_args(["--cwd", str(tmp_path), "--sandbox", "seatbelt"])
    assert args.sandbox == "seatbelt"
