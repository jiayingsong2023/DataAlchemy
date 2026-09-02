CREATE INDEX trajectory_annotations_source_acl_idx
    ON trajectory_annotations (tenant_id, source_acl_digest)
    WHERE source_acl_digest IS NOT NULL;

CREATE INDEX trajectory_annotations_permission_idx
    ON trajectory_annotations (tenant_id, training_permission_version)
    WHERE training_permission_version IS NOT NULL;

CREATE INDEX trajectory_annotations_evidence_idx
    ON trajectory_annotations USING GIN (label_json jsonb_path_ops);
