# ADR 0001: Runtime dependency and image boundaries

- Status: accepted
- Date: 2026-09-01

## Decision

The project keeps only PostgreSQL/S3/configuration clients in the shared package dependency set.
PEP 735 groups own the remaining runtime surface:

- `web`: WebUI control plane, RAG clients, authentication and Kubernetes Job submission;
- `inference`: GPU generation, embedding and reranking;
- `training`: governed LoRA/evaluation Job dependencies;
- `etl`: Spark cleaning and Presidio, including the fixed spaCy model;
- `dev`: test and lint tools.

Developer and CI environments install all four groups. Deployment builds must disable default
groups: `webui` installs `web`, `inference` installs `inference`, `harness-job` installs
`inference` plus `training`, and `Dockerfile.harness` installs `etl`. Inference and H5 derive from
the same immutable GHCR `gpu-runtime` digest; `gpu-runtime-build` is the only target that rebuilds
that shared ROCm/PyTorch layer. Helm routes each role to its distinct image. The Kubernetes Operator
retains its own small project manifest.

## Consequences

No deployment image may rely on another role's Python packages. Image tags and digests must be
recorded separately, and an unqualified `docker build .` is not a supported deployment command.
The incremental `Dockerfile.presidio` remains a Web cloud-safety flavor and pins the same Presidio,
spaCy, and model versions as the lock file.
