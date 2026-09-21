DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dataalchemy_verifier') THEN
        GRANT SELECT ON document_acl TO dataalchemy_verifier;
    END IF;
END $$;
