-- Request receipts bind transport retries to the existing task runtime.
-- No duplicate task state is maintained here.
CREATE TABLE chat_requests (
    request_key TEXT PRIMARY KEY CHECK (request_key ~ '^[0-9a-f]{64}$'),
    request_id UUID NOT NULL,
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    session_id UUID NOT NULL,
    task_id UUID NOT NULL UNIQUE,
    run_id UUID NOT NULL UNIQUE,
    context_ref TEXT,
    context_sha256 TEXT CHECK (context_sha256 ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((context_ref IS NULL) = (context_sha256 IS NULL))
);

ALTER TABLE chat_requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE chat_requests FORCE ROW LEVEL SECURITY;
CREATE POLICY chat_requests_owner_policy ON chat_requests
    USING (tenant_id = current_setting('app.tenant_id', true)
        AND owner_id = current_setting('app.user_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)
        AND owner_id = current_setting('app.user_id', true));
REVOKE ALL ON chat_requests FROM dataalchemy_app;
GRANT SELECT, INSERT ON chat_requests TO dataalchemy_app;
GRANT UPDATE (context_ref, context_sha256) ON chat_requests TO dataalchemy_app;
