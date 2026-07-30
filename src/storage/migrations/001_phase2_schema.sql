CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE documents (
    document_id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    owner_id TEXT,
    source_uri TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    status TEXT NOT NULL CHECK (status IN ('building', 'ready', 'failed', 'deleted')),
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ,
    UNIQUE (tenant_id, source_uri, version),
    UNIQUE (tenant_id, source_uri, content_hash),
    UNIQUE (document_id, tenant_id)
);

CREATE TABLE document_acl (
    document_id UUID NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    tenant_id TEXT NOT NULL,
    subject_type TEXT NOT NULL CHECK (subject_type IN ('tenant', 'role', 'user')),
    subject_id TEXT NOT NULL,
    permission TEXT NOT NULL CHECK (permission IN ('read', 'write', 'admin')),
    PRIMARY KEY (document_id, subject_type, subject_id),
    FOREIGN KEY (document_id, tenant_id) REFERENCES documents(document_id, tenant_id) ON DELETE CASCADE
);

CREATE TABLE document_chunks (
    chunk_id UUID PRIMARY KEY,
    document_id UUID NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    text TEXT NOT NULL,
    lexemes TEXT NOT NULL,
    fts TSVECTOR NOT NULL,
    embedding VECTOR(512) NOT NULL,
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id, ordinal)
);

CREATE INDEX documents_tenant_status_idx ON documents (tenant_id, status);
CREATE INDEX document_chunks_fts_idx ON document_chunks USING GIN (fts);
CREATE INDEX document_chunks_embedding_idx ON document_chunks USING hnsw (embedding vector_cosine_ops);

CREATE TABLE agent_tasks (
    task_id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    owner TEXT NOT NULL,
    role TEXT NOT NULL,
    goal TEXT NOT NULL,
    state TEXT NOT NULL,
    plan_json JSONB NOT NULL,
    current_step INTEGER NOT NULL DEFAULT 0,
    max_steps INTEGER NOT NULL CHECK (max_steps > 0),
    version INTEGER NOT NULL DEFAULT 1,
    budget_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    approval_json JSONB,
    finish_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE agent_events (
    event_id UUID PRIMARY KEY,
    task_id UUID NOT NULL REFERENCES agent_tasks(task_id) ON DELETE CASCADE,
    tenant_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json JSONB NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX agent_events_task_idx ON agent_events (task_id, occurred_at, event_id);

CREATE TABLE agent_tool_runs (
    tenant_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    result_json JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, tool_name, idempotency_key)
);

CREATE TABLE memories (
    memory_id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    owner_id TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('episodic', 'profile', 'procedural')),
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    embedding VECTOR(512),
    status TEXT NOT NULL CHECK (status IN ('candidate', 'approved', 'superseded', 'deleted')),
    source_event_id UUID NOT NULL REFERENCES agent_events(event_id),
    valid_until TIMESTAMPTZ,
    retention_until TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at TIMESTAMPTZ,
    UNIQUE (tenant_id, kind, content_hash),
    UNIQUE (memory_id, tenant_id)
);

CREATE INDEX memories_retrieval_idx ON memories (tenant_id, status, valid_until);
CREATE INDEX memories_embedding_idx ON memories USING hnsw (embedding vector_cosine_ops);

CREATE TABLE memory_acl (
    memory_id UUID NOT NULL REFERENCES memories(memory_id) ON DELETE CASCADE,
    tenant_id TEXT NOT NULL,
    subject_type TEXT NOT NULL CHECK (subject_type IN ('tenant', 'role', 'user')),
    subject_id TEXT NOT NULL,
    permission TEXT NOT NULL CHECK (permission IN ('read', 'write', 'admin')),
    PRIMARY KEY (memory_id, subject_type, subject_id),
    FOREIGN KEY (memory_id, tenant_id) REFERENCES memories(memory_id, tenant_id) ON DELETE CASCADE
);

CREATE TABLE memory_versions (
    memory_id UUID PRIMARY KEY REFERENCES memories(memory_id) ON DELETE CASCADE,
    supersedes_memory_id UUID REFERENCES memories(memory_id),
    decision_event_id UUID NOT NULL REFERENCES agent_events(event_id)
);

CREATE TABLE deletion_requests (
    request_id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    target_type TEXT NOT NULL CHECK (target_type IN ('document', 'memory')),
    target_id UUID NOT NULL,
    requested_by TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'completed', 'failed')),
    failure_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX deletion_requests_tenant_status_idx ON deletion_requests (tenant_id, status);

ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_acl ENABLE ROW LEVEL SECURITY;
ALTER TABLE document_chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_tool_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE memories ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_acl ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE deletion_requests ENABLE ROW LEVEL SECURITY;

ALTER TABLE documents FORCE ROW LEVEL SECURITY;
ALTER TABLE document_acl FORCE ROW LEVEL SECURITY;
ALTER TABLE document_chunks FORCE ROW LEVEL SECURITY;
ALTER TABLE agent_tasks FORCE ROW LEVEL SECURITY;
ALTER TABLE agent_events FORCE ROW LEVEL SECURITY;
ALTER TABLE agent_tool_runs FORCE ROW LEVEL SECURITY;
ALTER TABLE memories FORCE ROW LEVEL SECURITY;
ALTER TABLE memory_acl FORCE ROW LEVEL SECURITY;
ALTER TABLE memory_versions FORCE ROW LEVEL SECURITY;
ALTER TABLE deletion_requests FORCE ROW LEVEL SECURITY;

CREATE POLICY documents_tenant_policy ON documents
    USING (
        tenant_id = current_setting('app.tenant_id', true)
        AND status <> 'deleted'
        AND (
            owner_id = current_setting('app.user_id', true)
            OR current_setting('app.role', true) = 'admin'
            OR EXISTS (
                SELECT 1 FROM document_acl a
                WHERE a.document_id = documents.document_id
                    AND a.tenant_id = documents.tenant_id
                    AND a.permission IN ('read', 'write', 'admin')
                    AND (
                        (a.subject_type = 'tenant' AND a.subject_id = current_setting('app.tenant_id', true))
                        OR (a.subject_type = 'user' AND a.subject_id = current_setting('app.user_id', true))
                        OR (a.subject_type = 'role' AND a.subject_id = current_setting('app.role', true))
                    )
            )
        )
    )
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

CREATE POLICY document_acl_tenant_policy ON document_acl
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

CREATE POLICY document_chunks_tenant_policy ON document_chunks
    USING (EXISTS (SELECT 1 FROM documents d WHERE d.document_id = document_chunks.document_id))
    WITH CHECK (EXISTS (SELECT 1 FROM documents d WHERE d.document_id = document_chunks.document_id));

CREATE POLICY agent_tasks_tenant_policy ON agent_tasks
    USING (tenant_id = current_setting('app.tenant_id', true)
        AND (owner = current_setting('app.user_id', true) OR current_setting('app.role', true) = 'admin'))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

CREATE POLICY agent_events_tenant_policy ON agent_events
    USING (tenant_id = current_setting('app.tenant_id', true)
        AND EXISTS (SELECT 1 FROM agent_tasks t WHERE t.task_id = agent_events.task_id))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

CREATE POLICY agent_tool_runs_tenant_policy ON agent_tool_runs
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

CREATE POLICY memories_tenant_policy ON memories
    USING (
        tenant_id = current_setting('app.tenant_id', true)
        AND (
            owner_id = current_setting('app.user_id', true)
            OR current_setting('app.role', true) = 'admin'
            OR (
                status = 'approved'
                AND EXISTS (
                    SELECT 1 FROM memory_acl a
                    WHERE a.memory_id = memories.memory_id
                        AND a.tenant_id = memories.tenant_id
                        AND a.permission IN ('read', 'write', 'admin')
                        AND (
                            (a.subject_type = 'tenant' AND a.subject_id = current_setting('app.tenant_id', true))
                            OR (a.subject_type = 'user' AND a.subject_id = current_setting('app.user_id', true))
                            OR (a.subject_type = 'role' AND a.subject_id = current_setting('app.role', true))
                        )
                )
            )
        )
    )
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)
        AND (owner_id = current_setting('app.user_id', true)
            OR current_setting('app.role', true) = 'admin'));

CREATE POLICY memory_acl_tenant_policy ON memory_acl
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

CREATE POLICY memory_versions_tenant_policy ON memory_versions
    USING (EXISTS (SELECT 1 FROM memories m WHERE m.memory_id = memory_versions.memory_id))
    WITH CHECK (EXISTS (SELECT 1 FROM memories m WHERE m.memory_id = memory_versions.memory_id));

CREATE POLICY deletion_requests_tenant_policy ON deletion_requests
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
