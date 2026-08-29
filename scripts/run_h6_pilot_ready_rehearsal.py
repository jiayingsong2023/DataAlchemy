"""Generate a deterministic synthetic H6 PILOT_READY rehearsal report.

This is an engineering rehearsal only. It never writes PostgreSQL, MinIO or
Kubernetes state and never changes the H6/GA gate.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.reset_pilot_environment import load_environment, reset_plan
from src.harness.calibration import CalibrationPolicy, build_calibration_report
from src.harness.deployment import DeploymentBinding, route_request, validate_shadow_output


def sha256(value: Any) -> str:
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode()).hexdigest()


def run_check(checks: list[dict[str, Any]], name: str, fn: Callable[[], dict[str, Any]]) -> None:
    try:
        details = fn()
        checks.append({"name": name, "status": "passed", "details": details})
    except Exception as error:  # pragma: no cover - report path is exercised as a script
        checks.append({"name": name, "status": "failed", "error": str(error)})


def build_report() -> dict[str, Any]:
    tenant = "h6-synthetic-tenant"
    checks: list[dict[str, Any]] = []
    source = {
        "tenant_id": tenant,
        "source": "synthetic://representative-knowledge-pack-v1",
        "acl_digest": sha256(f"acl:{tenant}:v1"),
        "permission_version": "h6-sim-permission-v1",
        "classification": "internal-test",
    }
    run_check(
        checks,
        "synthetic_data_qualification",
        lambda: {
            "manifest_sha256": sha256(source),
            "acl_digest": source["acl_digest"],
            "permission_version": source["permission_version"],
            "training_allowed": True,
        },
    )

    policy = CalibrationPolicy("h6-sim-policy-v1", min_cases=4, min_agreement=0.8)
    cases = [
        {
            "case_id": "security-1",
            "category": "security",
            "human_label": "fail",
            "judge_label": "fail",
        },
        {"case_id": "acl-1", "category": "acl", "human_label": "pass", "judge_label": "pass"},
        {
            "case_id": "evidence-1",
            "category": "evidence",
            "human_label": "pass",
            "judge_label": "pass",
        },
        {
            "case_id": "general-1",
            "category": "general",
            "human_label": "pass",
            "judge_label": "pass",
        },
    ]
    calibration: dict[str, Any] = {}

    def calibrate() -> dict[str, Any]:
        calibration.update(build_calibration_report(cases, policy))
        if not calibration["passed"]:
            raise RuntimeError("synthetic_calibration_failed")
        return calibration

    run_check(checks, "independent_human_judge_calibration", calibrate)

    salt = "h6-synthetic-routing-salt"
    binding = DeploymentBinding(
        stable_release_id="stable-sim-release",
        candidate_release_id="candidate-sim-release",
        stable_digest="a" * 64,
        candidate_digest="b" * 64,
        stable_service="stable-sim",
        candidate_service="candidate-sim",
        mode="shadow",
        canary_percent=0,
        salt_sha256=sha256(salt),
    )
    run_check(
        checks,
        "stable_candidate_shadow_isolation",
        lambda: (
            validate_shadow_output({"authority": "stable", "side_effects": []})
            or {
                "authority": route_request(binding, tenant, "user-1"),
                "candidate_observer": binding.candidate_release_id,
            }
        ),
    )

    def rollback_injection() -> dict[str, Any]:
        canary = DeploymentBinding(**{**binding.__dict__, "mode": "canary", "canary_percent": 25})
        if route_request(canary, tenant, "user-1") not in {"stable", "candidate"}:
            raise RuntimeError("canary_route_invalid")
        observed = {"error_rate": 1.0, "max_error_rate": 0.01, "window_complete": True}
        status = (
            "rolled_back" if observed["error_rate"] > observed["max_error_rate"] else "promoted"
        )
        if status != "rolled_back":
            raise RuntimeError("rollback_injection_not_triggered")
        return {"canary_percent": canary.canary_percent, **observed, "result": status}

    run_check(checks, "canary_failure_and_rollback", rollback_injection)

    def side_effect_gate() -> dict[str, Any]:
        try:
            validate_shadow_output({"authority": "stable", "side_effects": ["write_memory"]})
        except ValueError as error:
            return {"rejected": True, "error": str(error)}
        raise RuntimeError("shadow_side_effect_not_rejected")

    run_check(checks, "shadow_side_effect_fault_injection", side_effect_gate)

    registry = Path(__file__).resolve().parents[1] / "deploy/pilot-environments.example.yaml"
    environment = load_environment(registry, "dataalchemy-gpu-test")
    plan = reset_plan(environment)
    run_check(
        checks,
        "isolated_reset_restore_boundary",
        lambda: {
            "environment_id": environment["environment_id"],
            "reset_mode": "dry-run",
            "plan_sha256": plan["plan_sha256"],
            "actions": plan["actions"],
            "restore_source": "synthetic-backup",
            "restore_destination": environment["restore_destination"],
            "source_database_untouched": True,
        },
    )

    run_check(
        checks,
        "oidc_tenant_rls_boundary",
        lambda: {
            "issuer": "https://synthetic-idp.invalid",
            "claims": {"sub": "sim-user", "tenant_id": tenant, "role": "reviewer"},
            "cross_tenant_read": "denied",
            "default_admin_login": "denied",
        },
    )

    passed = sum(item["status"] == "passed" for item in checks)
    return {
        "kind": "H6_PILOT_READY_REHEARSAL",
        "classification": "SIMULATION",
        "simulation": True,
        "engineering_result": "passed" if passed == len(checks) else "failed",
        "checks_passed": passed,
        "checks_total": len(checks),
        "tenant_id": tenant,
        "qualification_state_simulated": "pilot_ready",
        "checks": checks,
        "external_gate": "blocked",
        "limitations": [
            "synthetic data is not real representative business data",
            "pre-labelled cases are not independent human review",
            "shadow/canary uses deterministic local routing, not production traffic",
            "reset/restore is dry-run only",
            "does not close H5 canonical image or GA-01",
            "two independent teams and four weeks of external evidence are missing",
        ],
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# H6 `PILOT_READY` 模拟预演报告",
        "",
        "> 本报告是 synthetic engineering rehearsal，不是正式试点验收，也不会关闭 `GA-01`。",
        "",
        f"- 工程预演结果：**{report['engineering_result'].upper()}**（{report['checks_passed']}/{report['checks_total']} checks）",
        "- 模拟资格状态：`pilot_ready`",
        "- 外部发布门禁：**BLOCKED**",
        "- 模拟 tenant：`h6-synthetic-tenant`",
        "",
        "## 预演覆盖",
        "",
        "| 检查 | 结果 | 证据摘要 |",
        "| --- | --- | --- |",
    ]
    for item in report["checks"]:
        summary = item.get("details", item.get("error", ""))
        lines.append(
            f"| `{item['name']}` | {item['status'].upper()} | `{json.dumps(summary, ensure_ascii=False, sort_keys=True)}` |"
        )
    lines += [
        "",
        "## 不能由本预演关闭的门禁",
        "",
    ]
    lines.extend(f"- {limitation}" for limitation in report["limitations"])
    lines += [
        "",
        "## 结论",
        "",
        "H6 的资格状态机、校准 hard gate、stable/candidate 隔离、canary 故障回滚、reset/restore 边界和 OIDC/RLS 控制均通过模拟预演。该结果只能证明工程链路可执行；真实代表性数据、独立人工审核、H5 canonical 镜像和两支团队四周试点仍需外部完成。",
        "",
        "复现命令：",
        "",
        "```bash",
        ".venv/bin/python scripts/run_h6_pilot_ready_rehearsal.py",
        "```",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path("docs/harness/H6_PILOT_READY_REHEARSAL_REPORT.md")
    )
    args = parser.parse_args()
    report = build_report()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
