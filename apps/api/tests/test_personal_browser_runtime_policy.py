from __future__ import annotations

from unittest.mock import patch

import pytest

from sub2gen.workers.personal import PersonalBrowserRuntimePolicy
from sub2gen.workers.personal.runtime import (
    flatten_exception_text,
    is_runtime_disconnect_error,
    is_runtime_normal_close_error,
)


def test_runtime_error_classification_flattens_exception_chains() -> None:
    cause = ConnectionError("websocket is not open")
    error = RuntimeError("browser operation failed")
    error.__cause__ = cause

    flattened = flatten_exception_text(error)

    assert "runtimeerror" in flattened
    assert "connectionerror" in flattened
    assert is_runtime_disconnect_error(error) is True
    assert is_runtime_normal_close_error(RuntimeError("sent 1000 (OK)")) is True


def test_launch_failure_backoff_is_stateful_and_bounded() -> None:
    policy = PersonalBrowserRuntimePolicy()
    with patch("sub2gen.workers.personal.runtime.time.monotonic", return_value=100.0):
        policy.record_launch_failure(ConnectionError("connection refused"))
        assert policy.launch_failure_streak == 1
        assert policy.launch_cooldown_until == 102.0

    with patch("sub2gen.workers.personal.runtime.time.monotonic", return_value=101.0):
        assert policy.cooldown_remaining() == 1.0
        with pytest.raises(RuntimeError, match="last_error=ConnectionError"):
            policy.raise_if_cooling_down()

    policy.reset_launch_failures()
    assert policy.launch_failure_streak == 0
    assert policy.cooldown_remaining() == 0.0


def test_runtime_policy_classifies_launch_and_context_failures() -> None:
    assert PersonalBrowserRuntimePolicy.is_retryable_launch_error("target closed") is True
    assert PersonalBrowserRuntimePolicy.is_memory_pressure_error("cannot allocate memory") is True
    assert PersonalBrowserRuntimePolicy.is_invalid_context_error("Cannot find context with specified id") is True
    assert PersonalBrowserRuntimePolicy.is_runtime_error("no browser is open") is True
