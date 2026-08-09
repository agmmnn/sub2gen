"""Inspect or explicitly initialize the sub2gen Redis state marker."""

from __future__ import annotations

import argparse
import asyncio
import json

from ..services.redis_runtime import RedisRuntime


async def _run(args: argparse.Namespace) -> int:
    runtime = RedisRuntime(mode="shadow")
    if args.command == "init":
        status = await runtime.initialize_state(force=args.force)
    else:
        await runtime.start()
        status = runtime.status_snapshot()
    print(json.dumps(status, indent=2, sort_keys=True))
    await runtime.stop()
    return 0 if status.get("redis_ready") else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="Check connectivity and the state marker")
    init_parser = subparsers.add_parser("init", help="Initialize a new/empty Redis deployment")
    init_parser.add_argument(
        "--force",
        action="store_true",
        help="Replace a mismatched version marker after manual review",
    )
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
