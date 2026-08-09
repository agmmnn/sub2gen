"""Protocol negotiation that keeps unversioned clients on their frozen dialect."""

from __future__ import annotations

from dataclasses import dataclass

from .generated import PROTOCOL_VERSION


class ProtocolNegotiationError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class NegotiatedProtocol:
    version: str | None
    legacy: bool


def negotiate_protocol(supported_versions: tuple[str, ...] | list[str] | None) -> NegotiatedProtocol:
    if supported_versions is None:
        return NegotiatedProtocol(version=None, legacy=True)
    normalized = tuple(dict.fromkeys(str(item).strip() for item in supported_versions if str(item).strip()))
    if PROTOCOL_VERSION in normalized:
        return NegotiatedProtocol(version=PROTOCOL_VERSION, legacy=False)
    raise ProtocolNegotiationError("worker does not support a server protocol version")
