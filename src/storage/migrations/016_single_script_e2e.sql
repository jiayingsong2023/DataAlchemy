CREATE TABLE h5_attempts (
    attempt_id UUID PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES agent_tasks(run_id) ON DELETE CASCADE,
    tenant_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'candidate', 'running', 'waiting_input', 'waiting_approval', 'passed',
        'failed', 'rolled_back', 'cancelled'
    )),
    config_json JSONB NOT NULL,
    config_sha256 TEXT NOT NULL CHECK (config_sha256 ~ '^[0-9a-f]{64}$'),
    active BOOLEAN NOT NULL DEFAULT true,
    lease_owner TEXT,
    lease_expires_at TIMESTAMPTZ,
    last_gate TEXT,
    last_error_code TEXT,
    snapshot_id UUID REFERENCES training_snapshots(snapshot_id),
    base_evaluation_id UUID REFERENCES evaluation_campaigns(evaluation_id),
    candidate_evaluation_id UUID REFERENCES evaluation_campaigns(evaluation_id),
    adapter_id UUID REFERENCES adapter_manifests(adapter_id),
    release_id UUID REFERENCES release_records(release_id),
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX h5_attempts_one_active_per_run
    ON h5_attempts (tenant_id, run_id) WHERE active;
CREATE INDEX h5_attempts_lease_idx
    ON h5_attempts (lease_expires_at) WHERE active AND lease_owner IS NOT NULL;

CREATE TABLE run_gate_events (
    gate_event_id UUID PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES agent_tasks(run_id) ON DELETE CASCADE,
    tenant_id TEXT NOT NULL,
    attempt_id UUID REFERENCES h5_attempts(attempt_id) ON DELETE CASCADE,
    gate_name TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'pending', 'running', 'waiting_input', 'waiting_approval', 'passed',
        'failed', 'rolled_back'
    )),
    input_artifact_id TEXT,
    input_sha256 TEXT CHECK (input_sha256 IS NULL OR input_sha256 ~ '^[0-9a-f]{64}$'),
    output_artifact_id TEXT,
    output_sha256 TEXT CHECK (output_sha256 IS NULL OR output_sha256 ~ '^[0-9a-f]{64}$'),
    evidence_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX run_gate_events_lookup_idx
    ON run_gate_events (tenant_id, run_id, gate_name, occurred_at DESC);

CREATE UNIQUE INDEX trajectory_annotations_feedback_source_unique
    ON trajectory_annotations (tenant_id, run_id, kind, content_key)
    WHERE kind = 'user_feedback' AND content_key IS NOT NULL;

ALTER TABLE h5_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE h5_attempts FORCE ROW LEVEL SECURITY;
ALTER TABLE run_gate_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE run_gate_events FORCE ROW LEVEL SECURITY;

CREATE POLICY h5_attempts_tenant_policy ON h5_attempts
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
CREATE POLICY run_gate_events_tenant_policy ON run_gate_events
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

GRANT SELECT, INSERT, UPDATE, DELETE ON h5_attempts, run_gate_events TO dataalchemy_app;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dataalchemy_verifier') THEN
        GRANT SELECT ON h5_attempts, run_gate_events TO dataalchemy_verifier;
    END IF;
END $$;
