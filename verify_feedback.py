import os
import sys
import json
import shutil

# Add src to path
sys.path.append(os.path.join(os.getcwd(), "src"))

from agents.coordinator import Coordinator
from config import FEEDBACK_DATA_DIR, WASHED_DATA_PATH

def test_feedback_mechanism():
    print("--- Testing Feedback Mechanism ---")
    
    # 1. Initialize Coordinator
    coordinator = Coordinator(mode="python")
    
    # 2. Save a "good" feedback
    print("\n[1] Saving 'good' feedback...")
    query = "What is DataAlchemy?"
    answer = "DataAlchemy is a multi-agent RAG system."
    feedback_id = coordinator.save_feedback(query, answer, "good")
    
    # 3. Save a "bad" feedback
    print("\n[2] Saving 'bad' feedback...")
    bad_query = "Who is the president of Mars?"
    bad_answer = "Elon Musk."
    coordinator.save_feedback(bad_query, bad_answer, "bad")
    
    # 4. Verify files exist
    files = os.listdir(FEEDBACK_DATA_DIR)
    print(f"\n[3] Verified: {len(files)} feedback files created in {FEEDBACK_DATA_DIR}")
    
    # 5. Run Ingestion (Wash only) to see if it picks up feedback
    print("\n[4] Running Agent A cleaning...")
    coordinator.run_ingestion_pipeline(stage="wash")
    
    # 6. Check if "User Feedback" is in the washed corpus
    found_good = False
    found_bad = False
    
    with open(WASHED_DATA_PATH, "r", encoding="utf-8") as f:
        for line in f:
            if "### User Feedback" in line:
                if "What is DataAlchemy?" in line:
                    found_good = True
                if "Who is the president of Mars?" in line:
                    found_bad = True
                    
    if found_good:
        print("\n[SUCCESS] 'Good' feedback found in washed corpus.")
    else:
        print("\n[FAILURE] 'Good' feedback NOT found in washed corpus.")
        
    if not found_bad:
        print("[SUCCESS] 'Bad' feedback correctly excluded from washed corpus.")
    else:
        print("[FAILURE] 'Bad' feedback was INCORRECTLY included in washed corpus.")

if __name__ == "__main__":
    # Clean up existing feedback for clean test
    if os.path.exists(FEEDBACK_DATA_DIR):
        shutil.rmtree(FEEDBACK_DATA_DIR)
        
    try:
        test_feedback_mechanism()
    except Exception as e:
        print(f"\n[ERROR] Test failed: {e}")
