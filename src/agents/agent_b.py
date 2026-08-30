"""Compatibility wrapper for the retired Agent B name."""

from inference.adapter_runtime import AdapterRuntime, clean_model_response

_clean_model_response = clean_model_response


class AgentB(AdapterRuntime):
    """Deprecated name; production callers migrate to AdapterRuntime."""
