"""Application composition and lifecycle primitives."""

from .container import AppContainer, build_container
from .tasks import TaskRegistry

__all__ = ["AppContainer", "TaskRegistry", "build_container"]
