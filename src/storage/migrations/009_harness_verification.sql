CREATE TABLE agent_step_verifications (
    verification_id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    task_id UUID NOT NULL REFERENCES agent_tasks(task_id) ON DELETE CASCADE,
    step_id UUID NOT NULL,
    criterion_id TEXT NOT NULL,
    verifier TEXT NOT NULL,
    verifier_version INTEGER NOT NULL CHECK (verifier_version > 0),
    verifier_contract_digest TEXT NOT NULL,
    attempt INTEGER NOT NULL CHECK (attempt > 0),
    status TEXT NOT NULL CHECK (status IN ('passed', 'failed', 'blocked', 'warning')),
    tool_result_digest TEXT NOT NULL,
    input_digest TEXT NOT NULL,
    error_code TEXT,
    summary_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (task_id, step_id, criterion_id, attempt)
);

CREATE INDEX agent_step_verifications_task_idx
    ON agent_step_verifications (task_id, step_id, completed_at, attempt);

ALTER TABLE agent_step_verifications ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_step_verifications FORCE ROW LEVEL SECURITY;

CREATE POLICY agent_step_verifications_tenant_policy ON agent_step_verifications
    USING (
        tenant_id = current_setting('app.tenant_id', true)
        AND EXISTS (SELECT 1 FROM agent_tasks t WHERE t.task_id = agent_step_verifications.task_id)
    )
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

GRANT SELECT, INSERT ON agent_step_verifications TO dataalchemy_app;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dataalchemy_verifier') THEN
        GRANT USAGE ON SCHEMA public TO dataalchemy_verifier;
        GRANT SELECT ON documents, document_chunks, memories, release_records TO dataalchemy_verifier;
    END IF;
END $$;
