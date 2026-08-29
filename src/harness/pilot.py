"""H6 pilot admission and external-evidence accounting."""

from __future__ import annotations

import json
import re
import uuid
from typing import Any

from storage.audit import AuditLog
from storage.postgres import PostgresDatabase


class PilotService:
    def __init__(self, database_url: str):
        self.database = PostgresDatabase(database_url)
        self.audit = AuditLog(database_url)

    def create(
        self,
        identity: dict[str, str],
        *,
        team_id: str,
        qualification_id: str,
        stable_release_id: str,
        candidate_release_id: str,
        owner: str,
        security_contact: str,
        policy: dict[str, Any] | None = None,
    ) -> str:
        if (
            identity.get("role") not in {"admin", "reviewer"}
            or not team_id
            or not owner
            or not security_contact
        ):
            raise PermissionError("pilot_admission_requires_reviewer")
        pilot_id = str(uuid.uuid4())
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT state FROM qualification_records WHERE qualification_id = %s",
                    (qualification_id,),
                )
                row = cursor.fetchone()
                if row is None or row["state"] != "pilot_ready":
                    raise ValueError("pilot_qualification_not_ready")
                cursor.execute(
                    "INSERT INTO pilot_programs (pilot_id, tenant_id, team_id, qualification_id, stable_release_id, candidate_release_id, policy_json, owner, security_contact, state) VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,'draft')",
                    (
                        pilot_id,
                        identity["tenant_id"],
                        team_id,
                        qualification_id,
                        stable_release_id,
                        candidate_release_id,
                        json.dumps(policy or {}, ensure_ascii=False),
                        owner,
                        security_contact,
                    ),
                )
        self.audit.record(
            identity, "pilot.created", "pilot", resource_id=pilot_id, metadata={"team_id": team_id}
        )
        return pilot_id

    def record_evidence(
        self,
        identity: dict[str, str],
        pilot_id: str,
        *,
        kind: str,
        artifact_key: str,
        artifact_sha256: str,
        reviewer: str,
        outcome: str,
        week_no: int | None = None,
        run_refs: list[str] | None = None,
    ) -> str:
        if (
            identity.get("role") not in {"admin", "reviewer"}
            or reviewer != identity.get("username")
            or not re.fullmatch(r"[0-9a-f]{64}", artifact_sha256)
        ):
            raise ValueError("pilot_evidence_invalid")
        if kind == "weekly_audit" and week_no not in {1, 2, 3, 4}:
            raise ValueError("pilot_week_invalid")
        evidence_id = str(uuid.uuid4())
        with self.database.transaction(identity) as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT team_id FROM pilot_programs WHERE pilot_id = %s", (pilot_id,)
                )
                if cursor.fetchone() is None:
                    raise ValueError("pilot_not_found")
                cursor.execute(
                    "INSERT INTO pilot_evidence_records (evidence_id,pilot_id,tenant_id,team_id,kind,week_no,artifact_key,artifact_sha256,reviewer,outcome,run_refs) SELECT %s,pilot_id,tenant_id,team_id,%s,%s,%s,%s,%s,%s,%s::jsonb FROM pilot_programs WHERE pilot_id=%s",
                    (
                        evidence_id,
                        kind,
                        week_no,
                        artifact_key,
                        artifact_sha256,
                        reviewer,
                        outcome,
                        json.dumps(run_refs or []),
                        pilot_id,
                    ),
                )
        self.audit.record(
            identity,
            "pilot.evidence_recorded",
            "pilot_evidence",
            resource_id=evidence_id,
            metadata={"pilot_id": pilot_id, "kind": kind},
        )
        return evidence_id

    def status(self, identity: dict[str, str], pilot_id: str) -> dict[str, Any]:
        with self.database.transaction(identity, read_only=True) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT * FROM pilot_programs WHERE pilot_id = %s", (pilot_id,))
                pilot = cursor.fetchone()
                if pilot is None:
                    raise ValueError("pilot_not_found")
                cursor.execute(
                    "SELECT kind, week_no, outcome FROM pilot_evidence_records WHERE pilot_id = %s",
                    (pilot_id,),
                )
                evidence = cursor.fetchall()
                cursor.execute(
                    "SELECT pilot_id, team_id FROM pilot_programs WHERE qualification_id = %s",
                    (pilot["qualification_id"],),
                )
                pilots = cursor.fetchall()
        weeks = {
            row["week_no"]
            for row in evidence
            if row["kind"] == "weekly_audit" and row["outcome"] == "passed"
        }
        signed = any(
            row["kind"] == "team_signoff" and row["outcome"] == "passed" for row in evidence
        )
        return {
            **pilot,
            "weeks_passed": len(weeks),
            "team_count": len({row["team_id"] for row in pilots}),
            "ga_eligible": len(pilots) == 2 and len(weeks) == 4 and signed,
            "evidence_count": len(evidence),
        }
