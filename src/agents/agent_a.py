import sys
import time
from typing import Any, Dict

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException

from utils.logger import logger


class AgentA:
    def __init__(self, mode="spark", namespace="data-alchemy"):
        self.mode = mode
        self.namespace = namespace
        self._init_k8s()

    def _init_k8s(self):
        """Initialize Kubernetes client with local or in-cluster config."""
        try:
            config.load_kube_config()
            logger.info("Loaded local KubeConfig")
        except Exception:
            try:
                config.load_incluster_config()
                logger.info("Loaded In-Cluster KubeConfig")
            except Exception as e:
                logger.error(f"Failed to initialize Kubernetes client: {e}")
                sys.exit(1)

        self.core_api = client.CoreV1Api()
        self.batch_api = client.BatchV1Api()
        self.custom_api = client.CustomObjectsApi()

    def clean_and_split(self) -> Dict[str, Any]:
        """Trigger data cleaning by requesting the DataAlchemy Operator."""
        logger.info(f"Starting data cleaning pipeline (Mode: {self.mode})...")

        # 1. Generate Request ID
        request_id = str(int(time.time()))
        job_name = f"dataalchemy-spark-ingest-{request_id}"

        logger.info(f"Triggering Spark cleaning via Operator (RequestID: {request_id})...")

        try:
            # Patch DataAlchemyStack CR to trigger ingest
            patch_body = {
                "metadata": {
                    "annotations": {
                        "dataalchemy.io/request-ingest": request_id
                    }
                }
            }

            self.custom_api.patch_namespaced_custom_object(
                group="dataalchemy.io",
                version="v1alpha1",
                namespace=self.namespace,
                plural="dataalchemystacks",
                name="dataalchemy",
                body=patch_body
            )
            logger.info(f"Successfully requested Ingestion. Expected Job: {job_name}")

            # 2. Track Job Status
            logger.info(f"Waiting for Operator to spawn Job: {job_name}...")
            job_found = False
            for _ in range(20): # 60s timeout
                try:
                    self.batch_api.read_namespaced_job(job_name, self.namespace)
                    job_found = True
                    break
                except ApiException as e:
                    if e.status != 404:
                        raise
                time.sleep(3)

            if not job_found:
                logger.error(f"Operator failed to launch Spark Job '{job_name}' within timeout.")
                return {"status": "error"}

            # 3. Wait for Pod to start
            logger.info("Job found. Waiting for Pod to be ready...")
            pod_name = None
            for _ in range(30): # 90s timeout
                pods = self.core_api.list_namespaced_pod(
                    self.namespace,
                    label_selector=f"job-name={job_name}"
                )
                if pods.items:
                    pod = pods.items[0]
                    pod_name = pod.metadata.name
                    phase = pod.status.phase
                    if phase in ["Running", "Succeeded", "Failed"]:
                        break
                    logger.info(f"Pod {pod_name} phase: {phase}...")
                time.sleep(3)

            if not pod_name:
                logger.error("Failed to find pod for job.")
                return {"status": "error"}

            # 4. Stream Logs
            logger.info(f"Streaming logs from {pod_name}...")
            try:
                log_stream = self.core_api.read_namespaced_pod_log(
                    name=pod_name,
                    namespace=self.namespace,
                    follow=True,
                    _preload_content=False
                )

                for line in log_stream.stream():
                    clean_line = line.decode('utf-8').strip()
                    if clean_line:
                        logger.info(f"[Spark] {clean_line}")

            except Exception as e:
                logger.warning(f"Log streaming interrupted: {e}")

            return {"status": "success", "job": job_name}

        except ApiException as e:
            logger.error(f"Kubernetes API error: {e}")
            return {"status": "error", "reason": str(e)}
        except Exception as e:
            logger.error(f"Unexpected error: {e}")
            return {"status": "error", "reason": str(e)}
