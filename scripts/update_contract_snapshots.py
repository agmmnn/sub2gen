"""Regenerate deterministic API contract snapshots after an intentional change."""

from __future__ import annotations

import json
from pathlib import Path

from sub2gen.contract_baseline import build_model_catalog_snapshot
from sub2gen.main import app


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_ROOT = REPOSITORY_ROOT / "apps" / "api" / "tests" / "contracts"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    write_json(CONTRACT_ROOT / "openapi.json", app.openapi())
    write_json(CONTRACT_ROOT / "model-catalog.json", build_model_catalog_snapshot())
    print(f"Updated {CONTRACT_ROOT / 'openapi.json'}")
    print(f"Updated {CONTRACT_ROOT / 'model-catalog.json'}")


if __name__ == "__main__":
    main()
