CREATE TABLE audit_events (
    audit_id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT,
    outcome TEXT NOT NULL CHECK (outcome IN ('allowed', 'denied', 'failed')),
    correlation_id TEXT,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX audit_events_tenant_time_idx ON audit_events (tenant_id, occurred_at DESC);
CREATE INDEX audit_events_correlation_idx ON audit_events (correlation_id);

CREATE TABLE memory_policy_events (
    policy_event_id UUID PRIMARY KEY,
    memory_id UUID NOT NULL REFERENCES memories(memory_id),
    tenant_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('expired', 'conflict', 'reverted')),
    before_json JSONB NOT NULL,
    after_json JSONB NOT NULL,
    actor TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE release_records (
    release_id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('candidate', 'shadow', 'canary', 'promoted', 'rolled_back', 'rejected')),
    manifest_json JSONB NOT NULL,
    approved_by TEXT,
    rollback_release_id UUID REFERENCES release_records(release_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_policy_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE release_records ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_events FORCE ROW LEVEL SECURITY;
ALTER TABLE memory_policy_events FORCE ROW LEVEL SECURITY;
ALTER TABLE release_records FORCE ROW LEVEL SECURITY;

CREATE POLICY audit_events_tenant_policy ON audit_events
    USING (tenant_id = current_setting('app.tenant_id', true)
        AND current_setting('app.role', true) = 'admin')
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
CREATE POLICY memory_policy_events_tenant_policy ON memory_policy_events
    USING (tenant_id = current_setting('app.tenant_id', true)
        AND current_setting('app.role', true) = 'admin')
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
CREATE POLICY release_records_tenant_policy ON release_records
    USING (tenant_id = current_setting('app.tenant_id', true)
        AND current_setting('app.role', true) = 'admin')
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

GRANT SELECT, INSERT, UPDATE, DELETE ON audit_events, memory_policy_events, release_records TO dataalchemy_app;
