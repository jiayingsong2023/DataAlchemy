CREATE TABLE run_manifests (
    run_id UUID PRIMARY KEY REFERENCES agent_tasks(run_id) ON DELETE CASCADE,
    task_id UUID NOT NULL UNIQUE REFERENCES agent_tasks(task_id) ON DELETE CASCADE,
    tenant_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'requested', 'staged', 'verified', 'published', 'publish_blocked',
        'corrupt', 'deleting', 'deleted'
    )),
    schema_version INTEGER NOT NULL DEFAULT 1 CHECK (schema_version > 0),
    staging_key TEXT,
    object_key TEXT,
    manifest_sha256 TEXT CHECK (manifest_sha256 IS NULL OR manifest_sha256 ~ '^[0-9a-f]{64}$'),
    manifest_size BIGINT CHECK (manifest_size IS NULL OR manifest_size >= 0),
    fingerprint_digest TEXT CHECK (fingerprint_digest IS NULL OR fingerprint_digest ~ '^[0-9a-f]{64}$'),
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    final_outcome TEXT,
    last_error_code TEXT,
    retention_until TIMESTAMPTZ,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    staged_at TIMESTAMPTZ,
    verified_at TIMESTAMPTZ,
    published_at TIMESTAMPTZ,
    deleted_at TIMESTAMPTZ
);

CREATE TABLE harness_outbox (
    outbox_id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    run_id UUID NOT NULL REFERENCES agent_tasks(run_id) ON DELETE CASCADE,
    task_id UUID NOT NULL REFERENCES agent_tasks(task_id) ON DELETE CASCADE,
    step_id UUID,
    job_id UUID,
    kind TEXT NOT NULL CHECK (kind IN (
        'publish_manifest', 'delete_manifest', 'submit_job', 'cancel_job', 'capture_job_logs'
    )),
    dedupe_key TEXT NOT NULL UNIQUE,
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'processing', 'retry', 'completed', 'dead')),
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    lease_owner TEXT,
    lease_expires_at TIMESTAMPTZ,
    last_error_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX harness_outbox_claim_idx
    ON harness_outbox (state, available_at, created_at)
    WHERE state IN ('pending', 'retry');
CREATE INDEX harness_outbox_run_idx ON harness_outbox (run_id, task_id, created_at);

CREATE TABLE agent_jobs (
    job_id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    run_id UUID NOT NULL REFERENCES agent_tasks(run_id) ON DELETE CASCADE,
    task_id UUID NOT NULL REFERENCES agent_tasks(task_id) ON DELETE CASCADE,
    step_id UUID NOT NULL,
    kind TEXT NOT NULL CHECK (kind = 'spark_rough_clean'),
    backend TEXT NOT NULL CHECK (backend = 'kubernetes'),
    state TEXT NOT NULL CHECK (state IN (
        'requested', 'submitting', 'running', 'succeeded', 'failed', 'cancel_requested',
        'cancelled', 'orphaned', 'reconciliation_required'
    )),
    external_name TEXT NOT NULL,
    external_uid TEXT,
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    input_key TEXT NOT NULL,
    input_sha256 TEXT NOT NULL CHECK (input_sha256 ~ '^[0-9a-f]{64}$'),
    result_key TEXT,
    result_sha256 TEXT CHECK (result_sha256 IS NULL OR result_sha256 ~ '^[0-9a-f]{64}$'),
    log_key TEXT,
    log_sha256 TEXT CHECK (log_sha256 IS NULL OR log_sha256 ~ '^[0-9a-f]{64}$'),
    last_observed_at TIMESTAMPTZ,
    deadline_at TIMESTAMPTZ NOT NULL,
    error_code TEXT,
    requested_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    submitted_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    UNIQUE (task_id, step_id)
);

CREATE INDEX agent_jobs_run_idx ON agent_jobs (run_id, state, requested_at);

ALTER TABLE harness_outbox
    ADD CONSTRAINT harness_outbox_job_fk
    FOREIGN KEY (job_id) REFERENCES agent_jobs(job_id) ON DELETE CASCADE;

CREATE OR REPLACE FUNCTION prevent_published_manifest_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.state = 'published' AND (
        NEW.object_key IS DISTINCT FROM OLD.object_key
        OR NEW.manifest_sha256 IS DISTINCT FROM OLD.manifest_sha256
        OR NEW.manifest_size IS DISTINCT FROM OLD.manifest_size
        OR NEW.fingerprint_digest IS DISTINCT FROM OLD.fingerprint_digest
    ) THEN
        RAISE EXCEPTION 'published manifest is immutable';
    END IF;
    IF OLD.state = 'deleted' AND NEW.state <> 'deleted' THEN
        RAISE EXCEPTION 'deleted manifest is a tombstone';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER run_manifests_immutable
BEFORE UPDATE ON run_manifests
FOR EACH ROW EXECUTE FUNCTION prevent_published_manifest_mutation();

ALTER TABLE run_manifests ENABLE ROW LEVEL SECURITY;
ALTER TABLE harness_outbox ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE run_manifests FORCE ROW LEVEL SECURITY;
ALTER TABLE harness_outbox FORCE ROW LEVEL SECURITY;
ALTER TABLE agent_jobs FORCE ROW LEVEL SECURITY;

CREATE POLICY run_manifests_tenant_policy ON run_manifests
    USING (
        tenant_id = current_setting('app.tenant_id', true)
        AND EXISTS (SELECT 1 FROM agent_tasks t WHERE t.task_id = run_manifests.task_id)
    )
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

CREATE POLICY harness_outbox_tenant_policy ON harness_outbox
    USING (
        tenant_id = current_setting('app.tenant_id', true)
        AND EXISTS (SELECT 1 FROM agent_tasks t WHERE t.task_id = harness_outbox.task_id)
    )
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

CREATE POLICY agent_jobs_tenant_policy ON agent_jobs
    USING (
        tenant_id = current_setting('app.tenant_id', true)
        AND EXISTS (SELECT 1 FROM agent_tasks t WHERE t.task_id = agent_jobs.task_id)
    )
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

GRANT SELECT, INSERT, UPDATE, DELETE ON run_manifests, harness_outbox, agent_jobs TO dataalchemy_app;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dataalchemy_verifier') THEN
        GRANT SELECT ON agent_tasks, agent_jobs TO dataalchemy_verifier;
    END IF;
END $$;
