#!/usr/bin/env python3

import argparse
from pathlib import Path

from pico.evaluation.phase1 import run_real_phase1_ablation


def main():
    parser = argparse.ArgumentParser(description="Compare native tool calling with Pico's text protocol.")
    parser.add_argument("--provider", default="gpt", choices=("gpt", "deepseek", "anthropic"))
    parser.add_argument("--benchmark", type=Path, default=Path("benchmarks/coding_tasks.json"))
    parser.add_argument("--artifact", type=Path, default=Path("artifacts/phase1-reliability-ablation.json"))
    parser.add_argument("--workspace-root", type=Path, default=Path("artifacts/phase1-reliability-workspaces"))
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--max-new-tokens", type=int, default=1024)
    args = parser.parse_args()
    artifact = run_real_phase1_ablation(
        provider=args.provider,
        benchmark_path=args.benchmark,
        artifact_path=args.artifact,
        workspace_root=args.workspace_root,
        repetitions=args.repetitions,
        max_new_tokens=args.max_new_tokens,
    )
    native = artifact["variants"]["native_tools"]
    text = artifact["variants"]["text_protocol"]
    print(f"provider={artifact['provider']} model={artifact['model']}")
    print(f"native_tools={native['status']} {native['summary']}")
    print(f"text_protocol={text['status']} {text['summary']}")
    print(f"artifact={args.artifact}")


if __name__ == "__main__":
    main()
