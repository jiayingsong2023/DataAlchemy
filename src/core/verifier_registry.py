"""Compose the default verifier registry."""

from .verifier_contracts import VerifierRegistry, VerifierSpec
from .verifier_evaluation import (
    _compile_decision,
    _compile_manifest,
    _experience_bundle,
    _gap_report,
    _model_migration,
    _release,
    _release_decision,
    _trajectory,
    _trial_transcript,
)
from .verifier_memory import (
    _chat_capture,
    _context_checkpoint,
    _context_snapshot,
    _ingest,
    _ingest_v2,
    _input_manifest,
    _memory,
    _memory_distillation,
    _memory_policy,
    _retrieval,
    _retrieval_v2,
)
from .verifier_task import _environment, _rag_outcome, _task_bundle, _task_run
from .verifier_training import (
    _adapter,
    _base_evaluation,
    _conflict_decision,
    _conflict_report,
    _deployment_binding,
    _dpo_gate,
    _evaluation,
    _qualification,
    _qualification_manifest,
    _refined_corpus,
    _release_v2,
    _rl_gate,
    _rough_clean,
    _rough_clean_v2,
    _shadow,
    _training_input,
    _training_snapshot,
)


def default_verifiers() -> VerifierRegistry:
    registry = VerifierRegistry()
    registry.register(VerifierSpec("verify_task_bundle", 1, _task_bundle))
    registry.register(VerifierSpec("verify_environment", 1, _environment))
    registry.register(VerifierSpec("verify_task_run", 1, _task_run))
    registry.register(VerifierSpec("verify_rag_outcome", 1, _rag_outcome))
    registry.register(VerifierSpec("verify_trial_transcript", 1, _trial_transcript))
    registry.register(VerifierSpec("verify_gap_report", 1, _gap_report))
    registry.register(VerifierSpec("verify_release_decision", 1, _release_decision))
    registry.register(VerifierSpec("verify_experience_bundle", 1, _experience_bundle))
    registry.register(VerifierSpec("verify_compile_manifest", 1, _compile_manifest))
    registry.register(VerifierSpec("verify_compile_decision", 1, _compile_decision))
    registry.register(VerifierSpec("verify_model_migration", 1, _model_migration))
    registry.register(VerifierSpec("verify_dpo_gate", 1, _dpo_gate))
    registry.register(VerifierSpec("verify_rl_gate", 1, _rl_gate))
    registry.register(VerifierSpec("verify_ingest", 1, _ingest))
    registry.register(VerifierSpec("verify_ingest", 2, _ingest_v2))
    registry.register(VerifierSpec("verify_retrieval", 1, _retrieval))
    registry.register(VerifierSpec("verify_retrieval", 2, _retrieval_v2))
    registry.register(VerifierSpec("verify_memory", 1, _memory))
    registry.register(VerifierSpec("verify_context_snapshot", 1, _context_snapshot))
    registry.register(VerifierSpec("verify_chat_capture", 1, _chat_capture))
    registry.register(VerifierSpec("verify_context_checkpoint", 1, _context_checkpoint))
    registry.register(VerifierSpec("verify_memory_distillation", 1, _memory_distillation))
    registry.register(VerifierSpec("verify_memory_policy", 1, _memory_policy))
    registry.register(VerifierSpec("verify_release", 1, _release))
    registry.register(VerifierSpec("verify_trajectory", 1, _trajectory))
    registry.register(VerifierSpec("verify_training_snapshot", 1, _training_snapshot))
    registry.register(VerifierSpec("verify_base_evaluation", 1, _base_evaluation))
    registry.register(VerifierSpec("verify_training_input", 1, _training_input))
    registry.register(VerifierSpec("verify_adapter", 1, _adapter))
    registry.register(VerifierSpec("verify_evaluation", 1, _evaluation))
    registry.register(VerifierSpec("verify_release", 2, _release_v2))
    registry.register(VerifierSpec("verify_qualification", 1, _qualification))
    registry.register(VerifierSpec("verify_qualification_manifest", 1, _qualification_manifest))
    registry.register(VerifierSpec("verify_deployment_binding", 1, _deployment_binding))
    registry.register(VerifierSpec("verify_shadow", 1, _shadow))
    registry.register(VerifierSpec("verify_rough_clean", 1, _rough_clean))
    registry.register(VerifierSpec("verify_rough_clean", 2, _rough_clean_v2))
    registry.register(VerifierSpec("verify_input_manifest", 1, _input_manifest))
    registry.register(VerifierSpec("verify_refined_corpus", 1, _refined_corpus))
    registry.register(VerifierSpec("verify_conflict_report", 1, _conflict_report))
    registry.register(VerifierSpec("verify_conflict_decision", 1, _conflict_decision))
    return registry
