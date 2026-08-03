CREATE TABLE evaluation_campaigns (
    evaluation_id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    created_by TEXT NOT NULL,
    subject_type TEXT NOT NULL CHECK (subject_type IN ('base', 'adapter', 'release')),
    subject_ref TEXT NOT NULL,
    suite_version TEXT NOT NULL,
    suite_sha256 TEXT NOT NULL CHECK (suite_sha256 ~ '^[0-9a-f]{64}$'),
    policy_version TEXT NOT NULL,
    required_trials INTEGER NOT NULL CHECK (required_trials > 0),
    state TEXT NOT NULL CHECK (state IN ('draft', 'running', 'blocked', 'passed', 'failed')),
    baseline_evaluation_id UUID REFERENCES evaluation_campaigns(evaluation_id),
    metrics_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    hard_gates_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    report_key TEXT,
    report_sha256 TEXT CHECK (report_sha256 IS NULL OR report_sha256 ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);

CREATE TABLE trajectory_trials (
    trial_id UUID PRIMARY KEY,
    evaluation_id UUID NOT NULL REFERENCES evaluation_campaigns(evaluation_id) ON DELETE CASCADE,
    run_id UUID NOT NULL UNIQUE REFERENCES agent_tasks(run_id) ON DELETE CASCADE,
    task_id UUID NOT NULL UNIQUE REFERENCES agent_tasks(task_id) ON DELETE CASCADE,
    tenant_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    trial_no INTEGER NOT NULL CHECK (trial_no > 0),
    state TEXT NOT NULL CHECK (state IN ('queued', 'running', 'succeeded', 'failed', 'invalidated', 'aborted')),
    fingerprint_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    transcript_key TEXT,
    transcript_sha256 TEXT CHECK (transcript_sha256 IS NULL OR transcript_sha256 ~ '^[0-9a-f]{64}$'),
    outcome_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    metrics_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    failure_code TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    UNIQUE (evaluation_id, case_id, trial_no)
);

CREATE INDEX trajectory_trials_evaluation_idx ON trajectory_trials (evaluation_id, case_id, state);
CREATE INDEX trajectory_trials_tenant_idx ON trajectory_trials (tenant_id, created_at DESC);

CREATE TABLE trajectory_annotations (
    annotation_id UUID PRIMARY KEY,
    trial_id UUID REFERENCES trajectory_trials(trial_id) ON DELETE CASCADE,
    run_id UUID NOT NULL REFERENCES agent_tasks(run_id) ON DELETE CASCADE,
    tenant_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('user_feedback', 'human_review', 'verifier_label')),
    label_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    rubric_version TEXT,
    content_key TEXT,
    content_sha256 TEXT CHECK (content_sha256 IS NULL OR content_sha256 ~ '^[0-9a-f]{64}$'),
    source_acl_digest TEXT,
    training_allowed BOOLEAN NOT NULL DEFAULT false,
    training_purpose TEXT,
    training_permission_version TEXT,
    reviewer TEXT,
    status TEXT NOT NULL CHECK (status IN ('unrated', 'candidate', 'approved', 'rejected', 'revoked')),
    reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at TIMESTAMPTZ
);

CREATE INDEX trajectory_annotations_run_idx ON trajectory_annotations (run_id, status, created_at DESC);
CREATE INDEX trajectory_annotations_training_idx
    ON trajectory_annotations (tenant_id, status, training_allowed)
    WHERE status = 'approved' AND training_allowed = true;

CREATE TABLE training_snapshots (
    snapshot_id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    created_by TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('candidate', 'approved', 'rejected', 'consumed', 'expired', 'revoked')),
    dataset_key TEXT NOT NULL,
    dataset_sha256 TEXT NOT NULL CHECK (dataset_sha256 ~ '^[0-9a-f]{64}$'),
    dataset_size BIGINT NOT NULL CHECK (dataset_size >= 0),
    policy_version TEXT NOT NULL,
    split_json JSONB NOT NULL CHECK (split_json ? 'train' AND split_json ? 'validation'),
    base_model_digest TEXT NOT NULL,
    approved_by TEXT,
    approved_at TIMESTAMPTZ,
    revoke_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE training_snapshot_items (
    snapshot_id UUID NOT NULL REFERENCES training_snapshots(snapshot_id) ON DELETE CASCADE,
    item_id TEXT NOT NULL,
    split TEXT NOT NULL CHECK (split IN ('train', 'validation')),
    source_type TEXT NOT NULL CHECK (source_type = 'trajectory_annotation'),
    source_id UUID NOT NULL REFERENCES trajectory_annotations(annotation_id),
    source_tenant_id TEXT NOT NULL,
    source_sha256 TEXT NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    source_acl_digest TEXT,
    training_allowed BOOLEAN NOT NULL,
    training_purpose TEXT NOT NULL,
    training_permission_version TEXT NOT NULL,
    transform_digest TEXT,
    PRIMARY KEY (snapshot_id, item_id),
    UNIQUE (snapshot_id, source_id)
);

CREATE INDEX training_snapshot_items_source_idx ON training_snapshot_items (source_id);

CREATE TABLE adapter_manifests (
    adapter_id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    snapshot_id UUID NOT NULL REFERENCES training_snapshots(snapshot_id),
    base_model_digest TEXT NOT NULL,
    tokenizer_digest TEXT NOT NULL,
    artifact_key TEXT NOT NULL,
    artifact_sha256 TEXT NOT NULL CHECK (artifact_sha256 ~ '^[0-9a-f]{64}$'),
    artifact_size BIGINT NOT NULL CHECK (artifact_size >= 0),
    config_json JSONB NOT NULL,
    environment_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    safety_scan_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    evaluation_id UUID REFERENCES evaluation_campaigns(evaluation_id),
    state TEXT NOT NULL CHECK (state IN ('candidate', 'verified', 'revoked')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at TIMESTAMPTZ,
    revoke_reason TEXT
);

ALTER TABLE release_records
    ADD COLUMN release_kind TEXT NOT NULL DEFAULT 'model',
    ADD COLUMN release_scope TEXT NOT NULL DEFAULT 'single_tenant_lora',
    ADD COLUMN adapter_id UUID REFERENCES adapter_manifests(adapter_id),
    ADD COLUMN evaluation_id UUID REFERENCES evaluation_campaigns(evaluation_id),
    ADD COLUMN training_snapshot_id UUID REFERENCES training_snapshots(snapshot_id),
    ADD COLUMN baseline_release_id UUID REFERENCES release_records(release_id),
    ADD COLUMN policy_version TEXT,
    ADD COLUMN manifest_sha256 TEXT CHECK (manifest_sha256 IS NULL OR manifest_sha256 ~ '^[0-9a-f]{64}$'),
    ADD COLUMN version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0);

ALTER TABLE agent_jobs DROP CONSTRAINT IF EXISTS agent_jobs_kind_check;
ALTER TABLE agent_jobs ADD CONSTRAINT agent_jobs_kind_check
    CHECK (kind IN ('spark_rough_clean', 'lora_train', 'model_evaluate'));

CREATE INDEX release_records_scope_idx ON release_records (tenant_id, release_scope, status, updated_at DESC);

-- Reviewers need the task owner only to enforce maker-checker on annotations;
-- this is read-only and remains tenant-scoped.
CREATE POLICY h5_reviewer_task_read_policy ON agent_tasks
    FOR SELECT
    USING (
        tenant_id = current_setting('app.tenant_id', true)
        AND current_setting('app.role', true) IN ('admin', 'reviewer')
    );

-- Runtime users may read only the already-promoted tenant adapter.  Candidate,
-- canary and rollback control remains admin-only through migration 006.
CREATE POLICY release_records_promoted_read_policy ON release_records
    FOR SELECT
    USING (
        tenant_id = current_setting('app.tenant_id', true)
        AND status = 'promoted'
        AND release_scope = 'single_tenant_lora'
    );

ALTER TABLE evaluation_campaigns ENABLE ROW LEVEL SECURITY;
ALTER TABLE trajectory_trials ENABLE ROW LEVEL SECURITY;
ALTER TABLE trajectory_annotations ENABLE ROW LEVEL SECURITY;
ALTER TABLE training_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE training_snapshot_items ENABLE ROW LEVEL SECURITY;
ALTER TABLE adapter_manifests ENABLE ROW LEVEL SECURITY;
ALTER TABLE evaluation_campaigns FORCE ROW LEVEL SECURITY;
ALTER TABLE trajectory_trials FORCE ROW LEVEL SECURITY;
ALTER TABLE trajectory_annotations FORCE ROW LEVEL SECURITY;
ALTER TABLE training_snapshots FORCE ROW LEVEL SECURITY;
ALTER TABLE training_snapshot_items FORCE ROW LEVEL SECURITY;
ALTER TABLE adapter_manifests FORCE ROW LEVEL SECURITY;

CREATE POLICY evaluation_campaigns_tenant_policy ON evaluation_campaigns
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
CREATE POLICY trajectory_trials_tenant_policy ON trajectory_trials
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
CREATE POLICY trajectory_annotations_tenant_policy ON trajectory_annotations
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
CREATE POLICY training_snapshots_tenant_policy ON training_snapshots
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));
CREATE POLICY training_snapshot_items_tenant_policy ON training_snapshot_items
    USING (EXISTS (
        SELECT 1 FROM training_snapshots s
        WHERE s.snapshot_id = training_snapshot_items.snapshot_id
          AND s.tenant_id = current_setting('app.tenant_id', true)
    ))
    WITH CHECK (EXISTS (
        SELECT 1 FROM training_snapshots s
        WHERE s.snapshot_id = training_snapshot_items.snapshot_id
          AND s.tenant_id = current_setting('app.tenant_id', true)
    ));
CREATE POLICY adapter_manifests_tenant_policy ON adapter_manifests
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

GRANT SELECT, INSERT, UPDATE, DELETE ON
    evaluation_campaigns, trajectory_trials, trajectory_annotations,
    training_snapshots, training_snapshot_items, adapter_manifests TO dataalchemy_app;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dataalchemy_verifier') THEN
        GRANT SELECT ON
            evaluation_campaigns, trajectory_trials, trajectory_annotations,
            training_snapshots, training_snapshot_items, adapter_manifests TO dataalchemy_verifier;
    END IF;
END $$;
