import kopf
import kubernetes
import os
import time
import logging

# --- Logging ---
LOG_FILE = "/app/data/logs/app.log"
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)
logger = logging.getLogger("DataAlchemyOperator")

def create_managed_resource(owner, resource_data):
    kopf.adopt(resource_data, owner=owner)
    api = kubernetes.client.CoreV1Api()
    apps_api = kubernetes.client.AppsV1Api()
    batch_api = kubernetes.client.BatchV1Api()
    
    kind = resource_data['kind']
    namespace = resource_data['metadata'].get('namespace', 'default')
    name = resource_data['metadata']['name']
    
    try:
        if kind == "Service":
            api.create_namespaced_service(namespace, resource_data)
        elif kind == "Deployment":
            apps_api.create_namespaced_deployment(namespace, resource_data)
        elif kind == "Job":
            batch_api.create_namespaced_job(namespace, resource_data)
    except kubernetes.client.exceptions.ApiException as e:
        if e.status == 409:
            try:
                if kind == "Service":
                    api.patch_namespaced_service(name, namespace, resource_data)
                elif kind == "Deployment":
                    apps_api.patch_namespaced_deployment(name, namespace, resource_data)
            except: pass
        else: raise

@kopf.on.create('dataalchemy.io', 'v1alpha1', 'dataalchemystacks')
@kopf.on.update('dataalchemy.io', 'v1alpha1', 'dataalchemystacks')
def reconcile_stack(spec, name, namespace, annotations, **kwargs):
    logger.info(f"🚀 [Hybrid Mode] Reconciling: {name}")
    
    # 凭据配置从 Secret 中获取
    secret_name = spec.get('credentialsSecret', 'dataalchemy-secret')
    
    # 1. Redis (1 Replica + LoadBalancer)
    redis_replicas = spec.get('cache', {}).get('replicas', 1)
    redis_name = f"{name}-redis"
    create_managed_resource(kwargs.get('body'), {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": redis_name, "namespace": namespace, "labels": {"app": "redis", "stack": name, "dataalchemy.io/managed": "true"}},
        "spec": {
            "replicas": redis_replicas,
            "selector": {"matchLabels": {"app": "redis", "stack": name}},
            "template": {
                "metadata": {"labels": {"app": "redis", "stack": name, "component": "redis"}},
                "spec": {"containers": [{
                    "name": "redis", 
                    "image": "redis:7.0-alpine", 
                    "args": ["redis-server", "--appendonly", "yes"],
                    "ports": [{"containerPort": 6379}],
                    "volumeMounts": [{"name": "redis-storage", "mountPath": "/data"}]
                }],
                "volumes": [{
                    "name": "redis-storage",
                    "hostPath": {
                        "path": "/run/desktop/mnt/host/c/Users/Administrator/work/LoRA/data/redis_data",
                        "type": "DirectoryOrCreate"
                    }
                }]}
            }
        }
    })
    create_managed_resource(kwargs.get('body'), {
        "apiVersion": "v1", "kind": "Service",
        "metadata": {"name": redis_name, "namespace": namespace, "labels": {"stack": name, "dataalchemy.io/managed": "true"}},
        "spec": {"type": "LoadBalancer", "selector": {"app": "redis", "stack": name}, "ports": [{"port": 6379, "targetPort": 6379}]}
    })

    # 2. MinIO (1 Replica + LoadBalancer)
    minio_name = f"{name}-minio"
    create_managed_resource(kwargs.get('body'), {
        "apiVersion": "apps/v1", "kind": "Deployment",
        "metadata": {"name": minio_name, "namespace": namespace, "labels": {"app": "minio", "stack": name, "dataalchemy.io/managed": "true"}},
        "spec": {
            "replicas": spec.get('storage', {}).get('replicas', 1),
            "selector": {"matchLabels": {"app": "minio", "stack": name}},
            "template": {
                "metadata": {"labels": {"app": "minio", "stack": name, "component": "minio"}},
                "spec": {"containers": [{
                    "name": "minio", "image": "minio/minio:latest",
                    "args": ["server", "/data", "--console-address", ":9001"],
                    "env": [
                        {
                            "name": "MINIO_ROOT_USER", 
                            "valueFrom": {"secretKeyRef": {"name": secret_name, "key": "AWS_ACCESS_KEY_ID"}}
                        },
                        {
                            "name": "MINIO_ROOT_PASSWORD", 
                            "valueFrom": {"secretKeyRef": {"name": secret_name, "key": "AWS_SECRET_ACCESS_KEY"}}
                        }
                    ],
                    "ports": [{"containerPort": 9000}, {"containerPort": 9001}],
                    "volumeMounts": [{"name": "minio-storage", "mountPath": "/data"}]
                }],
                "volumes": [{
                    "name": "minio-storage",
                    "hostPath": {
                        "path": "/run/desktop/mnt/host/c/Users/Administrator/work/LoRA/data/minio_data",
                        "type": "DirectoryOrCreate"
                    }
                }]
            }
        }
    })
    create_managed_resource(kwargs.get('body'), {
        "apiVersion": "v1", "kind": "Service",
        "metadata": {"name": minio_name, "namespace": namespace, "labels": {"stack": name, "dataalchemy.io/managed": "true"}},
        "spec": {"type": "LoadBalancer", "selector": {"app": "minio", "stack": name}, 
                 "ports": [{"name": "api", "port": 9000, "targetPort": 9000}, {"name": "console", "port": 9001, "targetPort": 9001}]}
    })

    # 3. Spark Job Trigger (Input from S3, Output to S3)
    ingest_request = annotations.get('dataalchemy.io/request-ingest')
    if ingest_request:
        logger.info(f"⚡ Ingest request detected: {ingest_request}")
        job_name = f"{name}-spark-ingest-{int(time.time())}"
        create_managed_resource(kwargs.get('body'), {
            "apiVersion": "batch/v1", "kind": "Job",
            "metadata": {"name": job_name, "namespace": namespace, "labels": {"stack": name, "component": "spark-ingest", "dataalchemy.io/managed": "true"}},
            "spec": {
                "ttlSecondsAfterFinished": 600,
                "template": {"spec": {
                    "serviceAccountName": "spark",
                    "containers": [{
                        "name": "spark-driver", 
                        "image": spec.get('compute', {}).get('spark', {}).get('image', 'data-processor:latest'),
                        "imagePullPolicy": "Never",
                        "env": [
                            {"name": "REDIS_URL", "value": f"redis://{redis_name}:6379"},
                            {"name": "S3_ENDPOINT", "value": f"http://{minio_name}:9000"},
                            {
                                "name": "AWS_ACCESS_KEY_ID", 
                                "valueFrom": {"secretKeyRef": {"name": secret_name, "key": "AWS_ACCESS_KEY_ID"}}
                            },
                            {
                                "name": "AWS_SECRET_ACCESS_KEY", 
                                "valueFrom": {"secretKeyRef": {"name": secret_name, "key": "AWS_SECRET_ACCESS_KEY"}}
                            },
                            {"name": "PYTHONPATH", "value": "/app"}
                        ],
                        "command": ["python", "main.py"],
                        "args": ["--input", "s3a://lora-data/raw", "--output", "s3a://lora-data/processed"],
                        "volumeMounts": [{"name": "data-volume", "mountPath": "/app/data"}]
                    }],
                    "volumes": [{"name": "data-volume", "hostPath": {"path": "/run/desktop/mnt/host/c/Users/Administrator/work/LoRA/data", "type": "Directory"}}],
                    "restartPolicy": "Never"
                }}, "backoffLimit": 0}
        })

    return {"status": "Active", "message": "Spark Configured: S3 -> S3"}
