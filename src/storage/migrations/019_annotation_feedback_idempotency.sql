-- Some long-lived environments applied migration 016 before this index was added.
CREATE UNIQUE INDEX IF NOT EXISTS trajectory_annotations_feedback_source_unique
    ON trajectory_annotations (tenant_id, run_id, kind, content_key)
    WHERE kind = 'user_feedback' AND content_key IS NOT NULL;
