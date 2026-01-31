"""
Proxy configuration utilities for OpenAI clients.
Handles socks proxy conversion and filtering.
"""
import os
from typing import Any, Dict, Optional


def get_http_proxy() -> Optional[str]:
    """
    Get HTTP proxy from environment variables.
    Filters out socks proxies (not supported by httpx) and returns http/https proxies only.
    httpx only supports http:// and https:// proxies, not socks://
    """
    # Check common proxy environment variables
    proxy_vars = [
        "HTTP_PROXY", "http_proxy",
        "HTTPS_PROXY", "https_proxy",
        "ALL_PROXY", "all_proxy"
    ]

    # First, try to find HTTP/HTTPS proxies
    for var in ["HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy"]:
        proxy_url = os.getenv(var)
        if proxy_url and proxy_url.startswith(("http://", "https://")):
            return proxy_url

    # If no HTTP proxy found, check ALL_PROXY but filter socks
    all_proxy = os.getenv("ALL_PROXY") or os.getenv("all_proxy")
    if all_proxy:
        if all_proxy.startswith("socks://"):
            # FIClash typically also provides HTTP proxy on the same port
            # Try converting socks://127.0.0.1:7890 to http://127.0.0.1:7890
            if "127.0.0.1:7890" in all_proxy or "localhost:7890" in all_proxy:
                # FIClash HTTP proxy is usually on the same port
                return "http://127.0.0.1:7890"
            # Otherwise, return None (disable proxy for OpenAI)
            return None
        elif all_proxy.startswith(("http://", "https://")):
            return all_proxy

    return None

def get_openai_client_kwargs() -> Dict[str, Any]:
    """
    Get kwargs for OpenAI client initialization with proper proxy handling.
    Filters out socks proxies (not supported by httpx) and uses http/https proxies.
    """
    kwargs = {}

    # Get HTTP proxy (filtered from socks)
    http_proxy = get_http_proxy()
    if http_proxy:
        # Create httpx client with proxy support
        try:
            import httpx
            # httpx supports http:// and https:// proxies
            kwargs["http_client"] = httpx.Client(proxy=http_proxy, timeout=60.0)
        except Exception as e:
            # If proxy setup fails, continue without proxy
            # This allows the client to work even if proxy is misconfigured
            import warnings
            warnings.warn(f"Failed to configure proxy: {e}. Continuing without proxy.")

    return kwargs
