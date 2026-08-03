"""Small, deterministic H6 deployment binding and routing contract."""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DeploymentBinding:
    stable_release_id: str
    candidate_release_id: str
    stable_digest: str
    candidate_digest: str
    stable_service: str
    candidate_service: str
    mode: str = "shadow"
    canary_percent: int = 0
    salt_sha256: str = ""

    def __post_init__(self) -> None:
        if not self.stable_release_id or not self.candidate_release_id or self.stable_release_id == self.candidate_release_id:
            raise ValueError("deployment_release_pair_invalid")
        if not self.stable_service or not self.candidate_service or self.stable_service == self.candidate_service:
            raise ValueError("deployment_service_pair_invalid")
        if self.mode not in {"shadow", "canary"} or not 0 <= self.canary_percent <= 100:
            raise ValueError("deployment_routing_invalid")
        if len(self.stable_digest) != 64 or len(self.candidate_digest) != 64:
            raise ValueError("deployment_digest_invalid")
        if self.salt_sha256 and (len(self.salt_sha256) != 64 or any(c not in "0123456789abcdef" for c in self.salt_sha256)):
            raise ValueError("deployment_salt_invalid")

    @classmethod
    def from_manifest(cls, manifest: dict[str, Any]) -> "DeploymentBinding":
        value = manifest.get("deployment_binding")
        if not isinstance(value, dict):
            raise ValueError("deployment_binding_missing")
        return cls(**value)


def route_request(binding: DeploymentBinding, tenant_id: str, subject_id: str) -> str:
    """Return the runtime authority; shadow never changes the authority."""
    if binding.mode == "shadow" or binding.canary_percent == 0:
        return "stable"
    digest = hmac.new(
        binding.salt_sha256.encode("utf-8"), f"{tenant_id}:{subject_id}".encode("utf-8"), hashlib.sha256
    ).digest()
    return "candidate" if int.from_bytes(digest[:4], "big") % 100 < binding.canary_percent else "stable"


def validate_shadow_output(output: dict[str, Any]) -> None:
    if output.get("side_effects"):
        raise ValueError("shadow_side_effects_forbidden")
    if output.get("authority") not in {None, "stable"}:
        raise ValueError("shadow_authority_changed")
