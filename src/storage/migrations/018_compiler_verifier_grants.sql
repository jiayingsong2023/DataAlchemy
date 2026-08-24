DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'dataalchemy_verifier') THEN
        GRANT SELECT ON
            agent_tasks,
            run_manifests,
            trajectory_trials,
            trajectory_annotations,
            training_snapshots,
            training_snapshot_items,
            evaluation_campaigns
        TO dataalchemy_verifier;
    END IF;
END $$;
