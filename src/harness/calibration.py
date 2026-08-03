"""Deterministic human/LLM calibration aggregation for H6 qualification."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable


@dataclass(frozen=True)
class CalibrationPolicy:
    policy_version: str
    min_cases: int = 1
    min_agreement: float = 0.8
    max_false_accepts: int = 0
    required_hard_gate_categories: tuple[str, ...] = ("security", "acl")


def build_calibration_report(
    cases: Iterable[dict[str, Any]], policy: CalibrationPolicy
) -> dict[str, Any]:
    """Aggregate pre-labelled cases; fail closed on missing human labels."""
    rows = list(cases)
    if len(rows) < policy.min_cases:
        raise ValueError("calibration_sample_too_small")
    if not policy.policy_version:
        raise ValueError("calibration_policy_missing")
    categories = Counter()
    agreements = 0
    false_accepts = 0
    false_rejects = 0
    hard_gate_seen: set[str] = set()
    for case in rows:
        human = case.get("human_label")
        judge = case.get("judge_label")
        category = case.get("category", "general")
        if human not in {"pass", "fail"} or judge not in {"pass", "fail"}:
            raise ValueError("calibration_label_invalid")
        categories[category] += 1
        if category in policy.required_hard_gate_categories:
            hard_gate_seen.add(category)
        if human == judge:
            agreements += 1
        if human == "fail" and judge == "pass":
            false_accepts += 1
        if human == "pass" and judge == "fail":
            false_rejects += 1
    agreement = agreements / len(rows)
    missing = sorted(set(policy.required_hard_gate_categories) - hard_gate_seen)
    passed = (
        agreement >= policy.min_agreement
        and false_accepts <= policy.max_false_accepts
        and not missing
    )
    return {
        "policy_version": policy.policy_version,
        "sample_count": len(rows),
        "category_counts": dict(sorted(categories.items())),
        "agreement": agreement,
        "false_accepts": false_accepts,
        "false_rejects": false_rejects,
        "hard_gate_categories": sorted(hard_gate_seen),
        "missing_hard_gate_categories": missing,
        "passed": passed,
    }
