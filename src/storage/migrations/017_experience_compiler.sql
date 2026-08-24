ALTER TABLE training_snapshots
    ADD COLUMN algorithm TEXT,
    ADD COLUMN compile_manifest_key TEXT,
    ADD COLUMN compile_manifest_sha256 TEXT CHECK (
        compile_manifest_sha256 IS NULL OR compile_manifest_sha256 ~ '^[0-9a-f]{64}$'
    ),
    ADD COLUMN target_tokenizer_digest TEXT CHECK (
        target_tokenizer_digest IS NULL OR target_tokenizer_digest ~ '^[0-9a-f]{64}$'
    ),
    ADD COLUMN chat_template_digest TEXT CHECK (
        chat_template_digest IS NULL OR chat_template_digest ~ '^[0-9a-f]{64}$'
    ),
    ADD CONSTRAINT training_snapshot_compile_fields CHECK (
        (algorithm IS NULL AND compile_manifest_key IS NULL AND compile_manifest_sha256 IS NULL
            AND target_tokenizer_digest IS NULL AND chat_template_digest IS NULL)
        OR
        (algorithm = 'sft' AND compile_manifest_key IS NOT NULL AND compile_manifest_sha256 IS NOT NULL
            AND target_tokenizer_digest IS NOT NULL AND chat_template_digest IS NOT NULL)
    );
