CREATE TABLE pilot_programs (
    pilot_id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    team_id TEXT NOT NULL,
    qualification_id UUID NOT NULL REFERENCES qualification_records(qualification_id),
    stable_release_id UUID NOT NULL REFERENCES release_records(release_id),
    candidate_release_id UUID NOT NULL REFERENCES release_records(release_id),
    policy_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    owner TEXT NOT NULL,
    security_contact TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('draft','ready','active','suspended','completed','signed')),
    planned_start DATE,
    planned_end DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, team_id)
);

CREATE TABLE pilot_evidence_records (
    evidence_id UUID PRIMARY KEY,
    pilot_id UUID NOT NULL REFERENCES pilot_programs(pilot_id) ON DELETE CASCADE,
    tenant_id TEXT NOT NULL,
    team_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('weekly_audit','incident','exception','team_signoff')),
    week_no INTEGER CHECK (week_no IS NULL OR week_no BETWEEN 1 AND 4),
    artifact_key TEXT NOT NULL,
    artifact_sha256 TEXT NOT NULL CHECK (artifact_sha256 ~ '^[0-9a-f]{64}$'),
    reviewer TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('passed','failed','open')),
    run_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX pilot_evidence_week_idx ON pilot_evidence_records (pilot_id, kind, week_no);
ALTER TABLE pilot_programs ENABLE ROW LEVEL SECURITY;
ALTER TABLE pilot_programs FORCE ROW LEVEL SECURITY;
ALTER TABLE pilot_evidence_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE pilot_evidence_records FORCE ROW LEVEL SECURITY;
CREATE POLICY pilot_programs_tenant_policy ON pilot_programs USING (tenant_id = current_setting('app.tenant_id', true)) WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
CREATE POLICY pilot_evidence_tenant_policy ON pilot_evidence_records USING (tenant_id = current_setting('app.tenant_id', true)) WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
GRANT SELECT, INSERT, UPDATE, DELETE ON pilot_programs, pilot_evidence_records TO dataalchemy_app;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dataalchemy_verifier') THEN
        GRANT SELECT ON pilot_programs, pilot_evidence_records TO dataalchemy_verifier;
    END IF;
END $$;
