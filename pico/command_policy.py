"""Fast-fail policy for obviously destructive shell commands.

This is a preflight guard, not a sandbox: Seatbelt and approval remain the
authoritative controls for process and filesystem access.
"""

from __future__ import annotations

import re


class DangerousCommandError(ValueError):
    """Raised before a command reaches a shell process."""


_RULES = (
    ("privilege escalation", re.compile(r"(?:^|[;&|]\s*)sudo(?:\s|$)")),
    ("filesystem formatting", re.compile(r"(?:^|[;&|]\s*)mkfs(?:\.[A-Za-z0-9_+-]+)?(?:\s|$)")),
    ("raw device write", re.compile(r"\bdd\b[^\n]*\bof\s*=\s*/dev/")),
    ("root filesystem deletion", re.compile(r"\brm\b[^\n]*-[^\n]*r[^\n]*f[^\n]*\s+/(?:\s|$)")),
    ("system shutdown", re.compile(r"(?:^|[;&|]\s*)(?:shutdown|reboot|halt|poweroff)(?:\s|$)")),
    ("fork bomb", re.compile(r"\:\(\)\s*\{[^}]*\|[^}]*&[^}]*\}\s*;")),
)


def validate_shell_command(command: str):
    text = str(command or "").strip()
    for label, pattern in _RULES:
        if pattern.search(text):
            raise DangerousCommandError(f"shell command rejected by safety policy: {label}")
    return None
