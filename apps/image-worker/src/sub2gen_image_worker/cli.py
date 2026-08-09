from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from sub2gen_provider_chatgpt import ChatGPTImagegenProcessBackend, ChromeUseProcessAdapter

from .client import ImageWorkerClient
from .config import WorkerConfig, default_config_path
from .identity import DeviceIdentity


def _identity_path(config_path: Path) -> Path:
    return config_path.with_name("image-worker-identity.json")


async def _run(args) -> int:
    path = Path(args.config).expanduser()
    config = WorkerConfig.load(path)
    identity = DeviceIdentity.load_or_create(_identity_path(path))
    client = ImageWorkerClient(config, identity)
    if args.command == "init":
        print(config.save(path))
        return 0
    if args.command == "pair":
        print(json.dumps(await client.pair(args.code), indent=2))
        return 0
    if args.command == "health":
        backend = ChatGPTImagegenProcessBackend(
            config.imagegen_executable,
            chrome_use=ChromeUseProcessAdapter(config.chrome_use_executable),
        )
        health = await backend.health()
        print(json.dumps({"status": health.status.value, "detail": health.detail, "metadata": dict(health.metadata)}))
        return 0 if health.status.value == "ready" else 1
    await client.run()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="sub2gen-image-worker")
    parser.add_argument("--config", default=str(default_config_path()))
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    pair = commands.add_parser("pair")
    pair.add_argument("code")
    commands.add_parser("health")
    commands.add_parser("run")
    raise SystemExit(asyncio.run(_run(parser.parse_args())))
