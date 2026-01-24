import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv(override=True)
import os

# Proxy Awareness: Bypass proxy for internal k3d domains (.test, .localhost)
if "NO_PROXY" in os.environ:
    if ".test" not in os.environ["NO_PROXY"]:
        os.environ["NO_PROXY"] += ",.test"
    if ".localhost" not in os.environ["NO_PROXY"]:
        os.environ["NO_PROXY"] += ",.localhost"
else:
    os.environ["NO_PROXY"] = "localhost,127.0.0.1,.localhost,.test"

# Force lowercase as well for some libraries
os.environ["no_proxy"] = os.environ["NO_PROXY"]

# Base directory of the project
# src/config.py -> src -> project_root
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Data directory paths (configurable via environment variables)
# In k3d: DATA_DIR=/app/data, locally: defaults to {BASE_DIR}/data
DATA_DIR = os.getenv("DATA_DIR", os.path.join(BASE_DIR, "data"))
MODEL_DIR = os.getenv("MODEL_DIR", os.path.join(DATA_DIR, "models"))
SPARK_JARS_DIR = os.getenv("SPARK_JARS_DIR", os.path.join(DATA_DIR, "spark-jars"))

# --- Configuration Validation ---
def validate_config():
    """Validate critical configuration settings and log warnings."""
    from utils.logger import logger as da_logger
    
    # Check if variables are available (defined below in this module)
    dk_key = os.getenv("DEEPSEEK_API_KEY")
    s3_ep = os.getenv("S3_ENDPOINT", "http://minio.test")
    
    if os.getenv("LOG_LEVEL") == "DEBUG":
        key_status = "SET" if dk_key else "MISSING"
        da_logger.debug(f"[Config] DEEPSEEK_API_KEY: {key_status}")
        da_logger.debug(f"[Config] S3_ENDPOINT: {s3_ep}")

    if not dk_key:
        da_logger.warning("DEEPSEEK_API_KEY is not set in .env. LLM-powered features will be disabled.")
    
    auth_key = os.getenv("AUTH_SECRET_KEY")
    if not auth_key or auth_key == "09d25e094faa6ca2556c818166b7a9563b93f7099f6f0f4caa6cf63b88e8d3e7":
        da_logger.warning("AUTH_SECRET_KEY is missing or using insecure default! Please set a unique key in .env.")

# Spark Configuration
SPARK_APP_NAME = "LLM_Data_Cleaning"
SPARK_MASTER = "local[*]"

# Data Paths (use DATA_DIR for consistency)
RAW_DATA_DIR = os.path.join(DATA_DIR, "raw")
PROCESSED_DATA_DIR = os.path.join(DATA_DIR, "processed")
# 核心修改：粗洗结果现在在 S3 上
WASHED_DATA_PATH = "s3a://lora-data/processed"
# 精洗结果（SFT）
SFT_OUTPUT_PATH = os.path.join(DATA_DIR, "sft_train.jsonl")
SFT_S3_PATH = f"s3://{os.getenv('S3_BUCKET', 'lora-data')}/sft/sft_train.jsonl"
ADAPTER_S3_PREFIX = "models/lora-adapter"
RAG_CHUNKS_PATH = os.path.join(DATA_DIR, "rag_chunks.jsonl")
FEEDBACK_DATA_DIR = os.path.join(DATA_DIR, "feedback")

# Data Sources
GIT_PR_PATH = os.path.join(RAW_DATA_DIR, "git_pr")
JIRA_PATH = os.path.join(RAW_DATA_DIR, "jira")
CONFLUENCE_PATH = os.path.join(RAW_DATA_DIR, "confluence")
DOCUMENTS_PATH = os.path.join(RAW_DATA_DIR, "documents")

# DeepSeek / LLM Configuration
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

# S3 / MinIO Configuration
S3_ENDPOINT = os.getenv("S3_ENDPOINT", "http://minio.test")
S3_ACCESS_KEY = os.getenv("AWS_ACCESS_KEY_ID", "minioadmin")
S3_SECRET_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "minioadmin")
S3_BUCKET = os.getenv("S3_BUCKET", "lora-data")

# Redis Configuration
REDIS_URL = os.getenv("REDIS_URL", "redis://data-alchemy.test:6379")

# Logging Configuration
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"

# Auth Configuration
AUTH_SECRET_KEY = os.getenv("AUTH_SECRET_KEY", "09d25e094faa6ca2556c818166b7a9563b93f7099f6f0f4caa6cf63b88e8d3e7")
AUTH_ALGORITHM = "HS256"
AUTH_TOKEN_EXPIRE_MINUTES = 60 * 24 # 24 hours
DISABLE_DEFAULT_ADMIN = os.getenv("DISABLE_DEFAULT_ADMIN", "false").lower() == "true"

LLM_CONFIG = {
    "api_key": DEEPSEEK_API_KEY,
    "base_url": DEEPSEEK_BASE_URL,
    "model": "deepseek-chat",
    "temperature": 0.7,
    "max_tokens": 1024,
}

# Regex Patterns for Sanitization
PATTERNS = {
    "ip_address": r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}",
    "email": r"[\w\.-]+@[\w\.-]+\.\w+",
    "api_key": r"(?i)(api[_-]?key|token|auth|password)[\s:=]+[a-zA-Z0-9_\-\.]{16,}",
}

# Replacement Tokens
TOKENS = {
    "ip_address": "[INTERNAL_IP]",
    "email": "[EMAIL]",
    "api_key": "[SECRET]",
}

# ============================================================
# Model Configuration Loader
# ============================================================

import yaml
import re
from typing import Dict, Any

def _expand_env_vars(value: Any) -> Any:
    """Recursively expand environment variables in config values."""
    if isinstance(value, str):
        # Match ${VAR_NAME} pattern
        pattern = r'\$\{([^}]+)\}'
        def replacer(match):
            var_name = match.group(1)
            val = os.getenv(var_name)
            if val is None:
                # Provide sensible defaults for known variables if not in .env
                if var_name == "DEEPSEEK_BASE_URL":
                    return "https://api.deepseek.com"
                return ""
            return val
        return re.sub(pattern, replacer, value)
    elif isinstance(value, dict):
        return {k: _expand_env_vars(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [_expand_env_vars(item) for item in value]
    return value


def load_model_config(config_path: str = None) -> Dict[str, Any]:
    """Load model configuration from YAML file."""
    if config_path is None:
        config_path = os.path.join(BASE_DIR, "models.yaml")
    
    if not os.path.exists(config_path):
        # We don't use logger here as this might be called during early initialization
        return {}
    
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        return _expand_env_vars(config)
    except Exception as e:
        print(f"[!] ERROR loading models.yaml: {e}")
        return {}


# Load model config at module level
MODEL_CONFIG = load_model_config()


def get_model_config(model_key: str) -> Dict[str, Any]:
    """Get configuration for a specific model (model_a, model_b, model_c, model_d)."""
    return MODEL_CONFIG.get(model_key, {})

