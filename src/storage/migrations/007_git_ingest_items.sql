CREATE TABLE connector_ingest_items (
    item_id UUID PRIMARY KEY,
    run_id UUID NOT NULL REFERENCES connector_runs(run_id) ON DELETE CASCADE,
    connector_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    source_uri TEXT NOT NULL,
    source_version TEXT NOT NULL,
    raw_object_key TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('landed', 'indexed', 'rejected')),
    rejection_reason TEXT,
    document_id UUID REFERENCES documents(document_id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, connector_id, source_uri, source_version)
);

CREATE INDEX connector_ingest_items_run_idx ON connector_ingest_items (run_id, state);
ALTER TABLE connector_ingest_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE connector_ingest_items FORCE ROW LEVEL SECURITY;
CREATE POLICY connector_ingest_items_tenant_policy ON connector_ingest_items
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
GRANT SELECT, INSERT, UPDATE, DELETE ON connector_ingest_items TO dataalchemy_app;
