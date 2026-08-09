from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LocalStyleSelection:
    names: tuple[str, ...] = ()
    disabled: bool = True

    def cli_args(self) -> tuple[str, ...]:
        if self.disabled:
            return ("--no-style",)
        return tuple(item for name in self.names for item in ("--style", name))
