from __future__ import annotations

import hashlib
import io
from pathlib import Path

from PIL import Image
from sub2gen_provider_sdk import Artifact, ProviderError, ProviderErrorCode


def read_image_artifact(path: Path) -> Artifact:
    data = path.read_bytes()
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
            media_type = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}[image.format or ""]
    except (OSError, KeyError) as exc:
        raise ProviderError(ProviderErrorCode.INVALID_OUTPUT, "ChatGPT returned an invalid image") from exc
    return Artifact(media_type, data=data, filename=path.name, sha256=hashlib.sha256(data).hexdigest())
