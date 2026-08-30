"""Compatibility wrapper for the retired Agent D name."""

from rag.answering import LOCAL_ABSTENTION, GroundedAnswering, local_evidence_answer

_LOCAL_ABSTENTION = LOCAL_ABSTENTION
_local_evidence_answer = local_evidence_answer


class AgentD(GroundedAnswering):
    """Deprecated name; production callers migrate to GroundedAnswering."""
