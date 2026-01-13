import os
import sys
import argparse
import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
import time
import socket
import subprocess
import signal
import atexit
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Configuration
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MINIO_ENDPOINT = os.getenv("S3_ENDPOINT", "http://localhost:9000")
ACCESS_KEY = os.getenv("AWS_ACCESS_KEY_ID", "minioadmin")
SECRET_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "minioadmin")
BUCKET_NAME = os.getenv("S3_BUCKET", "lora-data")

# Global variable to track port-forward process
_port_forward_process = None

def cleanup_port_forward():
    """Clean up port-forward process on exit"""
    global _port_forward_process
    if _port_forward_process:
        try:
            _port_forward_process.terminate()
            _port_forward_process.wait(timeout=2)
        except:
            try:
                _port_forward_process.kill()
            except:
                pass
        _port_forward_process = None

atexit.register(cleanup_port_forward)

def check_minio_available(endpoint):
    """Check if MinIO is accessible at the given endpoint"""
    try:
        # Parse endpoint URL
        if endpoint.startswith("http://"):
            host_port = endpoint[7:]
        elif endpoint.startswith("https://"):
            host_port = endpoint[8:]
        else:
            host_port = endpoint
        
        if ":" in host_port:
            host, port = host_port.split(":", 1)
            port = int(port)
        else:
            host = host_port
            port = 9000
        
        # Try to connect
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(2)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except Exception:
        return False

def find_minio_service():
    """Find MinIO service in Kubernetes cluster"""
    try:
        # Try to find MinIO service
        result = subprocess.run(
            ["kubectl", "get", "svc", "-o", "json"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode != 0:
            return None
        
        import json
        services = json.loads(result.stdout)
        for svc in services.get("items", []):
            name = svc.get("metadata", {}).get("name", "")
            labels = svc.get("metadata", {}).get("labels", {})
            if "minio" in name.lower() or labels.get("app") == "minio":
                namespace = svc.get("metadata", {}).get("namespace", "default")
                return name, namespace
        return None
    except Exception:
        return None

def setup_port_forward(local_port=9000, remote_port=9000):
    """Set up kubectl port-forward for MinIO service"""
    global _port_forward_process
    
    # Find MinIO service
    service_info = find_minio_service()
    if not service_info:
        print("[!] Could not find MinIO service in Kubernetes cluster")
        return False
    
    service_name, namespace = service_info
    print(f"[*] Found MinIO service: {service_name} in namespace {namespace}")
    print(f"[*] Setting up port-forward: localhost:{local_port} -> {service_name}:{remote_port}")
    
    try:
        # Start port-forward in background
        _port_forward_process = subprocess.Popen(
            ["kubectl", "port-forward", "-n", namespace, f"svc/{service_name}", f"{local_port}:{remote_port}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        
        # Wait a moment for port-forward to establish
        time.sleep(2)
        
        # Check if process is still running
        if _port_forward_process.poll() is not None:
            stderr = _port_forward_process.stderr.read().decode() if _port_forward_process.stderr else ""
            print(f"[!] Port-forward failed: {stderr}")
            _port_forward_process = None
            return False
        
        print(f"[✓] Port-forward established: localhost:{local_port} -> {service_name}:{remote_port}")
        print(f"[*] Port-forward will be cleaned up when script exits")
        return True
    except Exception as e:
        print(f"[!] Failed to set up port-forward: {e}")
        return False

def ensure_minio_connection():
    """Ensure MinIO is accessible, set up port-forward if needed"""
    endpoint = MINIO_ENDPOINT
    
    # Check if MinIO is already accessible
    if check_minio_available(endpoint):
        print(f"[✓] MinIO is accessible at {endpoint}")
        return True
    
    # Try to set up port-forward
    print(f"[!] MinIO is not accessible at {endpoint}")
    print("[*] Attempting to set up kubectl port-forward...")
    
    # Parse port from endpoint
    if ":" in endpoint:
        try:
            port = int(endpoint.split(":")[-1].split("/")[0])
        except:
            port = 9000
    else:
        port = 9000
    
    if setup_port_forward(local_port=port, remote_port=9000):
        # Update endpoint to use localhost
        if not endpoint.startswith("http://localhost") and not endpoint.startswith("http://127.0.0.1"):
            # Replace host with localhost
            if endpoint.startswith("http://"):
                endpoint = f"http://localhost:{port}"
            elif endpoint.startswith("https://"):
                endpoint = f"https://localhost:{port}"
            else:
                endpoint = f"http://localhost:{port}"
        
        # Wait a bit more and check again
        time.sleep(1)
        if check_minio_available(endpoint):
            print(f"[✓] MinIO is now accessible via port-forward at {endpoint}")
            return True
    
    print(f"[!] Could not establish connection to MinIO")
    print(f"[*] Please ensure MinIO is running and accessible, or set up port-forward manually:")
    print(f"    kubectl port-forward svc/<minio-service> {port}:9000")
    return False

def get_s3_client():
    return boto3.client('s3',
                        endpoint_url=MINIO_ENDPOINT,
                        aws_access_key_id=ACCESS_KEY,
                        aws_secret_access_key=SECRET_KEY,
                        config=Config(
                            signature_version='s3v4',
                            s3={'addressing_style': 'path'},
                            retries={'max_attempts': 5, 'mode': 'standard'}
                        ),
                        region_name='us-east-1')

def ensure_bucket(s3):
    try:
        s3.head_bucket(Bucket=BUCKET_NAME)
    except ClientError:
        print(f"[*] Creating bucket '{BUCKET_NAME}'...")
        s3.create_bucket(Bucket=BUCKET_NAME)

def upload_file(s3, local_path, s3_key):
    try:
        s3.upload_file(str(local_path), BUCKET_NAME, s3_key)
        print(f"  [OK] {s3_key}")
        return True
    except Exception as e:
        print(f"  [FAIL] {s3_key}: {e}")
        return False

def main():
    parser = argparse.ArgumentParser(description="Manage MinIO Data")
    parser.add_argument("action", choices=["upload", "upload-knowledge", "list", "check"], help="Action")
    parser.add_argument("--path", default="data/raw", help="Local path for raw data")
    parser.add_argument("--no-port-forward", action="store_true", help="Skip automatic port-forward setup")
    args = parser.parse_args()

    # Ensure MinIO connection (set up port-forward if needed)
    if not args.no_port_forward:
        if not ensure_minio_connection():
            print("[!] Exiting due to connection failure")
            sys.exit(1)
    else:
        print("[*] Skipping automatic port-forward setup")

    try:
        s3 = get_s3_client()
        ensure_bucket(s3)
        
        if args.action == "upload":
            print(f"[*] Syncing raw data to s3://{BUCKET_NAME}/raw...")
            local_dir = Path(args.path)
            for f in local_dir.rglob('*'):
                if f.is_file():
                    upload_file(s3, f, f"raw/{f.relative_to(local_dir).as_posix()}")
                    
        elif args.action == "upload-knowledge":
            print(f"[*] Uploading Knowledge Base to s3://{BUCKET_NAME}/knowledge...")
            # 同步 Agent C 需要的两个核心文件
            files = ["faiss_index.bin", "metadata.db"]
            for filename in files:
                local_p = Path(BASE_DIR) / "data" / filename
                if local_p.exists():
                    upload_file(s3, local_p, f"knowledge/{filename}")
                else:
                    print(f"  [SKIP] {filename} not found locally.")

        elif args.action in ["list", "check"]:
            response = s3.list_objects_v2(Bucket=BUCKET_NAME)
            if 'Contents' in response:
                print(f"{'Key':<40} {'Size':<10} {'Last Modified':<20} {'ETag/MD5'}")
                print("-" * 100)
                for obj in response['Contents']:
                    key = obj['Key']
                    size = f"{obj['Size']} B"
                    # Format timestamp
                    ts = obj['LastModified'].strftime("%Y-%m-%d %H:%M:%S")
                    # ETag usually contains the MD5 in quotes
                    etag = obj['ETag'].replace('"', '')
                    print(f"{key:<40} {size:<10} {ts:<20} {etag}")
            else:
                print("  (Empty bucket)")
                
    except Exception as e:
        print(f"[!] Operation failed: {e}")

if __name__ == "__main__":
    main()
