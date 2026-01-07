import os
import subprocess
import time
import json
from utils.logger import logger
import sys

class AgentA:
    def __init__(self, mode="spark"):
        self.mode = mode

    def clean_and_split(self):
        """Trigger data cleaning by requesting the DataAlchemy Operator."""
        logger.info(f"Starting data cleaning pipeline (Mode: {self.mode})...")
        
        # 1. 发送带 Request ID 的信号
        request_id = str(int(time.time()))
        # 与 Operator 命名规则对齐：dataalchemy-spark-ingest-[request_id]
        job_name = f"dataalchemy-spark-ingest-{request_id}"
        
        logger.info(f"Triggering Spark cleaning via Operator (RequestID: {request_id})...")
        try:
            patch_cmd = [
                "kubectl", "patch", "das", "dataalchemy", 
                "--type", "merge", 
                "-p", f'{{"metadata": {{"annotations": {{"dataalchemy.io/request-ingest": "{request_id}"}}}}}}'
            ]
            subprocess.run(patch_cmd, check=True, capture_output=True, text=True)
            logger.info(f"Successfully requested Ingestion. Expected Job: {job_name}")

            # 2. 精确追踪 Job 状态
            logger.info(f"Waiting for Operator to spawn Job: {job_name}...")
            found = False
            for _ in range(20): # 60s timeout for spawning
                check_job = subprocess.run(
                    f"kubectl get job {job_name} -o name",
                    shell=True, capture_output=True, text=True
                )
                if f"job.batch/{job_name}" in check_job.stdout:
                    found = True
                    break
                time.sleep(3)

            if not found:
                logger.error(f"Operator failed to launch Spark Job '{job_name}' within timeout.")
                return {"status": "error"}

            # 3. 等待 Pod 启动
            logger.info(f"Job found. Waiting for Pod to be ready...")
            for _ in range(30): # 90s timeout for pod ready
                check_pod = subprocess.run(
                    f"kubectl get pods -l job-name={job_name} -o json",
                    shell=True, capture_output=True, text=True
                )
                if check_pod.stdout.strip():
                    pods_data = json.loads(check_pod.stdout)
                    items = pods_data.get('items', [])
                    if items:
                        phase = items[0].get('status', {}).get('phase')
                        if phase in ["Running", "Succeeded", "Failed"]:
                            break
                        logger.info(f"Pod phase: {phase}...")
                
                time.sleep(3)

            # 4. 流式读取日志
            logger.info(f"Streaming logs from {job_name}...")
            log_process = subprocess.Popen(
                f"kubectl logs -f job/{job_name} --all-containers=true", 
                shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
            )
            for line in log_process.stdout:
                if line.strip(): logger.info(f"[Spark] {line.strip()}")
            log_process.wait()

            return {"status": "success", "job": job_name}

        except Exception as e:
            logger.error(f"Failed to trigger via Operator: {e}")
            sys.exit(1)
