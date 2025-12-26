import os
import sys
# Add src to path
sys.path.append(os.path.join(os.getcwd(), "src"))

from config import MODEL_CONFIG

def mask_key(key):
    if not key: return "None"
    if len(key) <= 8: return "****"
    return f"{key[:4]}...{key[-4:]}"

print("--- Environment Variables ---")
print(f"DEEPSEEK_BASE_URL: {os.getenv('DEEPSEEK_BASE_URL')}")
print(f"DEEPSEEK_API_KEY: {mask_key(os.getenv('DEEPSEEK_API_KEY'))}")

print("\n--- Model Configuration Debug ---")
for model_name, config in MODEL_CONFIG.items():
    print(f"\n[{model_name}]")
    for k, v in config.items():
        if "api_key" in k:
            print(f"  {k}: {mask_key(v)}")
        else:
            print(f"  {k}: {v}")
