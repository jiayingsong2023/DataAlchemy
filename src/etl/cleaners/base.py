import re
from bs4 import BeautifulSoup

def clean_html(text):
    """Remove HTML tags using BeautifulSoup."""
    if not text:
        return ""
    try:
        soup = BeautifulSoup(text, "html.parser")
        return soup.get_text(separator=" ").strip()
    except Exception:
        # Fallback to regex if BS4 fails
        return re.sub(r'<[^>]+>', '', text).strip()

def normalize_whitespace(text):
    """Reduce multiple spaces and newlines."""
    if not text:
        return ""
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def remove_urls(text):
    """Remove common URLs."""
    if not text:
        return ""
    return re.sub(r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+', '[URL]', text)

