# Script support policy

Scripts are independent entrypoints only when operators or evidence replay require them. New files
must be assigned one of the classes below; phase names alone do not make a script supported.

## Supported

These are maintained operator entrypoints. Python files only parse configuration and call package
code; shell files only orchestrate native tools.

- `migrate_postgres.py`, `pilot_check.py`, `pilot_up.sh`
- `helm-deploy.sh` (local k3d deployment)
- `setup/setup_k3d.sh`, `setup/verify_gpu.sh`

## Release evidence

These preserve independent, replayable evaluation, decision, recovery, or governed-training
commands. They are not general product CLI promises.

- `evaluate_phase*.py`, `evaluate_repeated_release.py`
- `decide_*.py`, `promote_tiered_release.py`
- `compile_sft_experiences.py`, `merge_gap_reports.py`, `train_compiled_snapshot.py`
- `publish_rag_suite.py`, `publish_trial_experiences.py`, `rerollout_task_bundles.py`
- `reset_pilot_environment.py`, `verify_pilot_restore.sh`
- `run_h5_pdf_cycle.py`, `run_h5_rehearsal.py`, `run_h6_pilot_ready_rehearsal.py`
- `run_pdf_full_cycle.py`

Historical phase scripts stay here while CI, current documentation, tests, or evidence replay still
reference them. Removal requires all four reference classes to be empty.

## Development

These are diagnostic, fixture, workstation setup, or human-analysis helpers. They may change with
the development environment and are not production entrypoints.

- `analyze_holdout_failures.py`, `build_pdf_training_candidates.py`
- `import_multidoc2dial_fixture.py`, `review_gap_with_deepseek.py`
- `core/*`, `ops/*`
- `setup/configure_*`, `setup/download_*`, `setup/fix_*`, `setup/install_*`

## Archived

Archived implementation and phase documentation live under `docs/archive/`; archived executables
do not remain runnable in the active `scripts/` tree. R6 removed `setup/setup_operator.sh` because it
had no current caller and targeted the deleted `data_processor/` build context. Git history remains
the recovery mechanism.
