from pathlib import Path

import pytest

from pico.sandbox import SandboxUnavailableError, SeatbeltExecutor, build_sandbox_executor


def test_seatbelt_executor_wraps_command_with_workspace_profile(tmp_path):
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return type("Result", (), {"returncode": 0, "stdout": "ok\n", "stderr": ""})()

    executor = SeatbeltExecutor(tmp_path, runner=runner, sandbox_exec="sandbox-exec")
    result = executor.run("printf ok", timeout=7, env={"PATH": "/bin"})

    assert result.stdout == "ok\n"
    command, kwargs = calls[0]
    assert command[:2] == ["sandbox-exec", "-p"]
    profile = command[2]
    assert f'(allow file-read* (subpath "{tmp_path}"))' in profile
    assert f'(allow file-write* (subpath "{tmp_path}"))' in profile
    assert '(deny network*)' in profile
    assert command[-3:] == ["/bin/sh", "-c", "printf ok"]
    assert kwargs["timeout"] == 7
    assert kwargs["env"]["PATH"] == "/bin"
    assert kwargs["env"]["TMPDIR"].startswith(str(tmp_path / ".pico" / "sandbox-tmp"))
    assert kwargs["cwd"] == tmp_path


def test_seatbelt_executor_fails_closed_when_backend_missing(tmp_path):
    executor = SeatbeltExecutor(tmp_path, sandbox_exec=None)

    with pytest.raises(SandboxUnavailableError, match="sandbox-exec"):
        executor.run("echo blocked", timeout=1, env={})


def test_build_sandbox_executor_supports_disabled_mode(tmp_path):
    executor = build_sandbox_executor(tmp_path, mode="disabled")

    assert executor is None


def test_build_sandbox_executor_rejects_unsupported_platform_mode(tmp_path, monkeypatch):
    monkeypatch.setattr("pico.sandbox.sys.platform", "linux")

    with pytest.raises(SandboxUnavailableError, match="macOS"):
        build_sandbox_executor(tmp_path, mode="seatbelt")
