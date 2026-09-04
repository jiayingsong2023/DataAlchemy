"""Re-run immutable PDF/RAG Task Bundles against exactly two local model targets."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.evidence import canonical_bytes
from core.verifiers import ReadOnlyServices, default_verifiers
from harness.evaluation import (
    EvaluationService,
    build_gap_report,
    model_fingerprint_digest,
    model_path_fingerprint,
    validate_suite_manifest,
)
from harness.evaluation_runner import run_evaluation
from harness.experience import validate_environment_receipt, validate_task_bundle
from harness.job_runner import finish_evaluation_trials
from inference.model_manager import ModelManager
from rag.retriever import Retriever
from rag.vector_store import VectorStore
from scripts.run_h5_pdf_cycle import build_runtime, capture_trial
from utils.s3_utils import S3Utils


def _sha256(body: bytes | dict[str, Any]) -> str:
    if isinstance(body, dict):
        body = canonical_bytes(body)
    return hashlib.sha256(body).hexdigest()


def _read_object(store: S3Utils, ref: str, expected_sha256: str) -> bytes:
    body = store.get_object_body(ref)
    if body is None or _sha256(body) != expected_sha256:
        raise ValueError(f"rerollout_object_hash_mismatch:{ref}")
    return body


def _target(config_json: str, model_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    config = json.loads(config_json)
    if not isinstance(config, dict) or config.get("enabled") is not True:
        raise ValueError("rerollout_target_not_enabled")
    model_path = Path(config.get("model_path", "")).resolve()
    if not model_path.is_relative_to(model_root.resolve()) or not model_path.is_dir():
        raise ValueError("rerollout_target_outside_model_root")
    adapter_value = config.get("adapter_path")
    adapter_path = Path(adapter_value).resolve() if adapter_value else None
    if adapter_path and (
        not adapter_path.is_relative_to(model_root.resolve()) or not adapter_path.is_dir()
    ):
        raise ValueError("rerollout_adapter_outside_model_root")
    adapter_id = config.get("adapter_id")
    if bool(adapter_path) != bool(adapter_id):
        raise ValueError("rerollout_adapter_identity_missing")
    config = {
        **config,
        "model_path": str(model_path),
        **({"adapter_path": str(adapter_path)} if adapter_path else {}),
    }
    fingerprint = model_path_fingerprint(
        model_path,
        model_root=model_root,
        adapter_path=adapter_path,
    )
    return config, fingerprint


def _assets(
    store: S3Utils, bundle_refs: list[str], receipt_map: dict[str, Any]
) -> list[dict[str, Any]]:
    if not isinstance(receipt_map, dict):
        raise ValueError("rerollout_receipt_map_invalid")
    assets = []
    for bundle_ref in bundle_refs:
        bundle_body = store.get_object_body(bundle_ref)
        if bundle_body is None:
            raise ValueError(f"rerollout_bundle_missing:{bundle_ref}")
        bundle = validate_task_bundle(json.loads(bundle_body))
        bundle_sha256 = _sha256(bundle_body)
        task_bundle_id = f"sha256:{bundle_sha256}"
        if bundle_ref != receipt_map.get(task_bundle_id, {}).get("bundle_ref", bundle_ref):
            raise ValueError("rerollout_bundle_ref_mismatch")
        model_input = json.loads(
            _read_object(store, bundle["task"]["input_ref"], bundle["task"]["input_sha256"])
        )
        verifier_contract = bundle["verifiers"][0]
        verifier_ref = receipt_map.get(task_bundle_id, {}).get("verifier_input_ref")
        if not verifier_ref:
            raise ValueError("rerollout_verifier_input_ref_missing")
        verifier_input = json.loads(
            _read_object(store, verifier_ref, verifier_contract["contract_sha256"])
        )
        receipt_descriptor = receipt_map.get(task_bundle_id, {}).get("receipt")
        if (
            not isinstance(receipt_descriptor, dict)
            or set(receipt_descriptor) != {"ref", "sha256"}
            or not isinstance(receipt_descriptor.get("ref"), str)
            or not isinstance(receipt_descriptor.get("sha256"), str)
        ):
            raise ValueError("rerollout_environment_receipt_missing")
        receipt_body = _read_object(
            store, receipt_descriptor.get("ref", ""), receipt_descriptor.get("sha256", "")
        )
        receipt = validate_environment_receipt(json.loads(receipt_body))
        if receipt["state"] != "ready" or receipt["task_bundle_id"] != task_bundle_id:
            raise ValueError("rerollout_environment_not_ready")
        assets.append(
            {
                "bundle": bundle,
                "fingerprint": {
                    "task_bundle_id": task_bundle_id,
                    "task_bundle_ref": bundle_ref,
                    "task_bundle_sha256": bundle_sha256,
                    "task_input_ref": bundle["task"]["input_ref"],
                    "task_input_sha256": bundle["task"]["input_sha256"],
                    "verifier_input_ref": verifier_ref,
                    "verifier_input_sha256": verifier_contract["contract_sha256"],
                },
                "split": bundle["task"]["split"],
                "model_input": model_input,
                "verifier_input": verifier_input,
                "receipt": receipt_descriptor,
                "receipt_value": receipt,
            }
        )
    return assets


def _predictor(
    model: ModelManager,
    contexts: dict[str, list[dict[str, Any]]] | None,
    identity: dict[str, str],
    policy: dict[str, Any],
    cached_inputs: dict[str, dict[str, Any]] | None = None,
):
    def predict(query: str) -> dict[str, Any]:
        if cached_inputs is not None:
            cached = cached_inputs[query]
            prompt, citations = cached["prompt"], cached["citations"]
            context = []
        else:
            assert contexts is not None
            context = contexts[query]
            citations = None
        evidence = "\n".join(
            f"[{index}] {item.get('text', '')}" for index, item in enumerate(context, 1)
        )
        if cached_inputs is None:
            prompt = (
                "Answer only from the evidence. Copy the relevant text after "
                '"Grounded evidence:" exactly; do not include headers or explanation. '
                "If evidence is insufficient, answer exactly: 现有文档没有说明这个问题。\n"
                f"Evidence:\n{evidence}\nQuestion: {query}\nAnswer:"
            )
        answer = model.generate(
            [prompt],
            {key: policy[key] for key in ("max_new_tokens", "do_sample", "temperature", "top_p")},
        )[0].strip()
        abstained = answer == "现有文档没有说明这个问题。"
        citations = (
            citations
            if citations is not None
            else (
                []
                if abstained
                else [
                    {
                        "tenant_id": identity["tenant_id"],
                        "document_id": item.get("document_id"),
                        "chunk_id": item.get("chunk_id"),
                        "source_uri": item.get("source"),
                        "source_sha256": str(
                            item.get("metadata", {}).get("source_version")
                            or item.get("document_version")
                            or ""
                        ).removeprefix("sha256:"),
                        "locator": item.get("metadata", {}).get("locator"),
                    }
                    for item in context
                    if item.get("context_type") == "document" and item.get("chunk_id")
                ]
            )
        )
        return {
            "prompt": prompt,
            "answer": answer,
            "status": "abstained" if abstained else "grounded",
            "citations": citations,
            "evidence_refs": [],
        }

    return predict


def _scope_filtered_input(query: str, item: dict[str, Any]) -> dict[str, Any]:
    """Keep the retrieved evidence line named by the source-provided document scope."""
    scope = query.splitlines()[0].removeprefix("Document scope: ").strip()
    prefix, separator, remainder = item["prompt"].partition("Evidence:\n")
    evidence, question_separator, question = remainder.partition("\nQuestion: ")
    if not scope or not separator or not question_separator:
        return item
    for line in evidence.splitlines():
        label, closing, text = line.partition("] ")
        if closing and text.startswith(scope):
            index = int(label.removeprefix("[")) - 1
            return {
                **item,
                "prompt": f"{prefix}{separator}[1] {text}{question_separator}{question}",
                "citations": [item["citations"][index]],
            }
    return item


def _rag_preflight(
    retriever: Retriever, assets: list[dict[str, Any]], identity: dict[str, str]
) -> dict[str, list[dict[str, Any]]]:
    contexts = {}
    for asset in assets:
        query = asset["model_input"]["query"]
        source_sha256 = asset["verifier_input"]["criteria"].get("source", {}).get("sha256")
        contexts[query] = [
            {**item, "context_type": "document"}
            for item in retriever.retrieve(
                query,
                identity,
                top_k=5,
                source_version=f"sha256:{source_sha256}" if source_sha256 else None,
            )
        ]
    for asset in assets:
        criteria = asset["verifier_input"]["criteria"]
        if criteria.get("expected_status") != "grounded":
            continue
        source_sha256 = criteria.get("source", {}).get("sha256")
        context = contexts[asset["model_input"]["query"]]
        if not any(
            item.get("context_type") == "document"
            and str(
                item.get("metadata", {}).get("source_version") or item.get("document_version") or ""
            ).removeprefix("sha256:")
            == source_sha256
            for item in context
        ):
            raise RuntimeError(
                f"rerollout_rag_fixture_unavailable:{asset['model_input']['case_id']}"
            )
    return contexts


def main() -> None:  # noqa: C901 - one auditable dual-target gate sequence
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-ref", action="append", default=[])
    parser.add_argument("--bundle-ref-file", type=Path)
    parser.add_argument("--receipt-map", help="JSON object keyed by Task Bundle ID")
    parser.add_argument("--receipt-map-file", type=Path)
    parser.add_argument(
        "--target-config", action="append", required=True, help="Enabled target JSON"
    )
    parser.add_argument("--model-root", type=Path, required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--username", default="tve4-runner")
    parser.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    parser.add_argument("--output-prefix", default="tve4/rerollout")
    parser.add_argument("--max-new-tokens", type=int, default=64)
    parser.add_argument("--context-cache-ref")
    parser.add_argument("--context-cache-sha256")
    parser.add_argument("--scope-filter-context", action="store_true")
    args = parser.parse_args()
    if len(args.target_config) != 2 or not args.database_url:
        raise ValueError("rerollout_requires_database_and_two_targets")

    store = S3Utils()
    store.ensure_bucket()
    if args.bundle_ref_file:
        args.bundle_ref.extend(json.loads(args.bundle_ref_file.read_text(encoding="utf-8")))
    if not args.bundle_ref or bool(args.receipt_map) == bool(args.receipt_map_file):
        raise ValueError("rerollout_assets_arguments_invalid")
    receipt_map = json.loads(
        args.receipt_map if args.receipt_map else args.receipt_map_file.read_text(encoding="utf-8")
    )
    assets = _assets(store, args.bundle_ref, receipt_map)
    identity = {"tenant_id": args.tenant_id, "username": args.username, "role": "admin"}
    if bool(args.context_cache_ref) != bool(args.context_cache_sha256):
        raise ValueError("rerollout_context_cache_descriptor_invalid")
    cached_inputs = None
    contexts = None
    prompt_template = "exact-grounded-evidence-v1"
    if args.context_cache_ref:
        cache = json.loads(_read_object(store, args.context_cache_ref, args.context_cache_sha256))
        if cache.get("schema_version") != "rag_context_cache.v1":
            raise ValueError("rerollout_context_cache_invalid")
        prompt_template = cache.get("prompt_template", prompt_template)
        if prompt_template not in {
            "exact-grounded-evidence-v1",
            "scope-first-extractive-v2",
            "scope-ranked-evidence-v3",
            "scope-filtered-evidence-v4",
        }:
            raise ValueError("rerollout_context_cache_invalid")
        by_case = cache.get("cases")
        if not isinstance(by_case, dict) or set(by_case) != {
            asset["model_input"]["case_id"] for asset in assets
        }:
            raise ValueError("rerollout_context_cache_invalid")
        cached_inputs = {}
        for asset in assets:
            case_id = asset["model_input"]["case_id"]
            query = asset["model_input"]["query"]
            item = by_case[case_id]
            if (
                not isinstance(item, dict)
                or set(item) != {"query", "prompt", "citations"}
                or item["query"] != query
                or not isinstance(item["prompt"], str)
                or not isinstance(item["citations"], list)
            ):
                raise ValueError("rerollout_context_cache_invalid")
            cached_inputs[query] = item
        if args.scope_filter_context:
            if prompt_template != "scope-ranked-evidence-v3":
                raise ValueError("rerollout_scope_filter_requires_ranked_cache")
            cached_inputs = {
                query: _scope_filtered_input(query, item) for query, item in cached_inputs.items()
            }
            prompt_template = "scope-filtered-evidence-v4"
    elif args.scope_filter_context:
        raise ValueError("rerollout_scope_filter_requires_context_cache")
    else:
        contexts = _rag_preflight(
            Retriever(VectorStore(database_url=args.database_url)), assets, identity
        )
    targets = [_target(value, args.model_root) for value in args.target_config]
    fingerprints = [item[1] for item in targets]
    if len({model_fingerprint_digest(item) for item in fingerprints}) != 2:
        raise ValueError("rerollout_targets_not_distinct")
    generation_policy = {
        "max_new_tokens": args.max_new_tokens,
        "do_sample": False,
        "temperature": 0.7,
        "top_p": 0.9,
        "retrieval": {
            "source_scoped": True,
            "recall_k": 100,
            "context_k": 5,
            "required_evidence_pages": True,
            **(
                {"context_cache_sha256": args.context_cache_sha256}
                if args.context_cache_sha256
                else {}
            ),
            **({"scope_filtered": True} if args.scope_filter_context else {}),
        },
        "prompt_template": prompt_template,
    }
    generation_policy_sha256 = _sha256(generation_policy)
    verifier_spec = default_verifiers().get("verify_rag_outcome", 1)
    suite = validate_suite_manifest(
        {
            "version": "tve4-rerollout-v1",
            "policy_version": "tve4-rag-v1",
            "cases": [
                {**asset["model_input"], **asset["verifier_input"]["criteria"]} for asset in assets
            ],
        }
    )
    runtime = build_runtime(args.database_url, store)
    service = EvaluationService(args.database_url)
    model = ModelManager()
    outcomes = []
    root_run_id = str(uuid.uuid4())

    for target_config, fingerprint in targets:
        target_digest = model_fingerprint_digest(fingerprint)
        evaluation_id = service.create_campaign(
            identity,
            suite,
            subject_type="adapter" if target_config.get("adapter_id") else "base",
            subject_ref=target_config.get("adapter_id", target_digest),
            required_trials=len(assets),
        )
        trial_ids = {}
        for number, asset in enumerate(assets, 1):
            case_id = asset["model_input"]["case_id"]
            task = asyncio.run(capture_trial(runtime, identity, root_run_id, case_id))
            fingerprint_value = {
                **asset["fingerprint"],
                "model_fingerprint_sha256": target_digest,
                "environment_receipt_sha256": asset["receipt"]["sha256"],
            }
            trial_ids[case_id] = service.register_trial(
                identity,
                evaluation_id,
                task,
                case_id=case_id,
                trial_no=number,
                fingerprint=fingerprint_value,
            )
        model.unload_models()
        model.load_models(
            target_config["model_path"],
            lora_adapter_path=target_config.get("adapter_path"),
            compile_model=False,
        )
        context = {
            "harness_version": 5,
            "run_id": root_run_id,
            **identity,
            "evaluation_id": evaluation_id,
            "suite_sha256": _sha256(suite),
            "database_url": args.database_url,
            "model_id": target_config["model_path"],
            "cases": [asset["model_input"] for asset in assets],
            "verifier_cases": [asset["verifier_input"] for asset in assets],
            "predict": _predictor(model, contexts, identity, generation_policy, cached_inputs),
            "model_fingerprint": fingerprint,
            "generation_policy": generation_policy,
            "generation_policy_sha256": generation_policy_sha256,
            "trial_ids": trial_ids,
            "task_fingerprints": {
                asset["model_input"]["case_id"]: asset["fingerprint"] for asset in assets
            },
            "environment_receipts": {
                asset["model_input"]["case_id"]: asset["receipt"] for asset in assets
            },
        }
        result = run_evaluation(context)
        finish_evaluation_trials(service, store, identity, context, result)
        campaign_state = service.complete_campaign(identity, evaluation_id, result)
        readonly = ReadOnlyServices(args.database_url, identity)
        for asset in assets:
            case_id = asset["model_input"]["case_id"]
            trial = readonly.trial(trial_ids[case_id])
            transcript_result = (
                default_verifiers()
                .get("verify_trial_transcript", 1)
                .handler(
                    {"parameters": {"trial_id": trial_ids[case_id]}},
                    {"tenant_id": args.tenant_id},
                    {"output": {}},
                    readonly,
                )
            )
            if transcript_result.status != "passed":
                raise RuntimeError(f"rerollout_transcript_verification_failed:{case_id}")
            outcomes.append(
                {
                    "task_bundle_id": asset["fingerprint"]["task_bundle_id"],
                    "case_id": case_id,
                    "split": asset["split"],
                    "target_fingerprint_sha256": target_digest,
                    "evaluation_id": evaluation_id,
                    "campaign_state": campaign_state,
                    "trial_id": trial_ids[case_id],
                    "state": trial["state"],
                    "environment_initial_state_sha256": asset["receipt_value"][
                        "initial_state_sha256"
                    ],
                    "transcript_ref": trial["transcript_key"],
                    "transcript_sha256": trial["transcript_sha256"],
                }
            )
        model.unload_models()

    report = build_gap_report(
        fingerprints,
        outcomes,
        generation_policy_sha256=generation_policy_sha256,
        verifier_contract_digest=verifier_spec.contract_digest,
    )
    report_body = canonical_bytes(report)
    report_sha256 = _sha256(report_body)
    report_ref = f"{args.output_prefix.rstrip('/')}/{report_sha256}.json"
    if not store.put_object(report_ref, report_body, "application/json"):
        raise RuntimeError("rerollout_gap_report_write_failed")
    verified = (
        default_verifiers()
        .get("verify_gap_report", 1)
        .handler(
            {
                "parameters": {
                    "report_ref": report_ref,
                    "report_sha256": report_sha256,
                    "generation_policy_sha256": generation_policy_sha256,
                    "verifier_contract_digest": verifier_spec.contract_digest,
                }
            },
            {"tenant_id": args.tenant_id},
            {"output": {}},
            ReadOnlyServices(args.database_url, identity),
        )
    )
    if verified.status != "passed" or report["metrics"]["invalid_tasks"]:
        raise RuntimeError(f"rerollout_gap_report_failed:{verified.error_code}")
    print(
        json.dumps(
            {"report_ref": report_ref, "report_sha256": report_sha256, **report["metrics"]},
            ensure_ascii=False,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
