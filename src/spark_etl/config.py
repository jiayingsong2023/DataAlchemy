import os

# Spark Configuration
SPARK_APP_NAME = "LLM_Data_Cleaning"
SPARK_MASTER = "local[*]"

# Data Paths
# config.py is now in src/spark_etl/, so BASE_DIR is 3 levels up
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RAW_DATA_DIR = os.path.join(BASE_DIR, "data", "raw")
PROCESSED_DATA_DIR = os.path.join(BASE_DIR, "data", "processed")
FINAL_OUTPUT_PATH = os.path.join(BASE_DIR, "data", "train.jsonl")
RAG_CHUNKS_PATH = os.path.join(BASE_DIR, "data", "rag_chunks.jsonl")

# Data Sources
GIT_PR_PATH = os.path.join(RAW_DATA_DIR, "git_pr")
JIRA_PATH = os.path.join(RAW_DATA_DIR, "jira")
CONFLUENCE_PATH = os.path.join(RAW_DATA_DIR, "confluence")
DOCUMENTS_PATH = os.path.join(RAW_DATA_DIR, "documents")
SFT_OUTPUT_PATH = os.path.join(BASE_DIR, "data", "sft_train.jsonl")

# DeepSeek / LLM Configuration
LLM_CONFIG = {
    "api_key": "YOUR_DEEPSEEK_API_KEY", # Replace with actual key
    "base_url": "https://api.deepseek.com",
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

