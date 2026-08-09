from __future__ import annotations

import pytest
from sub2gen.services.reference_inputs import ReferenceInputError, _validate_public_http_url


@pytest.mark.parametrize("url", ["http://127.0.0.1/image.png", "http://localhost/image.png", "file:///tmp/image.png"])
async def test_remote_reference_rejects_local_and_non_http_targets(url):
    with pytest.raises(ReferenceInputError):
        await _validate_public_http_url(url)
