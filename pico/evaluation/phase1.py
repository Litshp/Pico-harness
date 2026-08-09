"""Real-provider ablation for Pico's phase-one reliability features."""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path

from .evaluator import BenchmarkEvaluator, DEFAULT_BENCHMARK_PATH
from .metrics import _make_provider_client


@dataclass
class ToolProtocolOverride:
    client: object
    native_tools: bool

    def __post_init__(self):
        self.supports_prompt_cache = bool(getattr(self.client, "supports_prompt_cache", False))
        self.supports_native_tools = bool(
            self.native_tools and getattr(self.client, "supports_native_tools", False)
        )
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens, **kwargs):
        if not self.supports_native_tools:
            kwargs.pop("tools", None)
        result = self.client.complete(prompt, max_new_tokens, **kwargs)
        self.last_completion_metadata = dict(
            getattr(self.client, "last_completion_metadata", {}) or {}
        )
        return result


def _phase1_benchmark(benchmark_path, output_path):
    source = json.loads(Path(benchmark_path).read_text(encoding="utf-8"))
    for task in source.get("tasks", []):
        allowed = list(task.get("allowed_tools", []))
        if "patch_file" in allowed and "patch_file_v2" not in allowed:
            allowed.append("patch_file_v2")
        task["allowed_tools"] = allowed
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(source, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return output_path


def _row_metrics(rows):
    rows = list(rows)
    count = len(rows)
    attempts = sum(int(row.get("attempts", 0)) for row in rows)
    malformed = sum(int(row.get("malformed_response_count", 0)) for row in rows)
    return {
        "task_runs": count,
        "pass_rate": sum(1 for row in rows if row.get("passed")) / count if count else 0.0,
        "verifier_pass_rate": sum(1 for row in rows if row.get("verifier_passed")) / count if count else 0.0,
        "within_budget_rate": sum(1 for row in rows if row.get("within_budget")) / count if count else 0.0,
        "avg_tool_steps": sum(int(row.get("tool_steps", 0)) for row in rows) / count if count else 0.0,
        "avg_attempts": attempts / count if count else 0.0,
        "malformed_tool_rate": malformed / attempts if attempts else 0.0,
        "native_tool_call_count": sum(int(row.get("native_tool_call_count", 0)) for row in rows),
        "patch_failure_count": sum(int(row.get("patch_failure_count", 0)) for row in rows),
        "verification_failure_count": sum(int(row.get("verification_failure_count", 0)) for row in rows),
    }


def run_real_phase1_ablation(
    provider="gpt",
    benchmark_path=DEFAULT_BENCHMARK_PATH,
    artifact_path=Path("artifacts/phase1-reliability-ablation.json"),
    workspace_root=Path("artifacts/phase1-reliability-workspaces"),
    repetitions=3,
    max_new_tokens=1024,
):
    provider = str(provider)
    repetitions = int(repetitions)
    artifact_path = Path(artifact_path)
    workspace_root = Path(workspace_root)
    source_repo_root = Path(benchmark_path).resolve().parent.parent
    derived_benchmark = _phase1_benchmark(
        benchmark_path,
        artifact_path.parent / "phase1-coding-tasks.json",
    )

    probe = _make_provider_client(provider)
    native_supported = bool(getattr(probe, "supports_native_tools", False))
    variants = {}
    for variant, native_tools in (("native_tools", True), ("text_protocol", False)):
        rows = []
        if native_tools and not native_supported:
            variants[variant] = {
                "status": "unsupported",
                "summary": _row_metrics([]),
                "rows": [],
            }
            continue
        for repetition in range(repetitions):
            run_root = workspace_root / variant / f"repeat-{repetition + 1}"
            evaluator = BenchmarkEvaluator(
                benchmark_path=derived_benchmark,
                artifact_path=artifact_path.parent / "phase1-runs" / f"{variant}-{repetition + 1}.json",
                workspace_root=run_root,
                model_name=provider,
                model_version=str(getattr(probe, "model", "configured")),
                max_new_tokens=max_new_tokens,
                model_client_factory=lambda task, workspace, enabled=native_tools: ToolProtocolOverride(
                    _make_provider_client(provider),
                    enabled,
                ),
                repo_root=source_repo_root,
            )
            result = evaluator.run()
            for row in result["rows"]:
                row["repetition"] = repetition + 1
                rows.append(row)
        variants[variant] = {
            "status": "completed",
            "summary": _row_metrics(rows),
            "rows": rows,
        }

    artifact = {
        "schema_version": 1,
        "artifact_type": "phase1-reliability-ablation",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "provider": provider,
        "model": str(getattr(probe, "model", "configured")),
        "benchmark_path": str(benchmark_path),
        "repetitions": repetitions,
        "native_tools_supported": native_supported,
        "variants": variants,
    }
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return artifact
