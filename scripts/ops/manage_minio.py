import os
import sys
import argparse
import boto3
import requests
from requests.adapters import HTTPAdapter
from urllib3.connectionpool import HTTPConnectionPool
from botocore.client import Config
from botocore.exceptions import ClientError
import time
import socket
import subprocess
import json
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Proxy Awareness: Bypass proxy for .localhost to avoid VPN interference
if "NO_PROXY" in os.environ:
    if ".localhost" not in os.environ["NO_PROXY"]:
        os.environ["NO_PROXY"] += ",.localhost"
else:
    os.environ["NO_PROXY"] = "localhost,127.0.0.1,.localhost"

# Force lowercase as well for some libraries
os.environ["no_proxy"] = os.environ["NO_PROXY"]

# Configuration
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# MinIO Endpoint Configuration
MINIO_ENDPOINT = os.getenv("S3_ENDPOINT", "http://minio.localhost")
ACCESS_KEY = os.getenv("AWS_ACCESS_KEY_ID", "admin")
SECRET_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "minioadmin")
BUCKET_NAME = os.getenv("S3_BUCKET", "lora-data")

# Global variables for smart discovery
_K3D_IP = None
_ORIG_GETADDRINFO = socket.getaddrinfo

def get_k3d_lb_ip():
    """Try to find the K3d LoadBalancer IP using kubectl or docker"""
    global _K3D_IP
    if _K3D_IP: return _K3D_IP
    
    try:
        # Method 1: Get External IP of traefik service
        result = subprocess.run(
            ["kubectl", "get", "svc", "-n", "kube-system", "traefik", "-o", "json"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            ingresses = data.get("status", {}).get("loadBalancer", {}).get("ingress", [])
            for ing in ingresses:
                if "ip" in ing:
                    _K3D_IP = ing["ip"]
                    return _K3D_IP

        # Method 2: Fallback to docker container IP for the LB
        result = subprocess.run(
            ["docker", "inspect", "k3d-k3s-default-serverlb", "--format", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            ip = result.stdout.strip()
            if ip: 
                _K3D_IP = ip
                return ip
            
    except Exception:
        pass
    return None

def patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    """DNS Monkeypatch: Force minio.localhost to resolve to K3d IP"""
    if host == "minio.localhost" or host == "data-alchemy.localhost":
        ip = get_k3d_lb_ip()
        if ip:
            return _ORIG_GETADDRINFO(ip, port, family, type, proto, flags)
    return _ORIG_GETADDRINFO(host, port, family, type, proto, flags)

def check_minio_available(endpoint):
    """Check if MinIO is accessible at the given endpoint"""
    try:
        host_port = endpoint.replace("http://", "").replace("https://", "").split("/")[0]
        if ":" in host_port:
            host, port = host_port.split(":", 1)
            port = int(port)
        else:
            host = host_port
            port = 80
        
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(1)
        # We use the original getaddrinfo for checking standard connectivity first
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except Exception:
        return False

def ensure_minio_connection():
    """Verify MinIO is accessible, applying DNS monkeypatch if needed"""
    endpoint = MINIO_ENDPOINT
    print(f"[*] Checking MinIO connection at {endpoint}...")
    
    # 1. Check if it works normally (via /etc/hosts or standard DNS)
    if check_minio_available(endpoint):
        print(f"[✓] MinIO is accessible at {endpoint}")
        return True
    
    # 2. Smart Fallback: If .localhost is unreachable, try the monkeypatch
    if "localhost" in endpoint:
        ip = get_k3d_lb_ip()
        if ip:
            print(f"[!] {endpoint} unreachable via standard DNS. Testing via K3d IP {ip}...")
            # Test if port 80 is open on the IP
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            res = sock.connect_ex((ip, 80))
            sock.close()
            if res == 0:
                print(f"[✓] K3d LoadBalancer reachable at {ip}:80.")
                print(f"[*] Applying local DNS monkeypatch: {endpoint} -> {ip}")
                socket.getaddrinfo = patched_getaddrinfo
                return True
    
    print(f"[!] MinIO is not accessible at {endpoint}")
    print(f"[*] Troubleshooting Tips:")
    print(f"    1. Run: kubectl get ingress -n data-alchemy")
    print(f"    2. Try adding to /etc/hosts: {get_k3d_lb_ip() or '172.x.x.x'} minio.localhost")
    return False

def get_s3_client():
    # Since we've patched socket.getaddrinfo, Boto3 will resolve minio.localhost 
    # to the K3d IP automatically, keeping headers and signatures intact.
    return boto3.client('s3',
                        endpoint_url=MINIO_ENDPOINT,
                        aws_access_key_id=ACCESS_KEY,
                        aws_secret_access_key=SECRET_KEY,
                        config=Config(
                            signature_version='s3v4',
                            s3={'addressing_style': 'path'},
                            retries={'max_attempts': 2, 'mode': 'standard'},
                            connect_timeout=5,
                            read_timeout=5
                        ),
                        region_name='us-east-1')

def ensure_bucket(s3):
    try:
        s3.head_bucket(Bucket=BUCKET_NAME)
    except Exception as e:
        if "404" in str(e):
            print(f"[*] Creating bucket '{BUCKET_NAME}'...")
            s3.create_bucket(Bucket=BUCKET_NAME)
        else:
            raise e

def upload_file(s3, local_path, s3_key):
    try:
        s3.upload_file(str(local_path), BUCKET_NAME, s3_key)
        print(f"  [OK] {s3_key}")
        return True
    except Exception as e:
        print(f"  [FAIL] {s3_key}: {e}")
        return False

def main():
    parser = argparse.ArgumentParser(description="Manage MinIO Data for K3d/K8s")
    parser.add_argument("action", choices=["upload", "upload-knowledge", "list", "check"], help="Action")
    parser.add_argument("file", nargs="?", help="File to upload (for single file upload)")
    parser.add_argument("--path", default="data/raw", help="Local path for raw data directory")
    args = parser.parse_args()

    # Ensure MinIO connection
    if not ensure_minio_connection():
        print("[!] Exiting due to connection failure")
        sys.exit(1)

    try:
        s3 = get_s3_client()
        
        # Test connection with a small timeout to avoid long hangs
        print("[*] Initializing S3 client...")
        ensure_bucket(s3)
        
        if args.action == "upload":
            # Handle single file upload
            if args.file:
                file_path = Path(args.file)
                if not file_path.exists():
                    print(f"[!] File not found: {args.file}")
                    sys.exit(1)
                if not file_path.is_file():
                    print(f"[!] Not a file: {args.file}")
                    sys.exit(1)
                
                print(f"[*] Uploading {file_path.name} to s3://{BUCKET_NAME}/raw/...")
                s3_key = f"raw/{file_path.name}"
                if upload_file(s3, file_path, s3_key):
                    print(f"[✓] Upload complete: s3://{BUCKET_NAME}/{s3_key}")
                else:
                    print(f"[!] Upload failed")
                    sys.exit(1)
            else:
                # Handle directory upload
                print(f"[*] Syncing raw data to s3://{BUCKET_NAME}/raw...")
                local_dir = Path(args.path)
                if not local_dir.exists():
                    print(f"[!] Directory not found: {args.path}")
                    sys.exit(1)
                
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
