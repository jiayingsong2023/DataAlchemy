import kopf
import kubernetes
import os
import sys
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

def resolve_data_path(spec, namespace):
    """
    Resolve data path based on environment and platform.
    Priority:
    1. Spec override (dataPath in spec)
    2. Environment variable (DATAALCHEMY_DATA_PATH)
    3. ConfigMap (if exists)
    4. Platform-specific defaults
    """
    # Priority 1: Spec override
    if spec.get('storage', {}).get('dataPath'):
        return spec['storage']['dataPath']
    
    # Priority 2: Environment variable
    env_path = os.getenv("DATAALCHEMY_DATA_PATH")
    if env_path:
        return env_path
    
    # Priority 3: Check ConfigMap (if exists)
    try:
        api = kubernetes.client.CoreV1Api()
        config_map = api.read_namespaced_config_map("dataalchemy-config", namespace)
        if config_map.data and "dataPath" in config_map.data:
            return config_map.data["dataPath"]
    except:
        pass  # ConfigMap doesn't exist, continue
    
    # Priority 4: Platform-specific defaults
    if sys.platform == 'win32':
        # Docker Desktop Windows path
        return "/run/desktop/mnt/host/c/Users/Administrator/work/LoRA/data"
    else:
        # Linux/Ubuntu/k3d: use project root or /data
        # Try to detect project root from common locations
        project_root = os.getenv("PROJECT_ROOT")
        if not project_root:
            # Try common project locations
            if os.path.exists("/home/jack/work/DataAlchemy"):
                project_root = "/home/jack/work/DataAlchemy"
            elif os.path.exists("/data"):
                project_root = "/data"
            else:
                project_root = os.getenv("HOME", "/tmp") + "/work/DataAlchemy"
        return f"{project_root}/data"

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
    
    # Parse YAML and add nodePort for NodePort services
    resources = list(yaml.safe_load_all(content))
    service_type = variables.get('SERVICE_TYPE', 'LoadBalancer')
    
    if service_type == 'NodePort':
        # For k3d, we need fixed nodePorts to match port mappings
        # k3d maps: 9000:30000, 9001:30001, 6379:30002
        for resource in resources:
            if resource.get('kind') == 'Service':
                ports = resource.get('spec', {}).get('ports', [])
                for port in ports:
                    port_name = port.get('name', '')
                    if port_name == 'api' and port.get('port') == 9000:
                        port['nodePort'] = 30000
                    elif port_name == 'console' and port.get('port') == 9001:
                        port['nodePort'] = 30001
                    elif port.get('port') == 6379:  # Redis
                        port['nodePort'] = 30002
    
    return resources

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
                        # For Services, if patch fails (e.g., nodePort conflict), try to update
                        try:
                            api.patch_namespaced_service(name, namespace, resource_data)
                        except kubernetes.client.exceptions.ApiException as patch_e:
                            if patch_e.status == 422:  # Unprocessable Entity (e.g., nodePort conflict)
                                # Service exists but can't be updated due to nodePort conflict
                                # Check if the existing service is correct
                                existing = api.read_namespaced_service(name, namespace)
                                existing_nodeport = None
                                desired_nodeport = None
                                for port in existing.spec.ports:
                                    if port.node_port:
                                        existing_nodeport = port.node_port
                                for port in resource_data.get('spec', {}).get('ports', []):
                                    if port.get('nodePort'):
                                        desired_nodeport = port.get('nodePort')
                                
                                if existing_nodeport == desired_nodeport:
                                    # Service is already correct, skip update
                                    logger.info(f"Service {name} already has correct nodePort {existing_nodeport}, skipping update")
                                else:
                                    # Need to update, but nodePort conflict - delete and recreate
                                    logger.warning(f"Service {name} nodePort conflict ({existing_nodeport} vs {desired_nodeport}), deleting and recreating...")
                                    api.delete_namespaced_service(name, namespace)
                                    api.create_namespaced_service(namespace, resource_data)
                            else:
                                raise
                    elif kind == "Deployment":
                        apps_api.patch_namespaced_deployment(name, namespace, resource_data)
                except Exception as update_e:
                    # Log but don't fail - allow reconciliation to continue
                    logger.warning(f"Failed to update {kind} {name}: {update_e}")
            else: 
                # For other errors, re-raise to fail fast
                raise

@kopf.on.create('dataalchemy.io', 'v1alpha1', 'dataalchemystacks')
@kopf.on.update('dataalchemy.io', 'v1alpha1', 'dataalchemystacks')
def reconcile_stack(spec, name, namespace, annotations, **kwargs):
    logger.info(f"🚀 [Hybrid Mode] Reconciling: {name}")
    
    # Resolve data path dynamically
    data_path = resolve_data_path(spec, namespace)
    logger.info(f"📁 Using data path: {data_path}")
    
    variables = {
        "NAME": name,
        "NAMESPACE": namespace,
        "REDIS_REPLICAS": spec.get('cache', {}).get('replicas', 1),
        "MINIO_REPLICAS": spec.get('storage', {}).get('replicas', 1),
        "SECRET_NAME": spec.get('credentialsSecret', 'dataalchemy-secret'),
        "SPARK_IMAGE": spec.get('compute', {}).get('spark', {}).get('image', 'data-processor:latest'),
        "DATA_PATH": data_path,  # Dynamic data path
        "SERVICE_TYPE": spec.get('serviceType', 'LoadBalancer'),  # LoadBalancer or NodePort
    }
    
    # 1. & 2. Deploy Redis and MinIO (Deployments & Services)
    templates = load_templates(variables)
    for resource in templates:
        # Skip Jobs in the main reconciliation loop unless triggered
        if resource['kind'] == "Job":
            continue
        try:
            create_managed_resource(kwargs.get('body'), resource)
        except Exception as e:
            # Log but don't fail the entire reconciliation
            # This allows Spark Job creation to proceed even if Service update fails
            logger.warning(f"Failed to create/update {resource['kind']} {resource['metadata']['name']}: {e}")

    # 3. Spark Job Trigger (process even if Service updates failed)
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
