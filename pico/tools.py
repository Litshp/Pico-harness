"""工具定义与执行辅助逻辑。

可以把这个文件看成 agent 的能力白名单：模型能申请哪些动作、这些动作
如何做参数校验，以及最终如何执行，都是在这里定义的。
"""

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import textwrap
from functools import partial
from pathlib import Path

from .workspace import IGNORED_PATH_NAMES

BASE_TOOL_SPECS = {
    "list_files": {
        "schema": {"path": "str='.'"},
        "risky": False,
        "description": "List files in the workspace.",
    },
    "read_file": {
        "schema": {"path": "str", "start": "int=1", "end": "int=200"},
        "risky": False,
        "description": "Read a UTF-8 file by line range.",
    },
    "search": {
        "schema": {"pattern": "str", "path": "str='.'"},
        "risky": False,
        "description": "Search the workspace with rg or a simple fallback.",
    },
    "run_shell": {
        "schema": {"command": "str", "timeout": "int=20"},
        "risky": True,
        "description": "Run a shell command in the repo root.",
    },
    "write_file": {
        "schema": {"path": "str", "content": "str"},
        "risky": True,
        "description": "Write a text file.",
    },
    "patch_file": {
        "schema": {"path": "str", "old_text": "str", "new_text": "str"},
        "risky": True,
        "description": "Replace one exact text block in a file.",
    },
    "patch_file_v2": {
        "schema": {
            "patch": "str",
            "expected_hashes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "sha256": {"type": "string"},
                    },
                    "required": ["path", "sha256"],
                    "additionalProperties": False,
                },
            },
            "verify": {"type": "boolean"},
        },
        "risky": True,
        "description": "Apply a workspace-bounded unified diff, then run targeted verification.",
    },
}

DELEGATE_TOOL_SPEC = {
    "schema": {"task": "str", "max_steps": "int=3"},
    "risky": False,
    "description": "Ask a bounded read-only child agent to investigate.",
}


def legal_tool_names():
    return set(BASE_TOOL_SPECS) | {"delegate"}

TOOL_EXAMPLES = {
    "list_files": '<tool>{"name":"list_files","args":{"path":"."}}</tool>',
    "read_file": '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":80}}</tool>',
    "search": '<tool>{"name":"search","args":{"pattern":"binary_search","path":"."}}</tool>',
    "run_shell": '<tool>{"name":"run_shell","args":{"command":"uv run --with pytest python -m pytest -q","timeout":20}}</tool>',
    "write_file": '<tool name="write_file" path="binary_search.py"><content>def binary_search(nums, target):\n    return -1\n</content></tool>',
    "patch_file": '<tool name="patch_file" path="binary_search.py"><old_text>return -1</old_text><new_text>return mid</new_text></tool>',
    "patch_file_v2": '<tool>{"name":"patch_file_v2","args":{"patch":"--- a/app.py\\n+++ b/app.py\\n@@ -1 +1 @@\\n-old\\n+new\\n","expected_hashes":[],"verify":true}}</tool>',
    "delegate": '<tool>{"name":"delegate","args":{"task":"inspect README.md","max_steps":3}}</tool>',
}


def build_tool_registry(context):
    # 工具不是动态发现的，而是显式注册的。
    # 这样模型看到的是一个有边界、可审计的动作集合。
    tools = {
        name: {**spec, "run": partial(_TOOL_RUNNERS[name], context)}
        for name, spec in BASE_TOOL_SPECS.items()
    }
    # 子 agent 是刻意做成受限能力的：一旦深度耗尽，
    # 就连 delegate 这个工具都不再暴露给模型。
    if context.depth < context.max_depth:
        tools["delegate"] = {**DELEGATE_TOOL_SPEC, "run": partial(tool_delegate, context)}
    return tools


def tool_example(name):
    return TOOL_EXAMPLES.get(name, "")


def validate_tool(context, name, args):
    args = args or {}

    if name == "list_files":
        path = context.path(args.get("path", "."))
        if not path.is_dir():
            raise ValueError("path is not a directory")
        return

    if name == "read_file":
        path = context.path(args["path"])
        if not path.is_file():
            raise ValueError("path is not a file")
        start = int(args.get("start", 1))
        end = int(args.get("end", 200))
        if start < 1 or end < start:
            raise ValueError("invalid line range")
        return

    if name == "search":
        pattern = str(args.get("pattern", "")).strip()
        if not pattern:
            raise ValueError("pattern must not be empty")
        context.path(args.get("path", "."))
        return

    if name == "run_shell":
        command = str(args.get("command", "")).strip()
        if not command:
            raise ValueError("command must not be empty")
        timeout = int(args.get("timeout", 20))
        if timeout < 1 or timeout > 120:
            raise ValueError("timeout must be in [1, 120]")
        return

    if name == "write_file":
        path = context.path(args["path"])
        if path.exists() and path.is_dir():
            raise ValueError("path is a directory")
        if "content" not in args:
            raise ValueError("missing content")
        return

    if name == "patch_file":
        # patch_file 故意做得很严格：old_text 必须精确命中且只能出现一次，
        # 这样修改行为才是确定的，失败原因也更容易解释。
        path = context.path(args["path"])
        if not path.is_file():
            raise ValueError("path is not a file")
        old_text = str(args.get("old_text", ""))
        if not old_text:
            raise ValueError("old_text must not be empty")
        if "new_text" not in args:
            raise ValueError("missing new_text")
        text = path.read_text(encoding="utf-8")
        count = text.count(old_text)
        if count != 1:
            raise ValueError(f"old_text must occur exactly once, found {count}")
        return

    if name == "patch_file_v2":
        patch = str(args.get("patch", ""))
        paths = _validate_unified_patch(context, patch)
        expected_hashes = args.get("expected_hashes", [])
        if isinstance(expected_hashes, dict):
            expected_hashes = [
                {"path": raw_path, "sha256": expected}
                for raw_path, expected in expected_hashes.items()
            ]
        if not isinstance(expected_hashes, list):
            raise ValueError("expected_hashes must be an array")
        for item in expected_hashes:
            if not isinstance(item, dict) or not item.get("path") or not item.get("sha256"):
                raise ValueError("each expected_hashes item requires path and sha256")
            raw_path = item["path"]
            expected = item["sha256"]
            path = context.path(raw_path)
            relative = path.relative_to(context.root).as_posix()
            if relative not in paths:
                raise ValueError(f"expected hash path is not changed by patch: {relative}")
            if not path.is_file():
                raise ValueError(f"expected hash path is not a file: {relative}")
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != str(expected):
                raise ValueError(f"file hash mismatch for {relative}: expected {expected}, found {actual}")
        _check_unified_patch(context, patch)
        return

    if name == "delegate":
        task = str(args.get("task", "")).strip()
        if not task:
            raise ValueError("task must not be empty")
        if context.depth >= context.max_depth:
            raise ValueError("delegate depth exceeded")
        return


def tool_list_files(context, args):
    path = context.path(args.get("path", "."))
    if not path.is_dir():
        raise ValueError("path is not a directory")
    entries = [
        item for item in sorted(path.iterdir(), key=lambda item: (item.is_file(), item.name.lower()))
        if item.name not in IGNORED_PATH_NAMES
    ]
    lines = []
    for entry in entries[:200]:
        kind = "[D]" if entry.is_dir() else "[F]"
        lines.append(f"{kind} {entry.relative_to(context.root)}")
    return "\n".join(lines) or "(empty)"


def tool_read_file(context, args):
    path = context.path(args["path"])
    if not path.is_file():
        raise ValueError("path is not a file")
    start = int(args.get("start", 1))
    end = int(args.get("end", 200))
    if start < 1 or end < start:
        raise ValueError("invalid line range")
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    body = "\n".join(f"{number:>4}: {line}" for number, line in enumerate(lines[start - 1:end], start=start))
    return f"# {path.relative_to(context.root)}\n{body}"


def tool_search(context, args):
    pattern = str(args.get("pattern", "")).strip()
    if not pattern:
        raise ValueError("pattern must not be empty")
    path = context.path(args.get("path", "."))

    if shutil.which("rg"):
        # 优先用 rg，因为搜索会非常频繁，搜索延迟会直接影响 agent 控制循环。
        result = subprocess.run(
            ["rg", "-n", "--smart-case", "--max-count", "200", pattern, str(path)],
            cwd=context.root,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() or result.stderr.strip() or "(no matches)"

    matches = []
    files = [path] if path.is_file() else [
        item for item in path.rglob("*")
        if item.is_file() and not any(part in IGNORED_PATH_NAMES for part in item.relative_to(context.root).parts)
    ]
    for file_path in files:
        for number, line in enumerate(file_path.read_text(encoding="utf-8", errors="replace").splitlines(), start=1):
            if pattern.lower() in line.lower():
                matches.append(f"{file_path.relative_to(context.root)}:{number}:{line}")
                if len(matches) >= 200:
                    return "\n".join(matches)
    return "\n".join(matches) or "(no matches)"


def tool_run_shell(context, args):
    command = str(args.get("command", "")).strip()
    if not command:
        raise ValueError("command must not be empty")
    timeout = int(args.get("timeout", 20))
    if timeout < 1 or timeout > 120:
        raise ValueError("timeout must be in [1, 120]")
    # 沙箱执行器负责 OS 级隔离；显式 disabled 时才回退到原有受限环境执行。
    if context.sandbox_executor is not None:
        result = context.sandbox_executor.run(command, timeout=timeout, env=context.shell_env())
    else:
        result = subprocess.run(
            command,
            cwd=context.root,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            # 这里传入的是过滤后的环境变量，而不是直接继承整个父 shell 环境，
            # 目的是减少敏感信息被意外带进命令执行环境的风险。
            env=context.shell_env(),
        )
    return textwrap.dedent(
        f"""\
        exit_code: {result.returncode}
        stdout:
        {result.stdout.strip() or "(empty)"}
        stderr:
        {result.stderr.strip() or "(empty)"}
        """
    ).strip()


def tool_write_file(context, args):
    path = context.path(args["path"])
    content = str(args["content"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"wrote {path.relative_to(context.root)} ({len(content)} chars)"


def tool_patch_file(context, args):
    path = context.path(args["path"])
    if not path.is_file():
        raise ValueError("path is not a file")
    old_text = str(args.get("old_text", ""))
    if not old_text:
        raise ValueError("old_text must not be empty")
    if "new_text" not in args:
        raise ValueError("missing new_text")
    text = path.read_text(encoding="utf-8")
    count = text.count(old_text)
    if count != 1:
        raise ValueError(f"old_text must occur exactly once, found {count}")
    path.write_text(text.replace(old_text, str(args["new_text"]), 1), encoding="utf-8")
    return f"patched {path.relative_to(context.root)}"


def _patch_path(raw):
    value = str(raw).split("\t", 1)[0].strip()
    if value == "/dev/null":
        return ""
    if value.startswith(('"', "'")):
        raise ValueError("quoted patch paths are not supported")
    if value.startswith(("a/", "b/")):
        value = value[2:]
    return value


def _validate_unified_patch(context, patch):
    if not patch.strip():
        raise ValueError("patch must not be empty")
    forbidden = (
        "GIT binary patch",
        "Binary files ",
        "rename from ",
        "rename to ",
        "copy from ",
        "copy to ",
        "new file mode 120000",
        "old mode 120000",
        "new mode 120000",
    )
    if any(marker in patch for marker in forbidden):
        raise ValueError("binary, rename, copy, and symlink patches are not supported")
    paths = []
    for line in patch.splitlines():
        if not line.startswith(("--- ", "+++ ")):
            continue
        relative = _patch_path(line[4:])
        if not relative:
            continue
        path = context.path(relative)
        normalized = path.relative_to(context.root).as_posix()
        if normalized not in paths:
            paths.append(normalized)
    if not paths or "@@" not in patch:
        raise ValueError("patch must contain unified diff file headers and at least one hunk")
    return paths


def _check_unified_patch(context, patch):
    if not shutil.which("git"):
        raise ValueError("git is required to apply unified patches")
    result = subprocess.run(
        ["git", "apply", "--check", "--whitespace=nowarn", "-"],
        cwd=context.root,
        input=patch,
        capture_output=True,
        text=True,
        env=context.shell_env(),
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "patch does not apply"
        raise ValueError(detail)


def _run_verification_command(context, command, timeout=120):
    result = subprocess.run(
        command,
        cwd=context.root,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=context.shell_env(),
    )
    output = (result.stdout.strip() or result.stderr.strip() or "(empty)")
    return result.returncode, " ".join(command), output


def _targeted_verification(context, paths):
    python_paths = [path for path in paths if path.endswith(".py") and (context.root / path).exists()]
    if python_paths:
        direct_tests = [path for path in python_paths if path.startswith("tests/")]
        for path in python_paths:
            if path.startswith("tests/"):
                continue
            candidate = context.root / "tests" / f"test_{Path(path).stem}.py"
            if candidate.is_file():
                direct_tests.append(candidate.relative_to(context.root).as_posix())
        direct_tests = list(dict.fromkeys(direct_tests))
        if direct_tests and importlib.util.find_spec("pytest") is not None:
            return _run_verification_command(context, [sys.executable, "-m", "pytest", "-q", *direct_tests])
        return _run_verification_command(context, [sys.executable, "-m", "compileall", "-q", *python_paths])

    javascript_paths = [
        path for path in paths
        if Path(path).suffix in {".js", ".mjs", ".cjs"} and (context.root / path).exists()
    ]
    node = shutil.which("node")
    if javascript_paths and node:
        commands = []
        outputs = []
        exit_code = 0
        for path in javascript_paths:
            code, command, output = _run_verification_command(context, [node, "--check", path])
            commands.append(command)
            outputs.append(output)
            exit_code = exit_code or code
        return exit_code, " && ".join(commands), "\n".join(outputs)

    json_paths = [path for path in paths if path.endswith(".json") and (context.root / path).exists()]
    if json_paths:
        try:
            for path in json_paths:
                json.loads((context.root / path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return 1, "json.loads", str(exc)
        return 0, "json.loads", f"validated {len(json_paths)} JSON file(s)"
    return 0, "none", "skipped: no targeted verifier for changed file types"


def tool_patch_file_v2(context, args):
    patch = str(args.get("patch", ""))
    paths = _validate_unified_patch(context, patch)
    result = subprocess.run(
        ["git", "apply", "--whitespace=nowarn", "-"],
        cwd=context.root,
        input=patch,
        capture_output=True,
        text=True,
        env=context.shell_env(),
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "patch failed"
        raise RuntimeError(detail)
    lines = [f"patched {len(paths)} file(s): {', '.join(paths)}"]
    if bool(args.get("verify", True)):
        exit_code, command, output = _targeted_verification(context, paths)
        lines.extend(
            [
                f"verification_exit_code: {exit_code}",
                f"verification_command: {command}",
                "verification_output:",
                output,
            ]
        )
    return "\n".join(lines)


def tool_delegate(context, args):
    if context.depth >= context.max_depth:
        raise ValueError("delegate depth exceeded")
    task = str(args.get("task", "")).strip()
    if not task:
        raise ValueError("task must not be empty")
    return context.spawn_delegate(args)


_TOOL_RUNNERS = {
    "list_files": tool_list_files,
    "read_file": tool_read_file,
    "search": tool_search,
    "run_shell": tool_run_shell,
    "write_file": tool_write_file,
    "patch_file": tool_patch_file,
    "patch_file_v2": tool_patch_file_v2,
}
