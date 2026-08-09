from __future__ import annotations

import json
from pathlib import Path

from sub2gen.main import app


CONTRACT_ROOT = Path(__file__).resolve().parent / "contracts"


def test_openapi_contract_matches_snapshot() -> None:
    expected = json.loads((CONTRACT_ROOT / "openapi.json").read_text(encoding="utf-8"))
    actual = app.openapi()

    assert actual == expected, (
        "The public HTTP contract changed. If the change is intentional, review it and run "
        "`uv run python scripts/update_contract_snapshots.py`."
    )
