"""Validate a reviewed Phase 0 baseline result file without model dependencies."""

import argparse
import json
from pathlib import Path

import yaml


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("results", type=Path, help="JSON array of reviewed task results")
    parser.add_argument("--plan", type=Path, default=Path("eval/phase0_baseline.yaml"))
    args = parser.parse_args()

    plan = yaml.safe_load(args.plan.read_text(encoding="utf-8"))
    results = {item["id"]: item for item in json.loads(args.results.read_text(encoding="utf-8"))}
    expected = {task["id"] for task in plan["tasks"]}
    missing = expected - results.keys()
    failed = [task_id for task_id in expected if not results.get(task_id, {}).get("success")]
    if missing or failed:
        raise SystemExit(f"baseline incomplete: missing={sorted(missing)}, failed={sorted(failed)}")
    print(
        f"Phase 0 baseline passed: {len(expected)} tasks and {len(plan['memory_questions'])} questions"
    )


if __name__ == "__main__":
    main()
