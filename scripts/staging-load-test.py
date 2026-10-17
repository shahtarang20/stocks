#!/usr/bin/env python3
"""ComputeCoin Staging & Production Load Test

Measures endpoint latency, throughput (req/sec), and HTTP status codes under concurrency.

Usage:
    python scripts/staging-load-test.py [API_URL] [--concurrency 20] [--requests 200]
"""

import argparse
import asyncio
import os
import sys
import time
from collections import Counter

try:
    import httpx
except ImportError:
    venv_py = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "apps", "api", ".venv", "bin", "python"))
    if os.path.exists(venv_py) and sys.executable != venv_py:
        os.execv(venv_py, [venv_py] + sys.argv)
    raise


async def worker(
    client: httpx.AsyncClient,
    queue: asyncio.Queue,
    results: list[tuple[int, float]],
) -> None:
    while not queue.empty():
        url = await queue.get()
        start = time.perf_counter()
        try:
            resp = await client.get(url)
            elapsed = (time.perf_counter() - start) * 1000.0
            results.append((resp.status_code, elapsed))
        except Exception:
            elapsed = (time.perf_counter() - start) * 1000.0
            results.append((0, elapsed))
        finally:
            queue.task_done()


async def run_load_test(
    base_url: str,
    concurrency: int = 20,
    total_requests: int = 200,
) -> None:
    endpoints = [
        "/health",
        "/",
        "/v1/marketplace/machines",
        "/v1/settlement/contracts",
    ]

    print("========================================================")
    print(f" 🚀 ComputeCoin Load Test: {base_url}")
    print(f" Concurrency:    {concurrency} workers")
    print(f" Total Requests: {total_requests}")
    print("========================================================\n")

    queue: asyncio.Queue[str] = asyncio.Queue()
    for i in range(total_requests):
        endpoint = endpoints[i % len(endpoints)]
        queue.put_nowait(f"{base_url.rstrip('/')}{endpoint}")

    results: list[tuple[int, float]] = []

    start_total = time.perf_counter()
    async with httpx.AsyncClient(timeout=10.0, limits=httpx.Limits(max_connections=concurrency * 2)) as client:
        tasks = [asyncio.create_task(worker(client, queue, results)) for _ in range(concurrency)]
        await queue.join()
        for t in tasks:
            t.cancel()

    total_time = time.perf_counter() - start_total
    latencies = [r[1] for r in results]
    status_codes = Counter(r[0] for r in results)

    latencies.sort()
    count = len(latencies)
    if count == 0:
        print("❌ No requests completed.")
        sys.exit(1)

    p50 = latencies[int(count * 0.50)]
    p90 = latencies[int(count * 0.90)]
    p95 = latencies[int(count * 0.95)]
    p99 = latencies[int(count * 0.99)]
    avg_lat = sum(latencies) / count
    rps = count / total_time

    print("📊 Load Test Results:")
    print("--------------------------------------------------------")
    print(f"  Total Completed:   {count} requests")
    print(f"  Duration:          {total_time:.2f}s")
    print(f"  Throughput:        {rps:.1f} req/sec")
    print(f"  Average Latency:   {avg_lat:.2f}ms")
    print(f"  Min Latency:       {latencies[0]:.2f}ms")
    print(f"  p50 (Median):      {p50:.2f}ms")
    print(f"  p90:               {p90:.2f}ms")
    print(f"  p95:               {p95:.2f}ms")
    print(f"  p99:               {p99:.2f}ms")
    print(f"  Max Latency:       {latencies[-1]:.2f}ms")
    print("--------------------------------------------------------")
    print("Status Code Breakdown:")
    for code, freq in sorted(status_codes.items()):
        status_label = f"HTTP {code}" if code > 0 else "Connection Error"
        pct = (freq / count) * 100
        print(f"  {status_label:<18}: {freq:>4} ({pct:.1f}%)")
    print("========================================================")

    errors = status_codes.get(0, 0) + sum(freq for code, freq in status_codes.items() if code >= 500)
    if errors > 0:
        print(f"❌ Load test FAILED with {errors} error responses.")
        sys.exit(1)
    else:
        print("🎉 Load test PASSED with 0% error rate!")


def main() -> None:
    parser = argparse.ArgumentParser(description="ComputeCoin Staging Load Test")
    parser.add_argument("url", nargs="?", default="http://localhost:8000", help="Base URL of the API")
    parser.add_argument("--concurrency", type=int, default=20, help="Number of concurrent connections")
    parser.add_argument("--requests", type=int, default=200, help="Total number of requests")
    args = parser.parse_args()

    asyncio.run(run_load_test(args.url, args.concurrency, args.requests))


if __name__ == "__main__":
    main()
