"""Shared pytest fixtures.

Redirect ``$HOME`` (and ``Path.home()`` via the ``HOME`` env var) to a
per-test tmp directory so test artefacts — config.json, conversation log
SQLite DBs, persona files, etc. — never leak into the developer's real
home directory. ``Path.home()`` honours ``$HOME`` on POSIX, which is what
the bot uses for default storage paths.

Caveat: protects only DYNAMIC `Path.home()` evaluations. Module-load
constants like ``config.DEFAULT_CONFIG`` resolve before this fixture
runs and still point at the real home. Tests that rely on those
defaults must monkeypatch them explicitly.

Codex live tests (``codex_live`` marker) are skipped by this fixture
because they shell out to ``codex login status``, which reads
``~/.codex/auth.json`` from the *child* process's HOME. Redirecting
HOME would make those tests skip with "not authenticated" even on a
properly-configured machine.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_home(request, tmp_path, monkeypatch):
    if request.node.get_closest_marker("codex_live"):
        return
    monkeypatch.setenv("HOME", str(tmp_path))
