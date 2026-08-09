"""Deployment seed and mutable operational configuration layers."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class DeploymentSettings:
    """Immutable TOML/environment seed used to initialize operational settings."""

    values: Mapping[str, Any]

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> "DeploymentSettings":
        return cls(values=_freeze(deepcopy(dict(values))))


class _TrackedDict(dict[str, Any]):
    def __init__(self, values: Mapping[str, Any], callback: Callable[[tuple[str, ...], Any], None], path=()) -> None:
        super().__init__()
        self._callback = callback
        self._path = tuple(path)
        for key, value in values.items():
            dict.__setitem__(self, key, self._wrap(str(key), value))

    def _wrap(self, key: str, value: Any) -> Any:
        if isinstance(value, dict) and not isinstance(value, _TrackedDict):
            return _TrackedDict(value, self._callback, (*self._path, key))
        return value

    def __setitem__(self, key: str, value: Any) -> None:
        wrapped = self._wrap(str(key), value)
        dict.__setitem__(self, key, wrapped)
        self._callback((*self._path, str(key)), value)

    def setdefault(self, key: str, default: Any = None) -> Any:
        if key not in self:
            self[key] = default
        return self[key]

    def __deepcopy__(self, memo: dict[int, Any]) -> dict[str, Any]:
        copied = {key: deepcopy(value, memo) for key, value in self.items()}
        memo[id(self)] = copied
        return copied


class OperationalSettings:
    """Mutable database-backed overrides layered over deployment settings."""

    def __init__(self, deployment: DeploymentSettings) -> None:
        self.deployment = deployment
        self.overrides: dict[str, Any] = {}
        self.effective = _TrackedDict(self._thaw(deployment.values), self._record)

    @staticmethod
    def _thaw(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {key: OperationalSettings._thaw(item) for key, item in value.items()}
        if isinstance(value, tuple):
            return [OperationalSettings._thaw(item) for item in value]
        return deepcopy(value)

    def _record(self, path: tuple[str, ...], value: Any) -> None:
        if not path:
            return
        cursor = self.overrides
        for key in path[:-1]:
            cursor = cursor.setdefault(key, {})
        cursor[path[-1]] = deepcopy(value)

    def set(self, section: str, key: str, value: Any) -> None:
        section_values = self.effective.setdefault(section, {})
        section_values[key] = value

    def snapshot(self) -> dict[str, Any]:
        return self._thaw(self.effective)

    def overrides_snapshot(self) -> dict[str, Any]:
        return self._thaw(self.overrides)

    def override_items(self) -> Iterator[tuple[str, Any]]:
        return iter(self.overrides.items())
