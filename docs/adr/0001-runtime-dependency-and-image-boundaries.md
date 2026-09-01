# ADR 0001: Runtime dependency and image boundaries

- Status: accepted
- Date: 2026-09-01

## Decision

The project keeps only PostgreSQL/S3/configuration clients in the shared package dependency set.
PEP 735 groups own the remaining runtime surface:

- `web`: WebUI, RAG, local inference, authentication and Kubernetes Job submission;
- `training`: governed LoRA/evaluation Job dependencies;
- `etl`: Spark cleaning and Presidio, including the fixed spaCy model;
- `dev`: test and lint tools.

Developer and CI environments install all four groups. Deployment builds must disable default
groups: `webui` installs `web`, `harness-job` installs `training`, and `Dockerfile.harness` installs
`etl`. Helm routes `images.core`, `images.harnessJob`, and `images.etl` to those distinct images.
The Kubernetes Operator retains its own small project manifest.

## Consequences

No deployment image may rely on another role's Python packages. Image tags and digests must be
recorded separately, and an unqualified `docker build .` is not a supported deployment command.
The incremental `Dockerfile.presidio` remains a Web cloud-safety flavor and pins the same Presidio,
spaCy, and model versions as the lock file.
