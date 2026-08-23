"""Small deterministic evaluator used by the controlled model-evaluate Job."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from core.verifiers import ReadOnlyServices, VerificationResult, default_verifiers
from harness.jobs import validate_evaluation_context


def _prediction(value: Any, *, prompt: str, latency_ms: float) -> dict[str, Any]:
    if isinstance(value, dict):
        return {
            "answer": str(value.get("answer", "")),
            "status": value.get("status"),
            "citations": value.get("citations", []),
            "evidence_refs": value.get("evidence_refs", []),
            "prompt": str(value.get("prompt", prompt)),
            "latency_ms": float(value.get("latency_ms", latency_ms)),
        }
    return {
        "answer": str(value),
        "status": "generated",
        "citations": [],
        "evidence_refs": [],
        "prompt": prompt,
        "latency_ms": latency_ms,
    }


def _digest(value: Any) -> str:
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(body).hexdigest()


def _assertions(criteria: dict[str, Any], prediction: dict[str, Any]) -> list[dict[str, Any]]:
    answer = prediction["answer"]
    assertions = [
        {
            "name": "required_substring",
            "kind": "configuration_smoke",
            "value": str(value),
            "passed": str(value).lower() in answer.lower(),
        }
        for value in criteria.get("required_substrings", [])
    ]
    if "expected_status" in criteria:
        assertions.append(
            {
                "name": "expected_status",
                "kind": "configuration_smoke",
                "value": criteria["expected_status"],
                "passed": prediction["status"] == criteria["expected_status"],
            }
        )
    if "expected_answer" in criteria:
        assertions.append(
            {
                "name": "expected_answer",
                "kind": "configuration_smoke",
                "passed": answer == criteria["expected_answer"],
            }
        )
    if "expected_citation_count" in criteria:
        assertions.append(
            {
                "name": "expected_citation_count",
                "kind": "configuration_smoke",
                "value": criteria["expected_citation_count"],
                "passed": len(prediction["citations"]) == criteria["expected_citation_count"],
            }
        )
    return assertions


def run_evaluation(context: dict[str, Any]) -> dict[str, Any]:
    """Run fixed response assertions; model invocation is supplied by the worker context.

    A serialized context may select only an allowlisted local model and adapter path; it
    cannot provide executable code. The worker creates the predictor after validation.
    """
    validate_evaluation_context(context)
    predictor = context.get("predict")
    if not callable(predictor):
        from config import get_model_config
        from inference.model_manager import ModelManager

        model_id = context.get("model_id") or get_model_config("model_c").get("model_path")
        adapter_path = context.get("adapter_path") if context.get("use_adapter") else None
        model = ModelManager()
        # A fixed evaluator must not silently select a promoted adapter or compile a
        # different graph for base and candidate runs.
        model.load_models(model_id, lora_adapter_path=adapter_path, compile_model=False)

        def predictor(query: str) -> str:
            prompt = f"### Instruction:\n{query}\n\n### Response:\n"
            answer = model.generate(
                [prompt], {"max_new_tokens": context.get("max_new_tokens", 64), "do_sample": False}
            )[0]
            # ModelManager returns the prompt plus generated text.  TinyLlama can
            # echo the instruction template; use the first response block rather
            # than the final echoed marker, which may contain no answer.
            generated = answer.split("### Response:", 1)[-1]
            return generated.split("### Instruction:", 1)[0].strip()

    passed = 0
    invalidated = 0
    cases: list[dict[str, Any]] = []
    verifier_cases = {case["case_id"]: case["criteria"] for case in context["verifier_cases"]}
    generation_policy = context.get(
        "generation_policy",
        {"max_new_tokens": context.get("max_new_tokens", 64), "do_sample": False},
    )
    generation_policy_sha256 = _digest(generation_policy)
    if context.get("generation_policy_sha256") not in {None, generation_policy_sha256}:
        raise ValueError("h5_generation_policy_hash_mismatch")
    model_fingerprint = context.get("model_fingerprint")
    verifier = context.get("verify")
    if not callable(verifier):
        spec = default_verifiers().get("verify_rag_outcome", 1)
        services = ReadOnlyServices(
            context["database_url"],
            {
                "tenant_id": context["tenant_id"],
                "username": context["username"],
                "role": context["role"],
            },
        )

        def verifier(criteria: dict[str, Any], output: dict[str, Any]) -> VerificationResult:
            return spec.handler(
                {"parameters": criteria},
                {"tenant_id": context["tenant_id"]},
                {"output": output},
                services,
            )

    for case in context["cases"]:
        prompt = f"### Instruction:\n{case['query']}\n\n### Response:\n"
        started = time.perf_counter()
        prediction = _prediction(
            predictor(case["query"]),
            prompt=prompt,
            latency_ms=(time.perf_counter() - started) * 1000,
        )
        criteria = verifier_cases[case["case_id"]]
        verification = verifier(criteria, prediction)
        case_passed = verification.status == "passed"
        invalidated += int(verification.status == "blocked")
        cases.append(
            {
                "case_id": case["case_id"],
                "passed": case_passed,
                **prediction,
                "assertions": _assertions(criteria, prediction),
                "verification": {
                    "name": "verify_rag_outcome",
                    "version": 1,
                    "status": verification.status,
                    "error_code": verification.error_code,
                    "summary": verification.summary,
                },
                "model_fingerprint": model_fingerprint,
                "generation_policy": generation_policy,
                "generation_policy_sha256": generation_policy_sha256,
            }
        )
        passed += int(case_passed)
    total = len(cases)
    return {
        "output": {"evaluation_id": context["evaluation_id"], "cases": cases},
        "metrics": {
            "passed": passed,
            "total": total,
            "pass_rate": passed / total if total else 0.0,
        },
        "hard_gates": {
            "passed": passed == total,
            "invalidated_trials": invalidated,
            "independent_verifier": True,
            "judge_only": False,
        },
        "observed_scope": [f"evaluation:{context['evaluation_id']}"],
    }
