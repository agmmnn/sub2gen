from __future__ import annotations

from sub2gen.workers.personal import ResidentTabInfo, ResidentTabRegistry


def test_registry_owns_slot_identity_affinity_and_reservations() -> None:
    registry = ResidentTabRegistry(slot_prefix="b2-")
    slot_id = registry.next_slot_id()
    info = ResidentTabInfo(object(), slot_id)
    registry.tabs[slot_id] = info

    registry.remember_project("project-1", slot_id, info)
    registry.remember_token(7, slot_id, info)

    assert slot_id == "b2-slot-1"
    assert registry.resolve_project_affinity("project-1") == slot_id
    assert registry.resolve_token_affinity(7) == slot_id
    assert registry.reserve(slot_id) is True
    assert registry.resolve_token_affinity(7, available_only=True) is None
    assert registry.reserve(slot_id) is False
    registry.release(slot_id)
    assert registry.resolve_token_affinity(7, available_only=True) == slot_id


def test_registry_invalidates_unavailable_and_stale_affinity() -> None:
    registry = ResidentTabRegistry()
    info = ResidentTabInfo(object(), "slot-1", project_id="project-1", token_id=7)
    info.recaptcha_ready = True
    registry.tabs[info.slot_id] = info
    registry.remember_project("project-1", info.slot_id, info)
    registry.remember_token(7, info.slot_id, info)

    registry.mark_unavailable(info.slot_id)
    assert info.recaptcha_ready is False
    assert registry.resolve_project_affinity("project-1") is None
    assert registry.resolve_token_affinity(7) is None

    registry.clear_unavailable(info.slot_id)
    assert registry.resolve_project_affinity("project-1") == info.slot_id
    registry.tabs.pop(info.slot_id)
    assert registry.resolve_project_affinity("project-1") is None
    assert "project-1" not in registry.project_affinity
