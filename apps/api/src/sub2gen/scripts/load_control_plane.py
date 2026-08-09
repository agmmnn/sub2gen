"""Concurrent HTTP latency benchmark for a deployed sub2gen control-plane route."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import time
from pathlib import Path
from typing import Any

import httpx


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(percentile * len(ordered)) - 1))
    return ordered[index]


async def _run(args: argparse.Namespace) -> int:
    headers = {"Accept": "application/json"}
    if args.bearer:
        headers["Authorization"] = f"Bearer {args.bearer}"
    body: Any = None
    if args.body:
        body = json.loads(Path(args.body).read_text(encoding="utf-8"))

    latencies: list[float] = []
    errors: list[str] = []
    stop_at = time.monotonic() + max(1.0, args.duration)
    request_count = 0
    lock = asyncio.Lock()

    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"),
        headers=headers,
        timeout=args.timeout,
        limits=httpx.Limits(
            max_connections=args.concurrency,
            max_keepalive_connections=args.concurrency,
        ),
    ) as client:
        if args.admin_cookie:
            client.cookies.set("admin_session", args.admin_cookie)

        async def worker() -> None:
            nonlocal request_count
            while time.monotonic() < stop_at:
                started = time.perf_counter()
                try:
                    response = await client.request(args.method, args.path, json=body)
                    elapsed = time.perf_counter() - started
                    async with lock:
                        request_count += 1
                        latencies.append(elapsed)
                        if response.status_code != args.expected_status:
                            errors.append(f"HTTP {response.status_code}")
                except Exception as exc:
                    async with lock:
                        request_count += 1
                        errors.append(type(exc).__name__)

        started_at = time.monotonic()
        await asyncio.gather(*(worker() for _ in range(max(1, args.concurrency))))
        elapsed_total = time.monotonic() - started_at

    result = {
        "requests": request_count,
        "successful_samples": len(latencies),
        "errors": len(errors),
        "duration_seconds": round(elapsed_total, 3),
        "requests_per_second": round(request_count / elapsed_total, 3) if elapsed_total else 0,
        "p50_ms": round(_percentile(latencies, 0.50) * 1000, 3),
        "p95_ms": round(_percentile(latencies, 0.95) * 1000, 3),
        "p99_ms": round(_percentile(latencies, 0.99) * 1000, 3),
        "max_ms": round(max(latencies, default=0.0) * 1000, 3),
        "target_p95_ms": args.target_p95_ms,
        "passed": bool(latencies and not errors and _percentile(latencies, 0.95) * 1000 < args.target_p95_ms),
        "error_examples": errors[:10],
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["passed"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--path", default="/health")
    parser.add_argument("--method", default="GET", choices=["GET", "POST"])
    parser.add_argument("--body", help="JSON request body file")
    parser.add_argument("--bearer", default="")
    parser.add_argument("--admin-cookie", default="")
    parser.add_argument("--duration", type=float, default=300.0)
    parser.add_argument("--concurrency", type=int, default=50)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--expected-status", type=int, default=200)
    parser.add_argument("--target-p95-ms", type=float, default=100.0)
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
