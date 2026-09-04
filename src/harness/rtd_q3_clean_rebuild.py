"""Fail-closed contract for the RTD-Q3 clean-rebuild receipt."""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

_HASH = re.compile(r"^[0-9a-f]{64}$")
_DESCRIPTORS = (
    "revocation",
    "old_release",
    "clean_snapshot",
    "replacement_adapter",
    "replacement_evaluation",
    "replacement_release",
    "q2_receipt",
)
_STATES = {
    "revocation": "PASS",
    "old_release": "rolled_back",
    "clean_snapshot": "approved",
    "replacement_adapter": "verified",
    "replacement_evaluation": "passed",
    "replacement_release": "promoted",
    "q2_receipt": "PASS",
}


def _descriptor(value: Any, name: str) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"ref", "sha256", "state"}:
        raise ValueError(f"rtd_q3_{name}_descriptor_invalid")
    if (
        not isinstance(value["ref"], str)
        or not value["ref"]
        or not isinstance(value["sha256"], str)
        or not _HASH.fullmatch(value["sha256"])
        or value["state"] != _STATES[name]
    ):
        raise ValueError(f"rtd_q3_{name}_descriptor_invalid")
    return {"ref": value["ref"], "sha256": value["sha256"], "state": value["state"]}


def validate_clean_rebuild_receipt(receipt: dict[str, Any]) -> dict[str, Any]:
    """Validate the immutable evidence chain required to close RTD-Q3."""
    required = {
        "schema_version",
        "decision",
        "tenant_id",
        "build_git_sha",
        "revocation",
        "old_release",
        "clean_snapshot",
        "replacement_adapter",
        "replacement_evaluation",
        "replacement_release",
        "q2_receipt",
        "lineage",
        "rollback",
        "limitations",
    }
    if not isinstance(receipt, dict) or set(receipt) != required:
        raise ValueError("rtd_q3_receipt_fields_invalid")
    if (
        receipt["schema_version"] != "rtd_q3_clean_rebuild.v1"
        or receipt["decision"] != "PASS"
        or not isinstance(receipt["tenant_id"], str)
        or not receipt["tenant_id"].startswith("rtd-q3-")
        or not isinstance(receipt["build_git_sha"], str)
        or not re.fullmatch(r"[0-9a-f]{40}", receipt["build_git_sha"])
    ):
        raise ValueError("rtd_q3_receipt_identity_invalid")
    descriptors = {name: _descriptor(receipt[name], name) for name in _DESCRIPTORS}
    lineage = receipt["lineage"]
    if not isinstance(lineage, dict) or set(lineage) != {"revoked", "included", "excluded"}:
        raise ValueError("rtd_q3_lineage_invalid")
    for values in lineage.values():
        if not isinstance(values, list) or not values or any(
            not isinstance(item, str) or not item for item in values
        ):
            raise ValueError("rtd_q3_lineage_invalid")
    if set(lineage["revoked"]) & set(lineage["included"]):
        raise ValueError("rtd_q3_revoked_lineage_included")
    if receipt["rollback"] != {
        "old_release_status": "rolled_back",
        "new_release_repromoted": True,
    }:
        raise ValueError("rtd_q3_rollback_incomplete")
    if not isinstance(receipt["limitations"], list) or any(
        not isinstance(item, str) or not item for item in receipt["limitations"]
    ):
        raise ValueError("rtd_q3_limitations_invalid")
    return {**deepcopy(receipt), **descriptors}
