"""Approve public synthetic gap Experiences with a recorded DeepSeek judge."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from openai import OpenAI

from core.evidence import S3EvidenceStore, canonical_bytes, sha256
from core.verifiers import ReadOnlyServices
from harness.evaluation import EvaluationService, validate_suite_manifest, validate_trial_transcript
from harness.experience import _put_immutable, publish_trial_experience
from harness.product_loop import parse_document
from utils.cloud_audit import observable_model_call, record_cloud_call
from utils.proxy import get_openai_client_kwargs
from utils.s3_utils import S3Utils


def candidate_tasks(
    report: dict[str, Any], target_digests: set[str], outcome_state: str = "failed"
) -> list[dict[str, Any]]:
    """Return tasks with the requested verified outcome for a selected target."""
    if outcome_state not in {"failed", "succeeded"}:
        raise ValueError("deepseek_review_outcome_state_invalid")
    return [
        task
        for task in report["tasks"]
        if any(
            outcome["target_fingerprint_sha256"] in target_digests
            and outcome["state"] == outcome_state
            for outcome in task["outcomes"]
        )
    ]


def accepted_judgments(
    cases: list[dict[str, Any]],
    pass_a: dict[str, Any],
    pass_b: dict[str, Any],
    *,
    allow_partial: bool = False,
) -> dict[str, dict[str, Any]]:
    """Fail closed unless both judge passes approve every evidence-grounded case."""
    first = {item.get("case_id"): item for item in pass_a.get("cases", [])}
    second = {item.get("case_id"): item for item in pass_b.get("cases", [])}
    accepted = {}
    for case in cases:
        case_id = case["case_id"]
        a, b = first.get(case_id, {}), second.get(case_id, {})
        required = case["required_substrings"][0]
        if (
            a.get("answerable") is not True
            or b.get("decision") != "approved"
            or float(a.get("confidence", 0)) < 0.95
            or float(b.get("confidence", 0)) < 0.95
            or required.casefold() not in str(a.get("expected_response", "")).casefold()
            or required.casefold() not in str(b.get("expected_response", "")).casefold()
        ):
            if allow_partial:
                continue
            raise ValueError(f"deepseek_judgment_not_approved:{case_id}")
        accepted[case_id] = {"pass_a": a, "pass_b": b}
    return accepted


def _call(
    client: OpenAI, model: str, prompt: str, component: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    messages = [{"role": "user", "content": prompt}]
    config = {"temperature": 0, "max_tokens": 16384, "response_format": {"type": "json_object"}}
    record_cloud_call(component, model, ["public_multidoc2dial_fixture"])
    started = time.perf_counter()
    response = client.chat.completions.create(model=model, messages=messages, **config)
    content = response.choices[0].message.content or ""
    call = observable_model_call(
        component=component,
        model=model,
        messages=messages,
        response=content,
        generation_config=config,
        latency_ms=(time.perf_counter() - started) * 1000,
        status="succeeded",
        revision_or_digest=response.model,
        usage=response.usage.model_dump() if response.usage else None,
        provider_request_id=response.id,
    )
    return json.loads(content), call


def _prompt_a(cases: list[dict[str, Any]]) -> str:
    return (
        "You are an evidence-grounded dataset auditor. The input is an Apache-2.0 public "
        "MultiDoc2Dial synthetic replay fixture. For every case, decide answerability from "
        "the supplied PDF page text only. Copy the required evidence phrase verbatim inside "
        'expected_response. Return JSON only as {"cases":[{"case_id":str,'
        '"answerable":bool,"expected_response":str,"confidence":number,"reason":str}]}.\n\n'
        + json.dumps(cases, ensure_ascii=False)
    )


def _prompt_b(cases: list[dict[str, Any]], pass_a: dict[str, Any]) -> str:
    return (
        "Independently verify this public synthetic PDF QA audit. Compare both small-model "
        "answers with the source page and the first-pass proposal. Approve only when the "
        "source directly supports the gold evidence phrase. Copy that phrase verbatim inside "
        'expected_response. Return JSON only as {"cases":[{"case_id":str,'
        '"decision":"approved"|"rejected","expected_response":str,'
        '"confidence":number,"reason":str}]}.\n\n'
        + json.dumps({"cases": cases, "first_pass": pass_a}, ensure_ascii=False)
    )


def _read(services: ReadOnlyServices, ref: str, expected: str) -> bytes:
    body = services.object_body(ref)
    if body is None or sha256(body) != expected:
        raise ValueError(f"deepseek_review_object_hash_mismatch:{ref}")
    return body


def main() -> None:  # noqa: C901 - one auditable, fail-closed batch operation
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gap-report-ref", required=True)
    parser.add_argument("--gap-report-sha256", required=True)
    parser.add_argument("--target-fingerprint-sha256", action="append", required=True)
    parser.add_argument("--suite", action="append", type=Path, required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--model", default="deepseek-v4-pro")
    parser.add_argument("--permission-version", default="deepseek-v4-judge-v1")
    parser.add_argument("--judge-call-ref", action="append", default=[])
    parser.add_argument("--outcome-state", choices=("failed", "succeeded"), default="failed")
    parser.add_argument("--case-id-file", type=Path)
    parser.add_argument("--public-synthetic", action="store_true")
    args = parser.parse_args()
    if not args.database_url:
        raise ValueError("deepseek_review_database_url_missing")
    if not args.public_synthetic:
        raise ValueError("deepseek_review_public_synthetic_required")
    if not os.getenv("DEEPSEEK_API_KEY"):
        raise ValueError("deepseek_review_api_key_missing")

    labeler = {"tenant_id": args.tenant_id, "username": "deepseek-v4-labeler", "role": "admin"}
    reviewer = {"tenant_id": args.tenant_id, "username": "deepseek-v4-reviewer", "role": "reviewer"}
    services = ReadOnlyServices(args.database_url, labeler)
    evaluations = EvaluationService(args.database_url)
    s3 = S3Utils()
    store = S3EvidenceStore(s3.bucket, s3.client)
    report = json.loads(_read(services, args.gap_report_ref, args.gap_report_sha256))
    target_digests = set(args.target_fingerprint_sha256)
    report_targets = {item["fingerprint_sha256"] for item in report["targets"]}
    if not target_digests <= report_targets:
        raise ValueError("deepseek_review_target_not_in_gap_report")

    suites = [validate_suite_manifest(json.loads(path.read_text())) for path in args.suite]
    suite_cases = {case["case_id"]: case for suite in suites for case in suite["cases"]}
    pages = {
        case["case_id"]: parse_document(
            Path(suite["source"]["path"]).read_bytes(), Path(suite["source"]["path"]).name
        )[case["required_pages"][0] - 1]["text"]
        for suite in suites
        for case in suite["cases"]
    }
    selected_case_ids = (
        set(json.loads(args.case_id_file.read_text())) if args.case_id_file else None
    )
    tasks = [
        task
        for task in candidate_tasks(report, target_digests, args.outcome_state)
        if task["case_id"] in suite_cases
        and (selected_case_ids is None or task["case_id"] in selected_case_ids)
    ]
    audit_cases = []
    transcripts: dict[str, dict[str, Any]] = {}
    for task in tasks:
        case = suite_cases[task["case_id"]]
        answers = []
        for outcome in task["outcomes"]:
            transcript = validate_trial_transcript(
                json.loads(_read(services, outcome["transcript_ref"], outcome["transcript_sha256"]))
            )
            transcripts[outcome["trial_id"]] = transcript
            answers.append(
                {
                    "target_fingerprint_sha256": outcome["target_fingerprint_sha256"],
                    "state": outcome["state"],
                    "answer": transcript["answer"],
                    "verifier": transcript["verifier"],
                }
            )
        audit_cases.append(
            {
                "case_id": case["case_id"],
                "query": case["query"],
                "source_page": pages[case["case_id"]],
                "required_evidence": case["required_substrings"][0],
                "model_answers": answers,
            }
        )

    calls = []
    if args.judge_call_ref:
        if len(args.judge_call_ref) != 2:
            raise ValueError("deepseek_review_requires_two_judge_calls")
        calls = [json.loads(_read(services, ref, Path(ref).stem)) for ref in args.judge_call_ref]
        if [call.get("component") for call in calls] != [
            "gap_review.pass_a",
            "gap_review.pass_b",
        ] or any(call.get("status") != "succeeded" for call in calls):
            raise ValueError("deepseek_review_judge_call_invalid")
        pass_a, pass_b = [json.loads(call["response"]["content"]) for call in calls]
    else:
        client = OpenAI(
            api_key=os.environ["DEEPSEEK_API_KEY"],
            base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            timeout=180,
            **get_openai_client_kwargs(),
        )
        pass_a, call_a = _call(client, args.model, _prompt_a(audit_cases), "gap_review.pass_a")
        pass_b, call_b = _call(
            client, args.model, _prompt_b(audit_cases, pass_a), "gap_review.pass_b"
        )
        calls = [call_a, call_b]
    judgments = accepted_judgments(
        [{**suite_cases[task["case_id"]]} for task in tasks],
        pass_a,
        pass_b,
        allow_partial=True,
    )
    if not judgments:
        raise ValueError("deepseek_judgment_none_approved")
    rejected_case_ids = sorted(
        task["case_id"] for task in tasks if task["case_id"] not in judgments
    )
    tasks = [task for task in tasks if task["case_id"] in judgments]
    call_refs = []
    for call in calls:
        body = canonical_bytes(call)
        ref = f"tenants/{args.tenant_id}/annotations/deepseek/calls/sha256/{sha256(body)}.json"
        _put_immutable(store, ref, body)
        call_refs.append({"ref": ref, "sha256": sha256(body)})

    approved: dict[str, list[dict[str, str]]] = {digest: [] for digest in target_digests}
    for task in tasks:
        case = suite_cases[task["case_id"]]
        for outcome in task["outcomes"]:
            target = outcome["target_fingerprint_sha256"]
            if target not in target_digests or outcome["state"] != args.outcome_state:
                continue
            trial = services.trial(outcome["trial_id"])
            manifest = services.run_manifest(str(trial["run_id"])) if trial else None
            if trial is None or manifest is None or manifest["state"] != "published":
                raise ValueError(f"deepseek_review_trial_unpublished:{outcome['trial_id']}")
            descriptor = publish_trial_experience(
                store,
                tenant_id=args.tenant_id,
                trial=trial,
                transcript=transcripts[outcome["trial_id"]],
                source_manifest_ref=manifest["object_key"],
                source_manifest_sha256=manifest["manifest_sha256"],
            )
            label = {
                "decision": "approved",
                "task_bundle_id": task["task_bundle_id"],
                "run_id": str(trial["run_id"]),
                "trial_id": outcome["trial_id"],
                "split": case["split"],
                "experience_ref": descriptor["experience_ref"],
                "experience_sha256": descriptor["experience_sha256"],
                "expected_response": case["required_substrings"][0],
                "provenance": {
                    "reviewer_type": "llm_judge",
                    "model": args.model,
                    "permission_version": args.permission_version,
                    "judge_calls": call_refs,
                    "judgment": judgments[case["case_id"]],
                },
            }
            body = canonical_bytes(label)
            content_key = (
                f"tenants/{args.tenant_id}/annotations/deepseek/labels/sha256/{sha256(body)}.json"
            )
            _put_immutable(store, content_key, body)
            annotation_id = evaluations.create_annotation(
                labeler,
                run_id=str(trial["run_id"]),
                trial_id=outcome["trial_id"],
                kind="verifier_label",
                label=label,
                content_key=content_key,
                content_sha256=sha256(body),
            )
            evaluations.review_annotation(
                reviewer,
                annotation_id,
                status="approved",
                training_allowed=True,
                training_purpose="synthetic_pdf_agent_gap_test",
                permission_version=args.permission_version,
                reason="Automated public synthetic fixture approval; not human calibration",
            )
            approved[target].append(
                {"annotation_id": annotation_id, **descriptor, "case_id": case["case_id"]}
            )

    result = {
        "schema_version": "deepseek_gap_review.v1",
        "judge_model": args.model,
        "human_reviewed": False,
        "public_synthetic": True,
        "gap_report_ref": args.gap_report_ref,
        "gap_report_sha256": args.gap_report_sha256,
        "judge_calls": call_refs,
        "rejected_case_ids": rejected_case_ids,
        "approved": approved,
    }
    body = canonical_bytes(result)
    result_ref = f"tenants/{args.tenant_id}/annotations/deepseek/reviews/sha256/{sha256(body)}.json"
    _put_immutable(store, result_ref, body)
    print(json.dumps({"review_ref": result_ref, "review_sha256": sha256(body), **result}))


if __name__ == "__main__":
    main()
