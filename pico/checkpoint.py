"""Checkpoint and resume-state helpers."""

import uuid

from .features import memory as memorylib
from .workspace import clip, now

CHECKPOINT_SCHEMA_VERSION = "phase1-v1"
CHECKPOINT_NONE_STATUS = "no-checkpoint"
CHECKPOINT_FULL_VALID_STATUS = "full-valid"
CHECKPOINT_PARTIAL_STALE_STATUS = "partial-stale"
CHECKPOINT_WORKSPACE_MISMATCH_STATUS = "workspace-mismatch"
CHECKPOINT_SCHEMA_MISMATCH_STATUS = "schema-mismatch"

RUNTIME_IDENTITY_KEYS = (
    "cwd",
    "model",
    "model_client",
    "approval_policy",
    "read_only",
    "max_steps",
    "max_new_tokens",
    "feature_flags",
    "shell_env_allowlist",
    "workspace_fingerprint",
    "tool_signature",
)


def current_runtime_identity(agent):
    return {
        "session_id": agent.session.get("id", ""),
        "cwd": str(agent.root),
        "model": str(getattr(agent.model_client, "model", "")),
        "model_client": agent.model_client.__class__.__name__,
        "approval_policy": agent.approval_policy,
        "read_only": bool(agent.read_only),
        "max_steps": int(agent.max_steps),
        "max_new_tokens": int(agent.max_new_tokens),
        "feature_flags": dict(agent.feature_flags),
        "shell_env_allowlist": list(agent.shell_env_allowlist),
        "workspace_fingerprint": getattr(getattr(agent, "prefix_state", None), "workspace_fingerprint", agent.workspace.fingerprint()),
        "tool_signature": agent.tool_signature(),
    }


def checkpoint_state(agent):
    agent._ensure_session_shape()
    return agent.session["checkpoints"]


def current_checkpoint(agent):
    state = checkpoint_state(agent)
    checkpoint_id = str(state.get("current_id", "")).strip()
    if not checkpoint_id:
        return None
    return state.get("items", {}).get(checkpoint_id)


def evaluate_resume_state(agent):
    previous_resume_state = dict(agent.session.get("resume_state", {}) or {})
    invalidated = agent.invalidate_stale_memory()
    checkpoint = current_checkpoint(agent)
    status = CHECKPOINT_NONE_STATUS
    stale_paths = list(invalidated)
    mismatch_fields = []
    if checkpoint:
        if checkpoint.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
            status = CHECKPOINT_SCHEMA_MISMATCH_STATUS
        else:
            for item in checkpoint.get("key_files", []):
                path = str(item.get("path", "")).strip()
                if not path:
                    continue
                expected = item.get("freshness")
                current = memorylib.file_freshness(path, agent.root)
                if expected != current and path not in stale_paths:
                    stale_paths.append(path)
            saved_identity = dict(checkpoint.get("runtime_identity", {}) or agent.session.get("runtime_identity", {}) or {})
            current_identity = current_runtime_identity(agent)
            for key in RUNTIME_IDENTITY_KEYS:
                if key not in saved_identity:
                    continue
                if saved_identity.get(key) != current_identity.get(key):
                    mismatch_fields.append(key)
            saved_history_version = checkpoint.get("history_version")
            if saved_history_version is not None and int(saved_history_version) != len(agent.session.get("history", [])):
                mismatch_fields.append("history_version")
            mismatch_fields.sort()
            if stale_paths:
                status = CHECKPOINT_PARTIAL_STALE_STATUS
            elif mismatch_fields:
                status = CHECKPOINT_WORKSPACE_MISMATCH_STATUS
            else:
                status = CHECKPOINT_FULL_VALID_STATUS

    resume_state = {
        "status": status,
        "stale_paths": stale_paths,
        "runtime_identity_mismatch_fields": mismatch_fields,
        "stale_summary_invalidations": max(
            len(invalidated),
            int(previous_resume_state.get("stale_summary_invalidations", 0))
            if status == CHECKPOINT_PARTIAL_STALE_STATUS
            else 0,
        ),
    }
    agent.session["resume_state"] = resume_state
    agent.session["runtime_identity"] = current_runtime_identity(agent)
    return resume_state


def render_checkpoint_text(agent):
    checkpoint = current_checkpoint(agent)
    if not checkpoint:
        return ""
    lines = [
        "Task checkpoint:",
        f"- Resume status: {agent.resume_state.get('status', CHECKPOINT_NONE_STATUS)}",
        f"- Current goal: {checkpoint.get('current_goal', '-') or '-'}",
        f"- Current blocker: {checkpoint.get('current_blocker', '-') or '-'}",
        f"- Key files: {', '.join(str(item.get('path', '')).strip() for item in checkpoint.get('key_files', []) if str(item.get('path', '')).strip()) or '-'}",
    ]
    if checkpoint.get("completed"):
        lines.append("- Completed: " + " | ".join(str(item) for item in checkpoint.get("completed", [])))
    if checkpoint.get("completed_actions"):
        lines.append("- Completed actions: " + " | ".join(
            f"{item.get('tool', 'tool')}({clip(str(item.get('result', '')), 120)})"
            for item in checkpoint.get("completed_actions", [])[-6:]
        ))
    if checkpoint.get("modified_files"):
        lines.append("- Modified files: " + ", ".join(str(item) for item in checkpoint.get("modified_files", [])))
    if checkpoint.get("tests_run"):
        lines.append("- Tests run: " + " | ".join(str(item) for item in checkpoint.get("tests_run", [])[-4:]))
    if checkpoint.get("known_failures"):
        lines.append("- Known failures: " + " | ".join(str(item) for item in checkpoint.get("known_failures", [])[-4:]))
    if checkpoint.get("excluded"):
        lines.append("- Excluded: " + " | ".join(str(item) for item in checkpoint.get("excluded", [])))
    if agent.resume_state.get("stale_paths"):
        lines.append("- Stale paths: " + ", ".join(agent.resume_state["stale_paths"]))
    summary = str(checkpoint.get("summary", "")).strip()
    if summary:
        lines.append(f"- Summary: {summary}")
    # Keep the actionable continuation at the end so tail-based context
    # clipping retains it when older workspace details consume the prefix budget.
    lines.append(f"- Next step: {checkpoint.get('next_step', '-') or '-'}")
    return "\n".join(lines)


def infer_next_step(task_state):
    if task_state.status == "completed":
        return "No next step recorded."
    if task_state.stop_reason == "step_limit_reached":
        return "Resume from the latest checkpoint and continue the task."
    if task_state.last_tool:
        return f"Decide the next action after {task_state.last_tool}."
    return "Continue the task from the latest checkpoint."


def create_checkpoint(agent, task_state, user_message, trigger):
    state = checkpoint_state(agent)
    current = current_checkpoint(agent)
    checkpoint_id = "ckpt_" + uuid.uuid4().hex[:8]
    key_files = []
    freshness = {}
    for path in agent.memory.to_dict()["working"]["recent_files"]:
        file_freshness = memorylib.file_freshness(path, agent.root)
        freshness[path] = file_freshness
        key_files.append({"path": path, "freshness": file_freshness})
    history = list(agent.session.get("history", []))
    completed_actions = [
        {
            "tool": str(item.get("name", "")),
            "args": dict(item.get("args", {}) or {}),
            "result": clip(str(item.get("content", "")), 240),
        }
        for item in history
        if item.get("role") == "tool" and str(item.get("name", "")).strip()
    ]
    tests_run = [
        str(item.get("args", {}).get("command", "")).strip()
        for item in history
        if item.get("role") == "tool"
        and item.get("name") == "run_shell"
        and any(token in str(item.get("args", {}).get("command", "")).lower() for token in ("test", "pytest", "check"))
    ]
    known_failures = [
        clip(str(item.get("content", "")), 240)
        for item in history
        if item.get("role") == "tool"
        and any(marker in str(item.get("content", "")).lower() for marker in ("error", "failed", "traceback", "rejected"))
    ]
    selection = getattr(agent, "current_context_selection", None)
    checkpoint = {
        "checkpoint_id": checkpoint_id,
        "parent_checkpoint_id": current.get("checkpoint_id", "") if current else "",
        "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "created_at": now(),
        "current_goal": str(user_message),
        "completed": [task_state.final_answer] if task_state.final_answer else [],
        "excluded": [],
        "current_blocker": "" if str(task_state.stop_reason or "") in ("", "final_answer_returned") else str(task_state.stop_reason),
        "next_step": infer_next_step(task_state),
        "key_files": key_files,
        "freshness": freshness,
        "summary": f"{trigger}: {clip(str(user_message), 120)}",
        "runtime_identity": current_runtime_identity(agent),
        # Structured resume facts make continuation independent from a full transcript.
        "constraints": {
            "approval_policy": agent.approval_policy,
            "read_only": bool(agent.read_only),
            "max_steps": int(agent.max_steps),
        },
        "completed_actions": completed_actions,
        "modified_files": list(agent.memory.to_dict()["working"]["recent_files"]),
        "tests_run": tests_run,
        "known_failures": known_failures,
        "history_version": len(history),
        "context_selection": selection.to_metadata() if selection is not None else {},
    }
    state["items"][checkpoint_id] = checkpoint
    state["current_id"] = checkpoint_id
    task_state.checkpoint_id = checkpoint_id
    agent.session["runtime_identity"] = checkpoint["runtime_identity"]
    agent.session_path = agent.session_store.save(agent.session)
    return checkpoint
