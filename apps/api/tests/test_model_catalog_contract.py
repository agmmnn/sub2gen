from __future__ import annotations

import json
from pathlib import Path

from sub2gen.contract_baseline import build_model_catalog_snapshot


CONTRACT_PATH = Path(__file__).parent / "contracts" / "model-catalog.json"


def test_model_catalog_matches_snapshot() -> None:
    expected = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

    assert build_model_catalog_snapshot() == expected, (
        "The model catalog inputs changed. If the change is intentional, review it and run "
        "`uv run python scripts/update_contract_snapshots.py`."
    )
