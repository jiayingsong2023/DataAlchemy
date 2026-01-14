# Data Alchemy: Enterprise RAG + LoRA Multi-Agent System

This project is an enterprise-level artificial intelligence system that forms a **closed data loop**: internal enterprise data (Jira, Git PRs, documents, database content, backup data, etc.), data cleaning (Spark), model fine-tuning (LoRA), augmented retrieval (RAG), joint inference, and data feedback to implement AI Auto-Evolution.

The main branch is optimized for **Linux (Ubuntu/Debian)** running **K3d (Kubernetes)** with **ROCm (AMD GPU)** support.

## 📚 Architecture
![DataAlchemy](https://github.com/user-attachments/assets/e20fdd5f-9329-4988-8c67-fa77a69f1caa)

### Cloud-Native Hybrid Stack
This project uses a **Kubernetes Operator** to manage the lifecycle of core infrastructure in **K3d**.
- **Operator (Kopf)**: Manages the `DataAlchemyStack` Custom Resource.
- **Infrastructure**: Automates the deployment of **MinIO** (S3) and **Redis** (Cache).
- **Networking**: Services are exposed via **Traefik Ingress** on `minio.test` and `data-alchemy.test`.
- **Persistence**: Data is persisted to the host via K3d volume mounts.

## 🚀 Key Features

-   **Kubernetes Operator**: One-click deployment and management of the entire backend stack.
-   **Multi-Agent Architecture**:
    -   **Agent A (Cleaner)**: Triggers distributed Spark jobs via K8s Operator.
    -   **Agent B (Trainer)**: Specialized LoRA domain training.
    -   **Agent C (Knowledge)**: FAISS-powered high-speed vector search with S3 sync.
    -   **Agent D (Finalist)**: Intelligent fusion of RAG facts and LoRA intuition.
    -   **Agent S (Scheduler)**: Automates periodic ingestion and training.
-   **Optimized Inference Engine**:
    -   **AMD GPU Acceleration**: Leverages `torch.compile` (Inductor) and FP16 mixed-precision for ROCm.
    -   **Dynamic Batching**: High-throughput inference with `BatchInferenceEngine`.
-   **Distributed RAG (Agent C)**:
    -   **S3 Persistence**: FAISS index and metadata are stored in MinIO/S3.
    -   **Hot Reloading**: Background sync thread updates the local knowledge base from S3.
-   **Multi-User Auth & Session Management**:
    -   **JWT/OAuth2**: Secure token-based authentication.
    -   **Redis Session & History**: Persistent chat history.

---

## 🛠️ Getting Started

### 1. Prerequisites
-   **OS**: Linux (Ubuntu 20.04/22.04 recommended)
-   **AMD GPU**: Compatible with ROCm driver installed.
-   **Docker**: Ensure `docker` service is running.
-   **k3d**: Lightweight k3s cluster (`curl -s https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | bash`).
-   **Kubectl**: Kubernetes CLI tool.
-   **uv**: [Install uv](https://github.com/astral-sh/uv).

### 2. Environment & Cluster Setup

**1. One-Click Cluster Setup:**
This script creates a K3d cluster, builds Docker images, and deploys the entire stack.
```bash
# Set up k3d, build images, and deploy manifests
./scripts/setup/setup_k3d.sh
./scripts/k3d-deploy.sh
```

**2. Configure API Keys:**
Before running, you must configure your API keys (e.g., DeepSeek) for the WebUI.
Edit `deploy/k3d/05-webui.yaml` or use the command below (recommended):
```bash
kubectl set env deployment/webui DEEPSEEK_API_KEY="your_actual_api_key" -n data-alchemy
kubectl rollout restart deployment/webui -n data-alchemy
```

**3. Configure Python Environment (Local):**
```bash
uv sync
```

**4. Networking Setup (DNS):**
To access the services using their domain names, add the following to your `/etc/hosts` file:
```bash
# Get the LoadBalancer IP (usually 172.19.0.2)
# Add to /etc/hosts:
172.19.0.2 data-alchemy.test minio.test minio-console.test
```

### 3. Accessing the System

| Service | Access URL | Default Credentials |
| :--- | :--- | :--- |
| **WebUI** | [http://data-alchemy.test](http://data-alchemy.test) | `admin` / `admin123` |
| **MinIO Console** | [http://minio-console.test](http://minio-console.test) | `admin` / `minioadmin` |
| **MinIO API** | `http://minio.test` | `admin` / `minioadmin` |

---

## 🔄 End-to-End Workflow

This section guides you through the full data lifecycle: from uploading raw data to chatting with a fine-tuned model.

### Step 1: Upload Raw Data
Use the helper script to upload your local documents (TXT, PDF, MD) to the MinIO `raw` bucket.
```bash
# This script automatically handles S3 connection details
uv run python scripts/ops/manage_minio.py upload
```
*Verification*: Check [http://minio-console.test](http://minio-console.test) -> Browser -> `lora-data` -> `raw`.

### Step 2: Trigger Data Cleaning (Spark ETL)
The Operator watches for annotations to trigger Spark jobs. This runs **Agent A** to clean raw data and produce:
1.  **Refined Data**: Clean text for RAG.
2.  **SFT Pairs**: Validated Question-Answer pairs for Fine-tuning.

```bash
# Trigger the Ingest/Clean pipeline
kubectl patch das dataalchemy -n data-alchemy --type merge -p '{"metadata": {"annotations": {"dataalchemy.io/request-ingest": "now"}}}'
```
*Wait*: This starts a Spark job pod. Check status with `kubectl get pods -n data-alchemy`.

### Step 3: Run Model Fine-Tuning (LoRA)
Once data is cleaned (`sft_train.jsonl` exists in MinIO), submit the LoRA training job. This runs **Agent B** in training mode.

```bash
# Submit the training job
kubectl delete -f deploy/k3d/07-lora-job.yaml # Delete old job if exists
kubectl apply -f deploy/k3d/07-lora-job.yaml
```
*Monitor*: `kubectl logs -l job-name=lora-training -n data-alchemy -f`
*Result*: A LoRA adapter is saved to the shared volume (`/app/data/lora-tiny-llama-adapter`).

### Step 4: Chat & Inference
The WebUI (`Agent D`) automatically loads the new RAG index and LoRA adapter (after restart or sync).

1.  Open [http://data-alchemy.test](http://data-alchemy.test).
2.  Login with `admin` / `admin123`.
3.  Ask a question about your uploaded data.
4.  Observe the logs to see the "Combined RAG + Intuition" reasoning.

---

## 🔧 Troubleshooting

-   **Browser Can't Connect**:
    -   Ensure `/etc/hosts` includes `data-alchemy.test`.
    -   **Firefox**: Go to `about:config`, set `network.dns.native-is-localhost` to `false`. Disable "DNS over HTTPS" in Settings.
    -   **VPN/Proxy**: Add `data-alchemy.test` and `172.19.0.0/16` to your "No Proxy" list.

-   **HuggingFace Connection Timeout**:
    -   If containers cannot download models, check your host's VPN settings. Ensure "Allow LAN" is enabled and HTTP_PROXY env vars are set in the deployment if needed.

-   **Pod Restarting (Liveness Probe)**:
    -   The first startup downloads 2GB+ models. If the pod restarts too soon, increase `initialDelaySeconds` in `deploy/k3d/05-webui.yaml`.
