import pytest

from pico.command_policy import DangerousCommandError, validate_shell_command


@pytest.mark.parametrize(
    "command",
    [
        "sudo rm -rf build",
        "rm -rf /",
        "mkfs.ext4 /dev/disk2",
        "dd if=/dev/zero of=/dev/disk2",
        ":(){ :|:& };:",
        "shutdown -h now",
    ],
)
def test_rejects_high_risk_shell_commands(command):
    with pytest.raises(DangerousCommandError):
        validate_shell_command(command)


@pytest.mark.parametrize(
    "command",
    [
        "pytest -q",
        "git diff -- runtime.py",
        "rm -rf build",  # workspace-local cleanup is still approval-gated
        "python -m pip check",
    ],
)
def test_allows_normal_workspace_commands(command):
    assert validate_shell_command(command) is None


def test_empty_command_is_not_policy_error():
    assert validate_shell_command("   ") is None
