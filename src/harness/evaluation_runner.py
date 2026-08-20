"""Small deterministic evaluator used by the controlled model-evaluate Job."""

from __future__ import annotations

from typing import Any

from harness.jobs import validate_evaluation_context


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
    cases: list[dict[str, Any]] = []
    for case in context["cases"]:
        answer = predictor(case["query"])
        answer_text = str(answer)
        required = case.get("required_substrings", [])
        case_passed = all(str(value).lower() in answer_text.lower() for value in required)
        cases.append({"case_id": case["case_id"], "passed": case_passed})
        passed += int(case_passed)
    total = len(cases)
    return {
        "output": {"evaluation_id": context["evaluation_id"], "cases": cases},
        "metrics": {"passed": passed, "total": total, "pass_rate": passed / total if total else 0.0},
        "hard_gates": {"passed": passed == total, "invalidated_trials": 0},
        "observed_scope": [f"evaluation:{context['evaluation_id']}"],
    }
