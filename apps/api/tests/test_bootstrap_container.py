import asyncio
from types import SimpleNamespace

import pytest

from sub2gen import main
from sub2gen.bootstrap.container import AppContainer
from sub2gen.bootstrap.dependencies import get_container, get_websocket_container
from sub2gen.bootstrap.lifecycle import build_lifespan
from sub2gen.bootstrap.tasks import TaskRegistry


def test_container_dependencies_are_scoped_to_the_application() -> None:
    first = object()
    second = object()
    first_request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(container=first)))
    second_request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(container=second)))

    assert get_container(first_request) is first
    assert get_container(second_request) is second
    assert get_websocket_container(first_request) is first


def test_app_container_has_no_process_global_accessor() -> None:
    assert not hasattr(AppContainer, "get_instance")
    assert not hasattr(AppContainer, "set_instance")


def test_application_lifecycle_is_composed_by_bootstrap() -> None:
    assert main.lifespan.__module__ == build_lifespan.__module__


@pytest.mark.asyncio
async def test_task_registry_rejects_duplicate_names_and_cancels_tasks() -> None:
    registry = TaskRegistry()
    started = asyncio.Event()

    async def worker() -> None:
        started.set()
        await asyncio.Event().wait()

    task = registry.start("worker", worker())
    await started.wait()
    assert registry.is_running("worker") is True

    with pytest.raises(RuntimeError, match="already running"):
        registry.start("worker", worker())

    await registry.cancel_all()

    assert task.cancelled()
    assert registry.names == ()
    assert registry.is_running("worker") is False
