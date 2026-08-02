-- H4 ACL hardening: write checks must be as strict as read checks.

ALTER POLICY conversation_events_tenant_policy ON conversation_events
    WITH CHECK (
        tenant_id = current_setting('app.tenant_id', true)
        AND EXISTS (
            SELECT 1 FROM conversation_sessions s
            WHERE s.session_id = conversation_events.session_id
              AND s.tenant_id = conversation_events.tenant_id
              AND (s.owner_id = current_setting('app.user_id', true)
                   OR current_setting('app.role', true) = 'admin')
        )
    );

ALTER POLICY context_snapshots_tenant_policy ON context_snapshots
    WITH CHECK (
        tenant_id = current_setting('app.tenant_id', true)
        AND EXISTS (
            SELECT 1 FROM conversation_sessions s
            WHERE s.session_id = context_snapshots.session_id
              AND s.tenant_id = context_snapshots.tenant_id
              AND (s.owner_id = current_setting('app.user_id', true)
                   OR current_setting('app.role', true) = 'admin')
        )
    );

ALTER POLICY context_checkpoints_tenant_policy ON context_checkpoints
    WITH CHECK (
        tenant_id = current_setting('app.tenant_id', true)
        AND EXISTS (
            SELECT 1 FROM conversation_sessions s
            WHERE s.session_id = context_checkpoints.session_id
              AND s.tenant_id = context_checkpoints.tenant_id
              AND (s.owner_id = current_setting('app.user_id', true)
                   OR current_setting('app.role', true) = 'admin')
        )
    );

ALTER POLICY memory_sources_tenant_policy ON memory_sources
    USING (
        tenant_id = current_setting('app.tenant_id', true)
        AND EXISTS (
            SELECT 1 FROM memories m
            WHERE m.memory_id = memory_sources.memory_id
              AND m.tenant_id = memory_sources.tenant_id
              AND (m.owner_id = current_setting('app.user_id', true)
                   OR current_setting('app.role', true) = 'admin')
        )
    )
    WITH CHECK (
        tenant_id = current_setting('app.tenant_id', true)
        AND EXISTS (
            SELECT 1 FROM memories m
            WHERE m.memory_id = memory_sources.memory_id
              AND m.tenant_id = memory_sources.tenant_id
              AND (m.owner_id = current_setting('app.user_id', true)
                   OR current_setting('app.role', true) = 'admin')
        )
        AND (
            conversation_event_id IS NULL
            OR EXISTS (
                SELECT 1 FROM conversation_events e
                WHERE e.event_id = memory_sources.conversation_event_id
                  AND e.tenant_id = memory_sources.tenant_id
            )
        )
    );
