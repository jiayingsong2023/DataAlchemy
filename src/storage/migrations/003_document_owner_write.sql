CREATE POLICY documents_owner_write ON documents
    FOR UPDATE
    USING (
        tenant_id = current_setting('app.tenant_id', true)
        AND (
            owner_id = current_setting('app.user_id', true)
            OR current_setting('app.role', true) = 'admin'
        )
    )
    WITH CHECK (
        tenant_id = current_setting('app.tenant_id', true)
        AND (
            owner_id = current_setting('app.user_id', true)
            OR current_setting('app.role', true) = 'admin'
        )
    );
