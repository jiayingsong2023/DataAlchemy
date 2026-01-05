import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Base directory of the project
# src/config.py -> src -> project_root
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Spark Configuration
SPARK_APP_NAME = "LLM_Data_Cleaning"
SPARK_MASTER = "local[*]"

# Data Paths
RAW_DATA_DIR = os.path.join(BASE_DIR, "data", "raw")
PROCESSED_DATA_DIR = os.path.join(BASE_DIR, "data", "processed")
WASHED_DATA_PATH = os.path.join(BASE_DIR, "data", "cleaned_corpus.jsonl")
RAG_CHUNKS_PATH = os.path.join(BASE_DIR, "data", "rag_chunks.jsonl")
SFT_OUTPUT_PATH = os.path.join(BASE_DIR, "data", "sft_train.jsonl")
FEEDBACK_DATA_DIR = os.path.join(BASE_DIR, "data", "feedback")

# Data Sources
GIT_PR_PATH = os.path.join(RAW_DATA_DIR, "git_pr")
JIRA_PATH = os.path.join(RAW_DATA_DIR, "jira")
CONFLUENCE_PATH = os.path.join(RAW_DATA_DIR, "confluence")
DOCUMENTS_PATH = os.path.join(RAW_DATA_DIR, "documents")

# DeepSeek / LLM Configuration
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

# S3 / MinIO Configuration
S3_ENDPOINT = os.getenv("S3_ENDPOINT", "http://localhost:9000")
S3_ACCESS_KEY = os.getenv("AWS_ACCESS_KEY_ID", "minioadmin")
S3_SECRET_KEY = os.getenv("AWS_SECRET_ACCESS_KEY", "minioadmin")
S3_BUCKET = os.getenv("S3_BUCKET", "lora-data")

# Redis Configuration
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379")

if not DEEPSEEK_API_KEY:
    print("\n[!] WARNING: DEEPSEEK_API_KEY is not set in your .env file.")
    print("    LLM-powered features (Synthesis, Agent D) will not work properly.\n")

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
        print(f"[!] WARNING: models.yaml not found at {config_path}")
        print("    Using default hardcoded values.")
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

