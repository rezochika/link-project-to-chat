from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .base import AgentBackend

BackendFactory = Callable[[Path, dict], AgentBackend]

_registry: dict[str, BackendFactory] = {}


def register(name: str, factory: BackendFactory) -> None:
    if name in _registry:
        raise ValueError(f"Backend {name!r} already registered")
    _registry[name] = factory


def create(name: str, project_path: Path, state: dict) -> AgentBackend:
    if name not in _registry:
        raise KeyError(f"Unknown backend {name!r}; available: {sorted(_registry)}")
    return _registry[name](project_path, state)


def available() -> list[str]:
    return sorted(_registry)
