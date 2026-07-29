import re
try:
    from .presidio_sanitizer import PresidioSanitizer
    presidio_engine = PresidioSanitizer()
except ImportError:
    presidio_engine = None

try:
    from config import PATTERNS, TOKENS
except ImportError:
    # Fallback for standalone execution in WSL
    PATTERNS = {}
    TOKENS = {}

def sanitize_text(text):
    """Remove sensitive information using regex patterns from config."""
    if not text:
        return ""

    for key, pattern in PATTERNS.items():
        replacement = TOKENS.get(key, "[REDACTED]")
        text = re.sub(pattern, replacement, text)

    return text

def advanced_sanitize(text):
    """
    Two-pass sanitization:
    1. Fast Regex-based (IPs, Emails, API Keys)
    2. Deep NER-based (Names, Addresses via Presidio)
    """
    if not text:
        return ""
        
    # Pass 1: Regex
    text = sanitize_text(text)
    
    # Pass 2: Presidio (if available and initialized)
    if presidio_engine and presidio_engine.is_active:
        text = presidio_engine.sanitize(text)
        
    return text


def sanitize_for_cloud(text):
    """Fail closed when the required PII recognizer is unavailable."""
    if not presidio_engine or not presidio_engine.is_active:
        raise RuntimeError("Presidio must be available before data can be sent to a cloud model")
    return advanced_sanitize(text)

# Register UDFs if pyspark is available
try:
    from pyspark.sql.functions import udf
    from pyspark.sql.types import StringType
    sanitize_udf = udf(sanitize_text, StringType())
    advanced_sanitize_udf = udf(advanced_sanitize, StringType())
except ImportError:
    pass
