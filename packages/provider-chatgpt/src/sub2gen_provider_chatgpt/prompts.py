from __future__ import annotations

import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sub2gen_provider_sdk import GenerationRequest


@contextmanager
def materialized_references(request: GenerationRequest) -> Iterator[tuple[Path, ...]]:
    with tempfile.TemporaryDirectory(prefix="sub2gen-chatgpt-") as directory:
        root = Path(directory)
        paths: list[Path] = []
        for index, reference in enumerate(request.references):
            suffix = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}.get(
                reference.media_type, ".bin"
            )
            path = root / f"reference-{index}{suffix}"
            path.write_bytes(reference.read_bytes())
            paths.append(path)
        yield tuple(paths)
