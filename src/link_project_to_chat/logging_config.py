"""Logging configuration with text and JSON formatters."""
from __future__ import annotations

import json
import logging


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log_data = {
            "timestamp": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_data)


def configure_logging(level: str = "INFO", fmt: str = "text") -> None:
    """Configure root logging. fmt is 'text' or 'json'."""
    handler = logging.StreamHandler()
    if fmt == "json":
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.root.handlers.clear()
    logging.root.addHandler(handler)
    logging.root.setLevel(getattr(logging, level.upper(), logging.INFO))
