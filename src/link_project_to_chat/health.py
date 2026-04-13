"""Optional HTTP health check endpoint."""
from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

logger = logging.getLogger(__name__)


class HealthServer:
    """Runs a tiny HTTP server in a background thread."""

    def __init__(self, port: int, status_fn: Callable[[], dict[str, Any]]) -> None:
        self._port = port
        self._status_fn = status_fn
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path == "/health":
                    data = parent._status_fn()
                    body = json.dumps(data).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, format: str, *args: Any) -> None:
                pass  # suppress request logging

        self._server = HTTPServer(("0.0.0.0", self._port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        logger.info("Health check server started on port %d", self._port)

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server = None
