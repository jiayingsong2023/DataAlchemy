"""
Agent A: The Cleaner (Data Alchemy).
Wraps the Spark/Python ETL engines. Supports cross-environment Spark execution via WSL.
"""
import os
import json
import subprocess
from utils.logger import logger
import sys
from config import WASHED_DATA_PATH, RAG_CHUNKS_PATH

class AgentA:
    def __init__(self, mode="spark"):
        self.mode = mode
        self.engine = None

    def clean_and_split(self):
        """Perform data cleaning and semantic chunking using Spark on Kubernetes."""
        logger.info(f"Starting data cleaning pipeline (Mode: {self.mode})...")
        
        # 1. K8s Mode -> Trigger Job in Kubernetes
        logger.info("Attempting to trigger Spark cleaning in K8s...")
        try:
            # Find project root relative to this file: src/agents/agent_a.py -> project_root
            current_file_dir = os.path.dirname(os.path.abspath(__file__))
            base_dir = os.path.dirname(os.path.dirname(current_file_dir))
            
            rbac_path = os.path.normpath(os.path.join(base_dir, "k8s", "spark-rbac.yaml"))
            job_path = os.path.normpath(os.path.join(base_dir, "k8s", "spark-job.yaml"))

            # 1. Ensure RBAC is applied
            logger.info(f"Applying RBAC: {rbac_path.replace('\\', '/')}")
            if not os.path.exists(rbac_path):
                logger.error(f"RBAC file missing at {rbac_path}")
                raise FileNotFoundError(f"Missing K8s config: {rbac_path}")
            
            subprocess.run(f'kubectl apply -f "{rbac_path}"', shell=True, check=True)
            
            # 2. Cleanup previous job if exists
            subprocess.run("kubectl delete job spark-data-cleaner --ignore-not-found=true", shell=True, check=True)
            
            # 3. Apply the Spark Job
            logger.info(f"Submitting K8s Job: {job_path.replace('\\', '/')}")
            subprocess.run(f'kubectl apply -f "{job_path}"', shell=True, check=True)
            
            # 4. Stream Logs (Unified Logging)
            logger.info("Waiting for Spark pod to initialize...")
            try:
                # Wait up to 60s for the pod to be created and start running
                import time
                pod_ready = False
                for _ in range(12): # 12 * 5s = 60s
                    # Check if any pod for this job is running or finished
                    check_pod = subprocess.run(
                        "kubectl get pods -l job-name=spark-data-cleaner -o jsonpath='{.items[0].status.phase}'",
                        shell=True, capture_output=True, text=True
                    )
                    phase = check_pod.stdout.strip().replace("'", "")
                    if phase in ["Running", "Succeeded", "Failed"]:
                        pod_ready = True
                        break
                    logger.info(f"Pod phase: {phase or 'Pending'}. Waiting...")
                    time.sleep(5)
                
                if pod_ready:
                    logger.info("Streaming Spark job logs...")
                    log_process = subprocess.Popen(
                        "kubectl logs -f job/spark-data-cleaner --all-containers=true", 
                        shell=True, 
                        stdout=subprocess.PIPE, 
                        stderr=subprocess.STDOUT,
                        text=True
                    )
                    
                    for line in log_process.stdout:
                        clean_line = line.strip()
                        if clean_line:
                            # Prefix with [Spark] to distinguish from local logs
                            logger.info(f"[Spark] {clean_line}")
                    
                    log_process.wait()
                else:
                    logger.warning("Spark pod did not start in time. Skipping log stream.")
            except Exception as log_err:
                logger.warning(f"Could not stream Spark logs: {log_err}")

            # 5. Wait for Job completion (Final check)
            logger.info("Verifying Spark Job completion...")
            subprocess.run("kubectl wait --for=condition=complete job/spark-data-cleaner --timeout=30s", shell=True, check=True)
            
            logger.info("K8s Spark task completed successfully.")
            return {"status": "success", "engine": "spark_on_k8s"}
            
        except Exception as e:
            logger.error(f"K8s execution failed: {e}.", exc_info=True)
            sys.exit(1)
