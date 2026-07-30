CREATE TABLE connector_cursors (
    connector_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    cursor_value TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE connector_runs (
    run_id UUID PRIMARY KEY,
    connector_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('running', 'succeeded', 'failed')),
    cursor_before TEXT,
    cursor_after TEXT,
    error_summary TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);

ALTER TABLE connector_cursors ENABLE ROW LEVEL SECURITY;
ALTER TABLE connector_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE connector_cursors FORCE ROW LEVEL SECURITY;
ALTER TABLE connector_runs FORCE ROW LEVEL SECURITY;

CREATE POLICY connector_cursors_tenant_policy ON connector_cursors
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
CREATE POLICY connector_runs_tenant_policy ON connector_runs
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

GRANT SELECT, INSERT, UPDATE, DELETE ON connector_cursors, connector_runs TO dataalchemy_app;
