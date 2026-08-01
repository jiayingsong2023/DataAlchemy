ALTER TABLE agent_tasks
    ADD COLUMN run_id UUID,
    ADD COLUMN task_spec_json JSONB NOT NULL DEFAULT '{"schema_version": 0, "legacy": true}'::jsonb,
    ADD COLUMN plan_version INTEGER NOT NULL DEFAULT 1 CHECK (plan_version > 0),
    ADD COLUMN lease_owner TEXT,
    ADD COLUMN lease_expires_at TIMESTAMPTZ,
    ADD COLUMN heartbeat_at TIMESTAMPTZ,
    ADD COLUMN pause_requested BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN cancel_requested BOOLEAN NOT NULL DEFAULT false;

UPDATE agent_tasks SET run_id = task_id WHERE run_id IS NULL;
ALTER TABLE agent_tasks ALTER COLUMN run_id SET NOT NULL;
CREATE UNIQUE INDEX agent_tasks_run_id_idx ON agent_tasks (run_id);
CREATE INDEX agent_tasks_lease_idx ON agent_tasks (lease_expires_at) WHERE lease_owner IS NOT NULL;

ALTER TABLE agent_tool_runs
    ADD COLUMN run_id UUID,
    ADD COLUMN task_id UUID REFERENCES agent_tasks(task_id) ON DELETE CASCADE,
    ADD COLUMN step_id UUID,
    ADD COLUMN plan_version INTEGER,
    ADD COLUMN attempt INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN state TEXT,
    ADD COLUMN started_at TIMESTAMPTZ,
    ADD COLUMN completed_at TIMESTAMPTZ;

UPDATE agent_tool_runs
SET state = CASE WHEN result_json IS NULL THEN 'reserved' ELSE 'succeeded' END
WHERE state IS NULL;
ALTER TABLE agent_tool_runs ALTER COLUMN state SET NOT NULL;
ALTER TABLE agent_tool_runs ALTER COLUMN state SET DEFAULT 'reserved';
CREATE UNIQUE INDEX agent_tool_runs_task_step_idx
    ON agent_tool_runs (task_id, step_id)
    WHERE task_id IS NOT NULL AND step_id IS NOT NULL;
CREATE INDEX agent_tool_runs_run_idx ON agent_tool_runs (run_id, task_id, step_id);
