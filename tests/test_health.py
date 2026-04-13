"""Tests for the HealthServer."""
from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from typing import Any

import pytest

from link_project_to_chat.health import HealthServer


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _status_fn() -> dict[str, Any]:
    return {"uptime": 42.0, "tasks_running": 2, "session_id": "abc123"}


class TestHealthServer:
    def test_health_server_starts_and_responds(self) -> None:
        port = _find_free_port()
        server = HealthServer(port, _status_fn)
        server.start()
        try:
            time.sleep(0.05)
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health") as resp:
                assert resp.status == 200
        finally:
            server.stop()

    def test_health_response_is_valid_json(self) -> None:
        port = _find_free_port()
        server = HealthServer(port, _status_fn)
        server.start()
        try:
            time.sleep(0.05)
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health") as resp:
                body = resp.read().decode()
                data = json.loads(body)
                assert isinstance(data, dict)
        finally:
            server.stop()

    def test_health_response_contains_expected_keys(self) -> None:
        port = _find_free_port()
        server = HealthServer(port, _status_fn)
        server.start()
        try:
            time.sleep(0.05)
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health") as resp:
                data = json.loads(resp.read().decode())
                assert "uptime" in data
                assert "tasks_running" in data
                assert "session_id" in data
        finally:
            server.stop()

    def test_health_response_values_match_status_fn(self) -> None:
        port = _find_free_port()
        server = HealthServer(port, _status_fn)
        server.start()
        try:
            time.sleep(0.05)
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health") as resp:
                data = json.loads(resp.read().decode())
                assert data["uptime"] == 42.0
                assert data["tasks_running"] == 2
                assert data["session_id"] == "abc123"
        finally:
            server.stop()

    def test_other_paths_return_404(self) -> None:
        port = _find_free_port()
        server = HealthServer(port, _status_fn)
        server.start()
        try:
            time.sleep(0.05)
            with pytest.raises(urllib.error.HTTPError) as exc_info:  # type: ignore[attr-defined]
                urllib.request.urlopen(f"http://127.0.0.1:{port}/other")
            assert exc_info.value.code == 404
        finally:
            server.stop()

    def test_root_path_returns_404(self) -> None:
        port = _find_free_port()
        server = HealthServer(port, _status_fn)
        server.start()
        try:
            time.sleep(0.05)
            with pytest.raises(urllib.error.HTTPError) as exc_info:  # type: ignore[attr-defined]
                urllib.request.urlopen(f"http://127.0.0.1:{port}/")
            assert exc_info.value.code == 404
        finally:
            server.stop()

    def test_stop_shuts_down_cleanly(self) -> None:
        port = _find_free_port()
        server = HealthServer(port, _status_fn)
        server.start()
        time.sleep(0.05)
        server.stop()
        # After stop, connection should be refused
        time.sleep(0.05)
        with pytest.raises(OSError):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1)

    def test_stop_when_not_started_is_safe(self) -> None:
        port = _find_free_port()
        server = HealthServer(port, _status_fn)
        # Should not raise even though start was never called
        server.stop()

    def test_health_content_type_is_json(self) -> None:
        port = _find_free_port()
        server = HealthServer(port, _status_fn)
        server.start()
        try:
            time.sleep(0.05)
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health") as resp:
                content_type = resp.headers.get("Content-Type", "")
                assert "application/json" in content_type
        finally:
            server.stop()

    def test_status_fn_is_called_on_each_request(self) -> None:
        port = _find_free_port()
        call_count = 0

        def counting_status() -> dict[str, Any]:
            nonlocal call_count
            call_count += 1
            return {"uptime": float(call_count), "tasks_running": 0, "session_id": None}

        server = HealthServer(port, counting_status)
        server.start()
        try:
            time.sleep(0.05)
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health").close()
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health").close()
            assert call_count == 2
        finally:
            server.stop()
