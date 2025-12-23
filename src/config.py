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
FINAL_OUTPUT_PATH = os.path.join(BASE_DIR, "data", "train.jsonl")
RAG_CHUNKS_PATH = os.path.join(BASE_DIR, "data", "rag_chunks.jsonl")
SFT_OUTPUT_PATH = os.path.join(BASE_DIR, "data", "sft_train.jsonl")

# Data Sources
GIT_PR_PATH = os.path.join(RAW_DATA_DIR, "git_pr")
JIRA_PATH = os.path.join(RAW_DATA_DIR, "jira")
CONFLUENCE_PATH = os.path.join(RAW_DATA_DIR, "confluence")
DOCUMENTS_PATH = os.path.join(RAW_DATA_DIR, "documents")

# DeepSeek / LLM Configuration
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

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

