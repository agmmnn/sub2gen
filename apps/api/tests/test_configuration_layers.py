from __future__ import annotations

import pytest

from sub2gen.core.config import Config
from sub2gen.core.settings import DeploymentSettings, OperationalSettings


def test_deployment_seed_is_immutable_and_operational_values_are_tracked() -> None:
    deployment = DeploymentSettings.from_mapping({"debug": {"enabled": False}})
    operational = OperationalSettings(deployment)

    with pytest.raises(TypeError):
        deployment.values["debug"]["enabled"] = True

    operational.effective["debug"]["enabled"] = True
    assert operational.snapshot()["debug"]["enabled"] is True
    assert operational.overrides_snapshot() == {"debug": {"enabled": True}}
    assert deployment.values["debug"]["enabled"] is False


def test_config_runtime_setters_record_operational_source_without_mutating_deployment() -> None:
    config = Config()
    deployment_debug = bool(config.get_deployment_config().get("debug", {}).get("enabled", False))

    config.set_debug_enabled(not deployment_debug)

    assert config.debug_enabled is (not deployment_debug)
    assert config.get_operational_overrides()["debug"]["enabled"] is (not deployment_debug)
    assert config.get_deployment_config()["debug"]["enabled"] is deployment_debug
