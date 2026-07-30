DROP POLICY documents_tenant_policy ON documents;

CREATE POLICY documents_tenant_policy ON documents
    USING (
        tenant_id = current_setting('app.tenant_id', true)
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
