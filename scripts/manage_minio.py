import os
import sys
import argparse
import boto3
from botocore.client import Config
import subprocess
import time

# Configuration
MINIO_ENDPOINT = "http://localhost:9000"
ACCESS_KEY = "minioadmin"
SECRET_KEY = "minioadmin"
BUCKET_NAME = "lora-data"

def get_s3_client():
    return boto3.client('s3',
                        endpoint_url=MINIO_ENDPOINT,
                        aws_access_key_id=ACCESS_KEY,
                        aws_secret_access_key=SECRET_KEY,
                        config=Config(signature_version='s3v4'),
                        region_name='us-east-1')

def ensure_bucket(s3):
    try:
        s3.head_bucket(Bucket=BUCKET_NAME)
        print(f"[*] Bucket '{BUCKET_NAME}' exists.")
    except:
        print(f"[*] Creating bucket '{BUCKET_NAME}'...")
        s3.create_bucket(Bucket=BUCKET_NAME)

def upload_data(s3, local_path, prefix="raw"):
    print(f"[*] Uploading {local_path} to s3://{BUCKET_NAME}/{prefix}...")
    for root, dirs, files in os.walk(local_path):
        for file in files:
            local_file = os.path.join(root, file)
            # Calculate relative path for S3 key
            rel_path = os.path.relpath(local_file, local_path)
            s3_key = os.path.join(prefix, rel_path).replace("\\", "/")
            
            print(f"  - {local_file} -> {s3_key}")
            s3.upload_file(local_file, BUCKET_NAME, s3_key)
    print("[SUCCESS] Upload complete.")

def list_data(s3):
    print(f"[*] Listing contents of s3://{BUCKET_NAME}...")
    try:
        response = s3.list_objects_v2(Bucket=BUCKET_NAME)
        if 'Contents' in response:
            for obj in response['Contents']:
                print(f"  - {obj['Key']} ({obj['Size']} bytes)")
        else:
            print("  (Empty bucket)")
    except Exception as e:
        print(f"[!] Error listing bucket: {e}")

def port_forward():
    print("[*] Starting port-forward to MinIO (Ctrl+C to stop)...")
    cmd = ["kubectl", "port-forward", "svc/minio", "9000:9000", "9001:9001"]
    try:
        # Run in background? For script simplicity, we might just ask user to run it separately
        # or run it and wait a bit.
        # Let's check if port is open first.
        import socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        result = sock.connect_ex(('127.0.0.1', 9000))
        if result == 0:
            print("[*] Port 9000 is already open, assuming port-forward is running.")
            return True
        else:
            print("[!] Port 9000 not open. Please run 'kubectl port-forward svc/minio 9000:9000' in another terminal.")
            return False
    except:
        return False

def main():
    parser = argparse.ArgumentParser(description="Manage MinIO Data")
    parser.add_argument("action", choices=["upload", "list", "check"], help="Action to perform")
    parser.add_argument("--path", default="data/raw", help="Local path to upload")
    parser.add_argument("--prefix", help="S3 prefix (default: inferred from path)")
    args = parser.parse_args()

    if not port_forward():
        return

    try:
        s3 = get_s3_client()
        ensure_bucket(s3)
        
        if args.action == "upload":
            # Infer prefix from path if not specified
            if args.prefix:
                prefix = args.prefix
            elif "feedback" in args.path:
                prefix = "feedback"
            else:
                # Default to "raw" for data/raw
                prefix = "raw"
            
            upload_data(s3, args.path, prefix=prefix)
        elif args.action == "list":
            list_data(s3)
        elif args.action == "check":
            list_data(s3)
            
    except Exception as e:
        print(f"[!] Error: {e}")
        print("Ensure 'pip install boto3' is run and MinIO is accessible.")

if __name__ == "__main__":
    main()
