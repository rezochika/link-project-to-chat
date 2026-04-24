from pathlib import Path

import pytest

from link_project_to_chat.backends.factory import available, create, register


class _DummyBackend:
    def __init__(self, project_path: Path, state: dict):
        self.project_path = project_path
        self.state = state


def test_register_and_create_backend():
    register("dummy", lambda project_path, state: _DummyBackend(project_path, state))

    backend = create("dummy", Path("/tmp/project"), {"model": "x"})

    assert backend.project_path == Path("/tmp/project")
    assert backend.state == {"model": "x"}
    assert "dummy" in available()


def test_duplicate_registration_fails():
    register("duplicate", lambda project_path, state: _DummyBackend(project_path, state))

    with pytest.raises(ValueError, match="already registered"):
        register("duplicate", lambda project_path, state: _DummyBackend(project_path, state))


def test_unknown_backend_fails():
    with pytest.raises(KeyError, match="Unknown backend"):
        create("missing", Path("/tmp/project"), {})
