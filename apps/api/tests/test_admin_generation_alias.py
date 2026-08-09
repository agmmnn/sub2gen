from __future__ import annotations

from types import SimpleNamespace

import pytest

from sub2gen.api.admin import get_generation_config, get_generation_timeout
from sub2gen.core.database import GenerationConfig


class _GenerationDatabase:
    async def get_generation_config(self) -> GenerationConfig:
        return GenerationConfig(image_timeout=321, video_timeout=654, max_retries=7)


@pytest.mark.asyncio
async def test_generation_timeout_alias_forwards_the_application_container() -> None:
    container = SimpleNamespace(db=_GenerationDatabase())

    canonical = await get_generation_config("admin-token", container)  # type: ignore[arg-type]
    alias = await get_generation_timeout("admin-token", container)  # type: ignore[arg-type]

    assert alias == canonical
    assert alias["config"]["image_timeout"] == 321
    assert alias["config"]["video_timeout"] == 654
    assert alias["config"]["max_retries"] == 7
