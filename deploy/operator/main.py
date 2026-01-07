import kopf
import kubernetes
import os
import time
import logging
import yaml

# --- Logging ---
LOG_FILE = "/app/data/logs/app.log"
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
)
logger = logging.getLogger("DataAlchemyOperator")

TEMPLATE_FILE = "templates.yaml"

def load_templates(variables):
    """Load and render YAML templates with variables."""
    # Ensure we load from the same directory as the script
    base_dir = os.path.dirname(__file__)
    template_path = os.path.join(base_dir, TEMPLATE_FILE)
    
    with open(template_path, 'r') as f:
        content = f.read()
    
    for key, value in variables.items():
        placeholder = f"{{{{{key}}}}}"
        content = content.replace(placeholder, str(value))
    
    return list(yaml.safe_load_all(content))

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
    
    variables = {
        "NAME": name,
        "NAMESPACE": namespace,
        "REDIS_REPLICAS": spec.get('cache', {}).get('replicas', 1),
        "MINIO_REPLICAS": spec.get('storage', {}).get('replicas', 1),
        "SECRET_NAME": spec.get('credentialsSecret', 'dataalchemy-secret'),
        "SPARK_IMAGE": spec.get('compute', {}).get('spark', {}).get('image', 'data-processor:latest'),
    }
    
    # 1. & 2. Deploy Redis and MinIO (Deployments & Services)
    templates = load_templates(variables)
    for resource in templates:
        # Skip Jobs in the main reconciliation loop unless triggered
        if resource['kind'] == "Job":
            continue
        create_managed_resource(kwargs.get('body'), resource)

    # 3. Spark Job Trigger
    ingest_request = annotations.get('dataalchemy.io/request-ingest')
    if ingest_request:
        job_name = f"{name}-spark-ingest-{ingest_request}"
        logger.info(f"⚡ Ingest request detected: {ingest_request} -> Job: {job_name}")
        
        # Check if Job already exists to avoid duplicate spawning on same request
        batch_api = kubernetes.client.BatchV1Api()
        try:
            batch_api.read_namespaced_job(job_name, namespace)
            logger.info(f"⏭️ Job {job_name} already exists. Skipping.")
        except kubernetes.client.exceptions.ApiException as e:
            if e.status == 404:
                job_variables = variables.copy()
                job_variables["JOB_NAME"] = job_name
                
                # Reload and filter for Job
                templates = load_templates(job_variables)
                for resource in templates:
                    if resource['kind'] == "Job":
                        create_managed_resource(kwargs.get('body'), resource)
            else:
                raise

    return {"status": "Active", "message": "Declarative templates applied"}
