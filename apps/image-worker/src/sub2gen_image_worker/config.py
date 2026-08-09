from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse


def default_config_path() -> Path:
    return Path(os.environ.get("SUB2GEN_IMAGE_WORKER_CONFIG", "~/.config/sub2gen/image-worker.json")).expanduser()


@dataclass(frozen=True, slots=True)
class WorkerConfig:
    server_url: str = "http://127.0.0.1:8000"
    worker_id: str = "local-image-worker"
    label: str = "Local image worker"
    account_ref: str = "chatgpt:default"
    profile_ref: str = "chrome:relay"
    imagegen_executable: str = "vendor/chatgpt-imagegen/chatgpt-imagegen"
    chrome_use_executable: str = "chrome-use"
    project: str = "sub2gen"
    capabilities: tuple[str, ...] = ("image.generate:chatgpt-web",)

    @property
    def websocket_url(self) -> str:
        parsed = urlparse(self.server_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return f"{scheme}://{parsed.netloc}/worker_ws"

    @classmethod
    def load(cls, path: Path | None = None) -> "WorkerConfig":
        target = path or default_config_path()
        if not target.is_file():
            return cls()
        value = json.loads(target.read_text(encoding="utf-8"))
        value["capabilities"] = tuple(value.get("capabilities") or ())
        return cls(**value)

    def save(self, path: Path | None = None) -> Path:
        target = path or default_config_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")
        return target
