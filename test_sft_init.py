import os
import sys
# Add src to path
sys.path.append(os.path.join(os.getcwd(), "src"))

try:
    from etl.sft_generator import SFTGenerator
    print("Attempting to initialize SFTGenerator...")
    generator = SFTGenerator()
    print("SFTGenerator initialized successfully.")
    print(f"Model: {generator.model}")
    print(f"Base URL: {generator.base_url}")
    print(f"API Key present: {generator.api_key is not None}")
except Exception as e:
    print(f"Failed to initialize SFTGenerator: {e}")
