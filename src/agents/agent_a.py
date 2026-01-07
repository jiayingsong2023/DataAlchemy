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
        logger.info(f"Triggering Spark cleaning via Operator (RequestID: {request_id})...")
        try:
            patch_cmd = [
                "kubectl", "patch", "das", "test-stack", 
                "--type", "merge", 
                "-p", f'{{"metadata": {{"annotations": {{"dataalchemy.io/request-ingest": "{request_id}"}}}}}}'
            ]
            subprocess.run(patch_cmd, check=True, capture_output=True, text=True)
            logger.info("Successfully requested Ingestion from Operator.")

            # 2. 精确查找匹配该 RequestID 的新 Job
            logger.info(f"Waiting for Operator to spawn Job for request {request_id}...")
            job_name = ""
            for _ in range(15): # 45s
                # 获取所有符合标签的 Job，并根据创建时间排序
                check_job = subprocess.run(
                    "kubectl get jobs -l component=spark-ingest --sort-by=.metadata.creationTimestamp -o json",
                    shell=True, capture_output=True, text=True
                )
                if check_job.stdout.strip():
                    jobs_data = json.loads(check_job.stdout)
                    items = jobs_data.get('items', [])
                    if items:
                        # 查找最新的 Job
                        job_name = items[-1]['metadata']['name']
                        break
                time.sleep(3)

            if not job_name:
                logger.error("Operator failed to launch Spark Job.")
                return {"status": "error"}

            # 3. 等待 Pod 启动
            logger.info(f"Found Job: {job_name}. Waiting for Pod to be ready...")
            for _ in range(20):
                check_pod = subprocess.run(
                    f"kubectl get pods -l job-name={job_name} -o jsonpath='{{.items[0].status.phase}}'",
                    shell=True, capture_output=True, text=True
                )
                phase = check_pod.stdout.strip()
                if phase in ["Running", "Succeeded", "Failed"]:
                    break
                logger.info(f"Pod phase: {phase or 'Pending'}...")
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
