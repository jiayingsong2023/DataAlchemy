import pytest

from src.harness.rtd_q3_clean_rebuild import validate_clean_rebuild_receipt


def _receipt():
    def descriptor(state, name):
        return {
            "ref": f"tenants/rtd-q3-test/{name}.json",
            "sha256": "a" * 64,
            "state": state,
        }

    return {
        "schema_version": "rtd_q3_clean_rebuild.v1",
        "decision": "PASS",
        "tenant_id": "rtd-q3-test",
        "build_git_sha": "b" * 40,
        "revocation": descriptor("PASS", "revocation"),
        "old_release": descriptor("rolled_back", "old-release"),
        "clean_snapshot": descriptor("approved", "snapshot"),
        "replacement_adapter": descriptor("verified", "adapter"),
        "replacement_evaluation": descriptor("passed", "evaluation"),
        "replacement_release": descriptor("promoted", "release"),
        "q2_receipt": descriptor("PASS", "q2"),
        "lineage": {
            "revoked": ["source:revoked"],
            "included": ["source:clean-train", "source:clean-validation"],
            "excluded": ["source:revoked"],
        },
        "rollback": {"old_release_status": "rolled_back", "new_release_repromoted": True},
        "limitations": ["synthetic_internal_qualification"],
    }


def test_clean_rebuild_receipt_contract_accepts_complete_chain():
    receipt = _receipt()
    assert validate_clean_rebuild_receipt(receipt) == receipt


@pytest.mark.parametrize(
    ("field", "value"),
    [("old_release", "promoted"), ("replacement_release", "canary")],
)
def test_clean_rebuild_receipt_rejects_incomplete_states(field, value):
    receipt = _receipt()
    receipt[field]["state"] = value
    with pytest.raises(ValueError, match="descriptor_invalid"):
        validate_clean_rebuild_receipt(receipt)


def test_clean_rebuild_receipt_rejects_revoked_lineage_included():
    receipt = _receipt()
    receipt["lineage"]["included"].append("source:revoked")
    with pytest.raises(ValueError, match="revoked_lineage_included"):
        validate_clean_rebuild_receipt(receipt)
