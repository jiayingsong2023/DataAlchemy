-- H4: durable conversations, bounded context and governed memory provenance.

CREATE TABLE conversation_sessions (
    session_id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT 'New Chat',
    state TEXT NOT NULL DEFAULT 'active'
        CHECK (state IN ('active', 'closed', 'deleted')),
    auto_memory_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    context_generation INTEGER NOT NULL DEFAULT 1 CHECK (context_generation > 0),
    version BIGINT NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    closed_at TIMESTAMPTZ,
    deleted_at TIMESTAMPTZ,
    UNIQUE (session_id, tenant_id)
);

CREATE INDEX conversation_sessions_owner_idx
    ON conversation_sessions (tenant_id, owner_id, updated_at DESC);

CREATE TABLE conversation_events (
    event_id UUID PRIMARY KEY,
    session_id UUID NOT NULL,
    tenant_id TEXT NOT NULL,
    sequence_no BIGINT NOT NULL CHECK (sequence_no > 0),
    generation INTEGER NOT NULL CHECK (generation > 0),
    event_type TEXT NOT NULL CHECK (event_type IN (
        'user_message', 'assistant_message', 'tool_observation', 'session_closed', 'context_reset'
    )),
    content_json JSONB NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    trust_label TEXT NOT NULL CHECK (trust_label IN (
        'trusted_user', 'trusted_system', 'verified_tool', 'untrusted_external', 'legacy_unverified'
    )),
    task_id UUID,
    run_id UUID,
    agent_event_id UUID REFERENCES agent_events(event_id),
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (session_id, sequence_no),
    FOREIGN KEY (session_id, tenant_id)
        REFERENCES conversation_sessions(session_id, tenant_id) ON DELETE RESTRICT
);

CREATE INDEX conversation_events_session_idx
    ON conversation_events (tenant_id, session_id, sequence_no);

CREATE TABLE context_snapshots (
    snapshot_id UUID PRIMARY KEY,
    session_id UUID NOT NULL,
    tenant_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (generation > 0),
    task_id UUID,
    run_id UUID,
    task_spec_sha256 TEXT,
    plan_version INTEGER,
    identity_digest TEXT NOT NULL CHECK (identity_digest ~ '^[0-9a-f]{64}$'),
    pack_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
    checkpoint_id UUID,
    recent_event_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    document_chunk_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    memory_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    budget_json JSONB NOT NULL,
    envelope_sha256 TEXT NOT NULL CHECK (envelope_sha256 ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    FOREIGN KEY (session_id, tenant_id)
        REFERENCES conversation_sessions(session_id, tenant_id) ON DELETE RESTRICT
);

CREATE INDEX context_snapshots_session_idx
    ON context_snapshots (tenant_id, session_id, created_at DESC);

CREATE TABLE context_checkpoints (
    checkpoint_id UUID PRIMARY KEY,
    session_id UUID NOT NULL,
    tenant_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (generation > 0),
    source_sequence_start BIGINT NOT NULL CHECK (source_sequence_start > 0),
    source_sequence_end BIGINT NOT NULL CHECK (source_sequence_end >= source_sequence_start),
    source_digest TEXT NOT NULL CHECK (source_digest ~ '^[0-9a-f]{64}$'),
    summary TEXT NOT NULL,
    handoff_json JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate'
        CHECK (status IN ('candidate', 'verified', 'active', 'invalidated', 'superseded')),
    verifier_name TEXT,
    verifier_version INTEGER,
    verifier_result JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    verified_at TIMESTAMPTZ,
    FOREIGN KEY (session_id, tenant_id)
        REFERENCES conversation_sessions(session_id, tenant_id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX context_checkpoints_active_idx
    ON context_checkpoints (session_id, generation) WHERE status = 'active';

ALTER TABLE memories
    ALTER COLUMN source_event_id DROP NOT NULL,
    ADD COLUMN scope_type TEXT NOT NULL DEFAULT 'personal'
        CHECK (scope_type IN ('personal', 'team', 'tenant')),
    ADD COLUMN scope_id TEXT,
    ADD COLUMN claim_key TEXT,
    ADD COLUMN confidence DOUBLE PRECISION CHECK (confidence IS NULL OR confidence >= 0 AND confidence <= 1),
    ADD COLUMN trust_label TEXT NOT NULL DEFAULT 'legacy_unverified'
        CHECK (trust_label IN ('trusted_user', 'trusted_system', 'verified_tool', 'untrusted_external', 'legacy_unverified')),
    ADD COLUMN sensitivity_label TEXT NOT NULL DEFAULT 'unknown',
    ADD COLUMN risk_class TEXT NOT NULL DEFAULT 'legacy'
        CHECK (risk_class IN ('low', 'shared', 'high', 'prohibited', 'legacy')),
    ADD COLUMN policy_version TEXT NOT NULL DEFAULT 'memory-policy.v1',
    ADD COLUMN decision_reason TEXT,
    ADD COLUMN decided_by TEXT,
    ADD COLUMN decided_at TIMESTAMPTZ,
    ADD COLUMN row_version BIGINT NOT NULL DEFAULT 1 CHECK (row_version > 0);

ALTER TABLE memories DROP CONSTRAINT IF EXISTS memories_status_check;
ALTER TABLE memories ADD CONSTRAINT memories_status_check
    CHECK (status IN ('candidate', 'approved', 'superseded', 'deleted', 'rejected', 'conflicted'));

UPDATE memories SET scope_id = owner_id WHERE scope_id IS NULL;

CREATE TABLE memory_sources (
    memory_id UUID NOT NULL REFERENCES memories(memory_id) ON DELETE CASCADE,
    tenant_id TEXT NOT NULL,
    conversation_event_id UUID REFERENCES conversation_events(event_id),
    agent_event_id UUID REFERENCES agent_events(event_id),
    source_type TEXT NOT NULL CHECK (source_type IN ('conversation', 'agent', 'artifact')),
    source_sha256 TEXT NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (memory_id, source_type, source_sha256),
    FOREIGN KEY (memory_id, tenant_id) REFERENCES memories(memory_id, tenant_id) ON DELETE CASCADE
);

CREATE INDEX memory_sources_conversation_idx
    ON memory_sources (tenant_id, conversation_event_id);

CREATE UNIQUE INDEX memories_active_claim_idx
    ON memories (tenant_id, scope_type, scope_id, kind, claim_key)
    WHERE status = 'approved' AND claim_key IS NOT NULL;

ALTER TABLE memory_policy_events DROP CONSTRAINT IF EXISTS memory_policy_events_action_check;
ALTER TABLE memory_policy_events ADD CONSTRAINT memory_policy_events_action_check
    CHECK (action IN (
        'candidate_created', 'auto_approved', 'approval_requested', 'approved', 'rejected',
        'conflict_detected', 'conflict_resolved', 'superseded', 'expired', 'source_revoked',
        'deleted', 'reverted'
    ));

CREATE OR REPLACE FUNCTION reject_conversation_event_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'conversation_events is append-only';
END $$;

CREATE TRIGGER conversation_events_append_only
BEFORE UPDATE OR DELETE ON conversation_events
FOR EACH ROW EXECUTE FUNCTION reject_conversation_event_mutation();

ALTER TABLE conversation_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE conversation_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE context_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE context_checkpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE memory_sources ENABLE ROW LEVEL SECURITY;

ALTER TABLE conversation_sessions FORCE ROW LEVEL SECURITY;
ALTER TABLE conversation_events FORCE ROW LEVEL SECURITY;
ALTER TABLE context_snapshots FORCE ROW LEVEL SECURITY;
ALTER TABLE context_checkpoints FORCE ROW LEVEL SECURITY;
ALTER TABLE memory_sources FORCE ROW LEVEL SECURITY;

CREATE POLICY conversation_sessions_tenant_policy ON conversation_sessions
    USING (
        tenant_id = current_setting('app.tenant_id', true)
        AND (owner_id = current_setting('app.user_id', true)
             OR current_setting('app.role', true) = 'admin')
    )
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true)
        AND owner_id = current_setting('app.user_id', true));

CREATE POLICY conversation_events_tenant_policy ON conversation_events
    USING (
        tenant_id = current_setting('app.tenant_id', true)
        AND EXISTS (
            SELECT 1 FROM conversation_sessions s
            WHERE s.session_id = conversation_events.session_id
              AND (s.owner_id = current_setting('app.user_id', true)
                   OR current_setting('app.role', true) = 'admin')
        )
    )
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

CREATE POLICY context_snapshots_tenant_policy ON context_snapshots
    USING (
        tenant_id = current_setting('app.tenant_id', true)
        AND EXISTS (
            SELECT 1 FROM conversation_sessions s
            WHERE s.session_id = context_snapshots.session_id
              AND (s.owner_id = current_setting('app.user_id', true)
                   OR current_setting('app.role', true) = 'admin')
        )
    )
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

CREATE POLICY context_checkpoints_tenant_policy ON context_checkpoints
    USING (
        tenant_id = current_setting('app.tenant_id', true)
        AND EXISTS (
            SELECT 1 FROM conversation_sessions s
            WHERE s.session_id = context_checkpoints.session_id
              AND (s.owner_id = current_setting('app.user_id', true)
                   OR current_setting('app.role', true) = 'admin')
        )
    )
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

CREATE POLICY memory_sources_tenant_policy ON memory_sources
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

GRANT SELECT, INSERT, UPDATE ON conversation_sessions TO dataalchemy_app;
GRANT SELECT, INSERT ON conversation_events, context_snapshots, context_checkpoints, memory_sources TO dataalchemy_app;
GRANT SELECT, UPDATE ON memories TO dataalchemy_app;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dataalchemy_verifier') THEN
        GRANT SELECT ON conversation_sessions, conversation_events, context_snapshots,
            context_checkpoints, memory_sources, memories, memory_acl, memory_policy_events
            TO dataalchemy_verifier;
    END IF;
END $$;
