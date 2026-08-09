"""Small FastAPI dependencies backed by the application-owned container."""

from __future__ import annotations

from fastapi import WebSocket
from starlette.requests import HTTPConnection

from .container import AppContainer


def get_container(connection: HTTPConnection) -> AppContainer:
    """Return the dependency container for the current HTTP or WebSocket application."""

    return connection.app.state.container


def get_websocket_container(websocket: WebSocket) -> AppContainer:
    """Return the dependency container for the current WebSocket application."""

    return websocket.app.state.container
