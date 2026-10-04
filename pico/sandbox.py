"""OS-backed command sandboxing for risky shell tools.

The runtime deliberately keeps this adapter small: callers provide a workspace,
an environment, and a timeout; the adapter owns the platform-specific process
launch and failure-closed policy.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from typing import Callable


class SandboxUnavailableError(RuntimeError):
    """Raised when a requested OS sandbox cannot be enforced."""


def _profile_path(path: Path) -> str:
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


def seatbelt_profile(workspace: Path) -> str:
    """Build a deny-by-default profile for one workspace.

    Workspace access is intentionally the only writable project boundary. The
    system read paths are required for interpreters and tools to start, while
    networking remains denied even when a command tries to open a socket.
    """
    root = _profile_path(workspace.resolve())
    return "\n".join(
        [
            "(version 1)",
            "(deny default)",
            "(allow process-fork)",
            "(allow process-exec)",
            "(allow signal (target self))",
            "(allow sysctl-read)",
            '(allow file-read* (subpath "/usr"))',
            '(allow file-read* (subpath "/bin"))',
            '(allow file-read* (subpath "/System"))',
            '(allow file-read* (subpath "/Library"))',
            '(allow file-read* (subpath "/private/etc"))',
            '(allow file-read* (subpath "/dev"))',
            '(allow file-read* (subpath "/private/var"))',
            f'(allow file-read* (subpath "{root}"))',
            f'(allow file-write* (subpath "{root}"))',
            "(deny network*)",
        ]
    )


class SeatbeltExecutor:
    """Run shell commands through macOS ``sandbox-exec``."""

    def __init__(self, workspace: Path, runner: Callable | None = None, sandbox_exec: str | None = "auto"):
        self.workspace = Path(workspace).resolve()
        self.runner = runner
        if sandbox_exec == "auto":
            sandbox_exec = shutil.which("sandbox-exec")
        self.sandbox_exec = sandbox_exec

    def _command(self, command: str) -> list[str]:
        if not self.sandbox_exec:
            raise SandboxUnavailableError("sandbox-exec is unavailable; refusing unsandboxed shell execution")
        return [
            self.sandbox_exec,
            "-p",
            seatbelt_profile(self.workspace),
            "/bin/sh",
            "-c",
            command,
        ]

    def run(self, command: str, *, timeout: int, env: dict[str, str]):
        self.workspace.joinpath(".pico", "sandbox-tmp").mkdir(parents=True, exist_ok=True)
        child_env = dict(env)
        child_env["TMPDIR"] = str(self.workspace / ".pico" / "sandbox-tmp")
        argv = self._command(command)
        if self.runner is not None:
            return self.runner(argv, cwd=self.workspace, capture_output=True, text=True, timeout=timeout, env=child_env)

        process = subprocess.Popen(
            argv,
            cwd=self.workspace,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=child_env,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
            stdout = stdout or exc.stdout or ""
            stderr = stderr or exc.stderr or ""
            return subprocess.CompletedProcess(argv, 124, stdout, f"{stderr}\ncommand timed out after {timeout}s".strip())
        return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


def build_sandbox_executor(workspace: Path, mode: str = "auto"):
    """Return the configured executor, or ``None`` for explicit disabled mode."""
    mode = str(mode or "auto").strip().lower()
    if mode == "disabled":
        return None
    if mode not in {"auto", "seatbelt"}:
        raise ValueError("sandbox mode must be one of: auto, seatbelt, disabled")
    if sys.platform != "darwin":
        if mode == "seatbelt":
            raise SandboxUnavailableError("Seatbelt sandbox is only supported on macOS")
        return None
    executable = shutil.which("sandbox-exec")
    if not executable:
        if mode == "seatbelt":
            raise SandboxUnavailableError("sandbox-exec is unavailable; refusing unsandboxed shell execution")
        return None
    return SeatbeltExecutor(workspace, sandbox_exec=executable)
