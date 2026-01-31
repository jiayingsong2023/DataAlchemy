import polars as pl

from config import S3_ACCESS_KEY, S3_ENDPOINT, S3_SECRET_KEY


def get_storage_options():
    """Get storage options for Polars to access MinIO/S3."""
    return {
        "endpoint_url": S3_ENDPOINT,
        "access_key_id": S3_ACCESS_KEY,
        "secret_access_key": S3_SECRET_KEY,
        "region": "us-east-1",
        "use_path_style": "true",
        "allow_http": "true" if S3_ENDPOINT.startswith("http://") else "false"
    }

def scan_parquet_optimized(path: str):
    """Scan parquet with correct storage options and S3 path handling."""
    if path.startswith("s3://") or path.startswith("s3a://"):
        # Polars internal cloud reader prefers s3://
        s3_path = path.replace("s3a://", "s3://")

        # Ensure directory path ends with / for Spark output
        if not s3_path.endswith("/"):
            s3_path += "/"

        return pl.scan_parquet(s3_path, storage_options=get_storage_options())
    return pl.scan_parquet(path)
