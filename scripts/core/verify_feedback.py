import sys
from pathlib import Path

# Add src to path
sys.path.append(str(Path(__file__).resolve().parents[2] / "src"))

from feedback import save_feedback
from utils.s3_utils import S3Utils


def test_feedback_mechanism():
    print("--- Testing Feedback Mechanism ---")

    store = S3Utils()

    # 2. Save a "good" feedback
    print("\n[1] Saving 'good' feedback...")
    query = "What is DataAlchemy?"
    answer = "DataAlchemy is a multi-agent RAG system."
    good_id = save_feedback(store, query, answer, "good")

    # 3. Save a "bad" feedback
    print("\n[2] Saving 'bad' feedback...")
    bad_query = "Who is the president of Mars?"
    bad_answer = "Elon Musk."
    bad_id = save_feedback(store, bad_query, bad_answer, "bad")
    print(f"\n[SUCCESS] Wrote immutable feedback sources: {good_id}, {bad_id}")


if __name__ == "__main__":
    test_feedback_mechanism()
