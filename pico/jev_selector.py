"""Jev-powered dynamic context selection.

Jev is deliberately kept outside the execution path.  It only chooses a
bounded context shape; Pico still owns tool validation, approval, sandboxing,
and execution.  When the service is unavailable (or confidence is low), the
selector returns a deterministic safe fallback.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from .workspace import clip


INSPECT_TOOLS = ("list_files", "read_file", "search", "delegate")
MODIFY_TOOLS = ("read_file", "search", "write_file", "patch_file", "patch_file_v2")
EXECUTE_TOOLS = ("read_file", "search", "run_shell")
DELEGATE_TOOLS = ("delegate", "list_files", "read_file", "search")


@dataclass
class ContextSelection:
    tool_names: tuple[str, ...]
    history_indices: tuple[int, ...] | None = None
    memory_notes: list[dict] = field(default_factory=list)
    memory_scope: str = "relevant"
    source: str = "fallback"
    confidence: float = 0.0
    fallback: bool = True
    reason: str = ""

    def to_metadata(self):
        return {
            "source": self.source,
            "confidence": self.confidence,
            "fallback": self.fallback,
            "reason": self.reason,
            "tool_names": list(self.tool_names),
            "history_indices": list(self.history_indices) if self.history_indices is not None else None,
            "memory_note_count": len(self.memory_notes),
            "memory_scope": self.memory_scope,
        }


def _answer_value(answer, name, default=None):
    if answer is None:
        return default
    if isinstance(answer, dict):
        return answer.get(name, default)
    return getattr(answer, name, default)


class JevContextSelector:
    """Select the smallest useful context shape for one prompt build."""

    def __init__(self, agent, client=None, enabled=None, model=None):
        self.agent = agent
        self.model = model or os.environ.get("TYPESAFE_MODEL", "jev-1.12")
        self.enabled = bool(
            os.environ.get("PICO_JEV_ENABLED", "1").strip().lower() not in {"0", "false", "no"}
            if enabled is None
            else enabled
        )
        self.client = client
        self.last_selection = None
        self.last_error = ""
        if self.client is None and self.enabled:
            api_key = os.environ.get("TYPESAFE_API_KEY", "").strip()
            if api_key:
                try:
                    from typesafe_sdk import TypeSafeClient

                    self.client = TypeSafeClient(api_key=api_key, model=self.model, timeout=8)
                except Exception as exc:  # optional integration must not break Pico
                    self.last_error = f"client_init:{type(exc).__name__}"

    def fallback(self, tools, history, notes, reason="jev_disabled"):
        selection = ContextSelection(
            tool_names=tuple(sorted(tools)),
            history_indices=None,
            memory_notes=list(notes),
            memory_scope="relevant",
            source="fallback",
            confidence=0.0,
            fallback=True,
            reason=reason,
        )
        self.last_selection = selection
        return selection

    def select(self, user_message, tools, history, notes):
        notes = list(notes or [])
        if not self.enabled or self.client is None:
            return self.fallback(tools, history, notes, self.last_error or "jev_disabled")

        try:
            from typesafe_sdk import Choice

            tool_candidates = {
                name: {
                    "description": str(spec.get("description", "")),
                    "risky": bool(spec.get("risky")),
                    "schema": spec.get("schema", {}),
                }
                for name, spec in sorted(tools.items())
            }
            history_candidates = [
                {
                    "index": index,
                    "role": item.get("role", ""),
                    "name": item.get("name", ""),
                    "content": clip(item.get("content", ""), 240),
                }
                for index, item in enumerate(history[-16:], start=max(0, len(history) - 16))
            ]
            state = {
                "request": clip(user_message, 1200),
                "tool_candidates": tool_candidates,
                "history_candidates": history_candidates,
                "memory_candidates": [
                    {"index": index, "source": note.get("source", ""), "text": clip(note.get("text", ""), 240)}
                    for index, note in enumerate(notes)
                ],
            }
            response = self.client.system_one(
                state=state,
                questions={
                    "tool_scope": Choice(
                        instructions="Choose the smallest safe tool group needed for the current request.",
                        criteria={
                            "inspect": "Only inspect or search repository files.",
                            "modify": "Inspect and modify repository files.",
                            "execute": "Inspect files and run tests or commands.",
                            "delegate": "Use a bounded read-only investigation child.",
                            "none": "No tool is needed; answer from the supplied context.",
                        },
                    ),
                    "history_scope": Choice(
                        instructions="Choose how much prior history is useful for this request.",
                        criteria={
                            "recent": "Keep only the latest few events.",
                            "relevant": "Keep recent events and events semantically relevant to the request.",
                            "minimal": "Keep the smallest possible history slice.",
                        },
                    ),
                    "memory_scope": Choice(
                        instructions="Choose whether retrieved notes are useful for this request.",
                        criteria={
                            "relevant": "Include relevant working and episodic notes.",
                            "working": "Use working memory but omit episodic notes.",
                            "none": "Omit retrieved notes.",
                        },
                    ),
                },
                model=self.model,
            )
            answers = getattr(response, "answers", None) or (response.get("answers", {}) if isinstance(response, dict) else {})
            scope_answer = answers.get("tool_scope")
            history_answer = answers.get("history_scope")
            memory_answer = answers.get("memory_scope")
            confidence = min(
                float(_answer_value(scope_answer, "confidence", 0.0) or 0.0),
                float(_answer_value(history_answer, "confidence", 0.0) or 0.0),
                float(_answer_value(memory_answer, "confidence", 0.0) or 0.0),
            )
            if confidence < 0.55:
                return self.fallback(tools, history, notes, "low_confidence")

            scope = str(_answer_value(scope_answer, "choice", "inspect"))
            allowed = {
                "inspect": INSPECT_TOOLS,
                "modify": MODIFY_TOOLS,
                "execute": EXECUTE_TOOLS,
                "delegate": DELEGATE_TOOLS,
                "none": (),
            }.get(scope, INSPECT_TOOLS)
            selected_tools = tuple(name for name in allowed if name in tools)
            if scope == "none":
                selected_tools = ()
            history_scope = str(_answer_value(history_answer, "choice", "recent"))
            history_indices = self._select_history(history, user_message, history_scope)
            memory_scope = str(_answer_value(memory_answer, "choice", "relevant"))
            selected_notes = notes if memory_scope == "relevant" else ([] if memory_scope == "none" else [])
            selection = ContextSelection(
                tool_names=selected_tools,
                history_indices=history_indices,
                memory_notes=selected_notes,
                memory_scope=memory_scope,
                source="jev",
                confidence=confidence,
                fallback=False,
                reason=f"tool_scope={scope};history_scope={history_scope};memory_scope={memory_scope}",
            )
            self.last_selection = selection
            return selection
        except Exception as exc:  # service errors fall back without affecting execution
            self.last_error = f"jev_request:{type(exc).__name__}"
            return self.fallback(tools, history, notes, self.last_error)

    @staticmethod
    def _select_history(history, request, scope):
        if not history:
            return ()
        if scope == "minimal":
            return tuple(range(max(0, len(history) - 4), len(history)))
        if scope == "recent":
            return tuple(range(max(0, len(history) - 6), len(history)))
        tokens = {token.lower() for token in str(request).split() if len(token) > 2}
        scored = []
        for index, item in enumerate(history):
            text = f"{item.get('name', '')} {item.get('content', '')}".lower()
            score = len(tokens & set(text.split()))
            if index >= len(history) - 6:
                score += 1
            scored.append((score, index))
        selected = sorted(scored, key=lambda pair: (pair[0], pair[1]), reverse=True)[:10]
        return tuple(sorted(index for score, index in selected if score > 0)) or tuple(range(max(0, len(history) - 6), len(history)))
