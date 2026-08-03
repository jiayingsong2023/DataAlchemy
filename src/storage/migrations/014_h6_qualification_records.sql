CREATE TABLE qualification_records (
    qualification_id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    purpose TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('draft', 'data_approved', 'calibrated', 'pilot_ready', 'revoked')),
    data_owner TEXT NOT NULL,
    created_by TEXT NOT NULL,
    reviewer TEXT,
    source_manifest_key TEXT NOT NULL,
    source_manifest_sha256 TEXT NOT NULL CHECK (source_manifest_sha256 ~ '^[0-9a-f]{64}$'),
    source_acl_digest TEXT NOT NULL,
    permission_version TEXT NOT NULL,
    data_classification TEXT NOT NULL,
    retention_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    allowed_processing_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    suite_version TEXT NOT NULL,
    suite_sha256 TEXT NOT NULL CHECK (suite_sha256 ~ '^[0-9a-f]{64}$'),
    policy_version TEXT NOT NULL,
    base_evaluation_id UUID REFERENCES evaluation_campaigns(evaluation_id),
    candidate_evaluation_id UUID REFERENCES evaluation_campaigns(evaluation_id),
    calibration_report_key TEXT,
    calibration_report_sha256 TEXT CHECK (
        calibration_report_sha256 IS NULL OR calibration_report_sha256 ~ '^[0-9a-f]{64}$'
    ),
    stable_release_id UUID REFERENCES release_records(release_id),
    candidate_release_id UUID REFERENCES release_records(release_id),
    deployment_evidence_key TEXT,
    deployment_evidence_sha256 TEXT CHECK (
        deployment_evidence_sha256 IS NULL OR deployment_evidence_sha256 ~ '^[0-9a-f]{64}$'
    ),
    reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    data_approved_at TIMESTAMPTZ,
    calibrated_at TIMESTAMPTZ,
    pilot_ready_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ
);

CREATE INDEX qualification_records_tenant_state_idx
    ON qualification_records (tenant_id, state, created_at DESC);
CREATE INDEX qualification_records_evaluation_idx
    ON qualification_records (tenant_id, base_evaluation_id, candidate_evaluation_id);

ALTER TABLE training_snapshots
    ADD COLUMN qualification_id UUID REFERENCES qualification_records(qualification_id);
ALTER TABLE release_records
    ADD COLUMN qualification_id UUID REFERENCES qualification_records(qualification_id);

ALTER TABLE qualification_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE qualification_records FORCE ROW LEVEL SECURITY;

CREATE POLICY qualification_records_tenant_policy ON qualification_records
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

GRANT SELECT, INSERT, UPDATE, DELETE ON qualification_records TO dataalchemy_app;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dataalchemy_verifier') THEN
        GRANT SELECT ON qualification_records TO dataalchemy_verifier;
    END IF;
END $$;
