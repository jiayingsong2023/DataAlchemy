import boto3
import json
import os
from botocore.client import Config
from typing import List, Dict, Any, Optional
from config import S3_ENDPOINT, S3_ACCESS_KEY, S3_SECRET_KEY, S3_BUCKET
from utils.logger import logger

class S3Utils:
    """Unified S3 utility class for DataAlchemy."""
    
    def __init__(self, bucket: str = None):
        self.endpoint = S3_ENDPOINT
        self.access_key = S3_ACCESS_KEY
        self.secret_key = S3_SECRET_KEY
        self.bucket = bucket or S3_BUCKET
        self.client = self._get_client()

    def _get_client(self):
        return boto3.client(
            's3',
            endpoint_url=self.endpoint,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            config=Config(
                signature_version='s3v4',
                s3={'addressing_style': 'path'},
                retries={'max_attempts': 10, 'mode': 'standard'}
            ),
            region_name='us-east-1'
        )

    def ensure_bucket(self):
        """Ensure the target bucket exists."""
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except Exception:
            logger.info(f"Creating missing bucket: {self.bucket}")
            self.client.create_bucket(Bucket=self.bucket)

    def upload_file(self, local_path: str, s3_key: str) -> bool:
        """Upload a local file to S3."""
        try:
            self.client.upload_file(local_path, self.bucket, s3_key)
            return True
        except Exception as e:
            logger.error(f"S3 upload failed for {s3_key}: {e}")
            return False

    def download_file(self, s3_key: str, local_path: str) -> bool:
        """Download a file from S3 to local."""
        try:
            os.makedirs(os.path.dirname(local_path), exist_ok=True)
            self.client.download_file(self.bucket, s3_key, local_path)
            return True
        except Exception as e:
            logger.error(f"S3 download failed for {s3_key}: {e}")
            return False

    def list_objects(self, prefix: str) -> List[Dict[str, Any]]:
        """List objects with a specific prefix."""
        try:
            response = self.client.list_objects_v2(Bucket=self.bucket, Prefix=prefix)
            return response.get('Contents', [])
        except Exception as e:
            logger.error(f"S3 list failed for prefix {prefix}: {e}")
            return []

    def get_object_body(self, s3_key: str) -> Optional[bytes]:
        """Get the body of an S3 object."""
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=s3_key)
            return response['Body'].read()
        except Exception as e:
            logger.error(f"S3 get_object failed for {s3_key}: {e}")
            return None

    def put_object(self, s3_key: str, body: Any, content_type: str = "application/octet-stream") -> bool:
        """Put an object into S3."""
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=s3_key,
                Body=body,
                ContentType=content_type
            )
            return True
        except Exception as e:
            logger.error(f"S3 put_object failed for {s3_key}: {e}")
            return False

    def exists(self, s3_key: str) -> bool:
        """Check if an object exists in S3."""
        try:
            self.client.head_object(Bucket=self.bucket, Key=s3_key)
            return True
        except Exception:
            return False

    def upload_directory(self, local_dir: str, s3_prefix: str) -> bool:
        """Upload an entire directory to S3."""
        try:
            for root, _, files in os.walk(local_dir):
                for file in files:
                    local_path = os.path.join(root, file)
                    # Get relative path to maintain directory structure
                    rel_path = os.path.relpath(local_path, local_dir)
                    s3_key = os.path.join(s3_prefix, rel_path).replace("\\", "/")
                    self.upload_file(local_path, s3_key)
            return True
        except Exception as e:
            logger.error(f"S3 directory upload failed for {s3_prefix}: {e}")
            return False

    def download_directory(self, s3_prefix: str, local_dir: str) -> bool:
        """Download an entire directory from S3."""
        try:
            objects = self.list_objects(s3_prefix)
            if not objects:
                logger.warning(f"No objects found in S3 with prefix: {s3_prefix}")
                return False
                
            for obj in objects:
                s3_key = obj['Key']
                # Get relative path from prefix
                rel_path = os.path.relpath(s3_key, s3_prefix)
                local_path = os.path.join(local_dir, rel_path)
                self.download_file(s3_key, local_path)
            return True
        except Exception as e:
            logger.error(f"S3 directory download failed for {s3_prefix}: {e}")
            return False
