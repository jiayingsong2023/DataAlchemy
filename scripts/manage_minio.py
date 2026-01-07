import os
import sys
import argparse
import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
import time
import socket
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Configuration
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MINIO_ENDPOINT = os.getenv("S3_ENDPOINT", "http://minio.dataalchemy.local")
ACCESS_KEY = os.getenv("AWS_ACCESS_KEY_ID", "minioadmin")
SECRET_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "minioadmin")
BUCKET_NAME = os.getenv("S3_BUCKET", "lora-data")

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
    args = parser.parse_args()

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
