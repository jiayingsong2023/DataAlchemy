# ruff: noqa: E402

import argparse
import asyncio
import random
import statistics
import sys
import time
from pathlib import Path

# Add the repository and installed-package layouts for direct script execution.
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]

from inference.adapter_runtime import AdapterRuntime
from rag.answering import GroundedAnswering, answer_with_citations
from rag.retriever import Retriever
from rag.vector_store import VectorStore

# Sample prompts for benchmarking
PROMPTS = [
    "What is the main goal of this project?",
    "How do I deploy the system to Kubernetes?",
    "Tell me about the LoRA fine-tuning process.",
    "What is Agent D responsible for?",
    "How does the data cleaning pipeline work?",
    "What are the prerequisites for running this on AMD GPU?",
    "Explain the difference between Agent B and Agent C.",
    "How do I use the WebUI?",
    "What is the role of MinIO in this architecture?",
    "How can I clear the inference cache?",
]


async def simulate_user(
    user_id: int,
    retriever: Retriever,
    adapter_runtime: AdapterRuntime,
    answering: GroundedAnswering,
    num_requests: int,
    delay_range: tuple,
):
    """Simulate a single user making multiple requests"""
    latencies = []

    for i in range(num_requests):
        prompt = random.choice(PROMPTS)

        # Randomly decide to repeat a prompt to test cache
        if random.random() < 0.3:  # 30% chance to repeat
            prompt = PROMPTS[0]

        start_time = time.time()
        try:
            print(f"[User {user_id}] Request {i + 1}: {prompt[:30]}...")
            identity = {
                "tenant_id": "default",
                "username": f"benchmark-{user_id}",
                "role": "user",
            }
            context = await asyncio.to_thread(retriever.retrieve, prompt, identity, 3)
            await answer_with_citations(prompt, identity, context, adapter_runtime, answering)
            latency = time.time() - start_time
            latencies.append(latency)
            print(f"[User {user_id}] Request {i + 1} done in {latency:.2f}s")
        except Exception as e:
            print(f"[User {user_id}] Request {i + 1} failed: {e}")

        # Wait before next request
        await asyncio.sleep(random.uniform(*delay_range))

    return latencies


async def run_benchmark(num_users: int, requests_per_user: int, delay_range: tuple):
    print(f"\n{'=' * 60}")
    print(f"Starting Benchmark: {num_users} users, {requests_per_user} requests/user")
    print(f"{'=' * 60}\n")

    vector_store = VectorStore()
    retriever = Retriever(vector_store)
    adapter_runtime = AdapterRuntime()
    answering = GroundedAnswering()

    start_time = time.time()

    # Run all users concurrently
    tasks = [
        simulate_user(
            i,
            retriever,
            adapter_runtime,
            answering,
            requests_per_user,
            delay_range,
        )
        for i in range(num_users)
    ]

    try:
        all_latencies_nested = await asyncio.gather(*tasks)
    finally:
        if adapter_runtime.batch_engine is not None:
            await adapter_runtime.batch_engine.shutdown()
        adapter_runtime.model_manager.clear_cache()
    all_latencies = [
        latency for user_latencies in all_latencies_nested for latency in user_latencies
    ]

    total_time = time.time() - start_time
    total_requests = len(all_latencies)

    if not all_latencies:
        print("No successful requests.")
        return

    # Calculate statistics
    avg_latency = statistics.mean(all_latencies)
    p50 = statistics.median(all_latencies)
    p95 = (
        statistics.quantiles(all_latencies, n=20)[18]
        if len(all_latencies) >= 20
        else max(all_latencies)
    )
    throughput = total_requests / total_time

    print(f"\n{'=' * 60}")
    print("Benchmark Results:")
    print(f"{'=' * 60}")
    print(f"Total Requests:  {total_requests}")
    print(f"Total Time:      {total_time:.2f}s")
    print(f"Throughput:      {throughput:.2f} req/s")
    print(f"Avg Latency:     {avg_latency:.2f}s")
    print(f"P50 Latency:     {p50:.2f}s")
    print(f"P95 Latency:     {p95:.2f}s")
    print(f"{'=' * 60}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Inference Benchmark Tool")
    parser.add_argument("--users", type=int, default=3, help="Number of concurrent users")
    parser.add_argument("--reqs", type=int, default=5, help="Requests per user")
    parser.add_argument("--min-delay", type=float, default=0.5, help="Min delay between requests")
    parser.add_argument("--max-delay", type=float, default=2.0, help="Max delay between requests")

    args = parser.parse_args()

    asyncio.run(run_benchmark(args.users, args.reqs, (args.min_delay, args.max_delay)))
