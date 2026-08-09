from __future__ import annotations

import json

from sub2gen.diagnostics import collect_diagnostics, run_doctor


def test_release_diagnostics_cover_schema_packages_and_runtime(tmp_path) -> None:
    checks = collect_diagnostics(runtime_dir=tmp_path)
    names = {check.name for check in checks}

    assert all(check.ok for check in checks)
    assert {"python", "runtime_data", "sqlite_migrations", "fresh_schema", "styles"} <= names
    assert "module:sub2gen_provider_google_gemini" in names


def test_doctor_json_is_machine_readable(capsys) -> None:
    assert run_doctor(json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["checks"]
