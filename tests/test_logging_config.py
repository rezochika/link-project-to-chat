"""Tests for logging_config module."""
from __future__ import annotations

import json
import logging

import pytest

from link_project_to_chat.logging_config import JSONFormatter, configure_logging


class TestJSONFormatter:
    def test_format_returns_valid_json(self) -> None:
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="hello world",
            args=(),
            exc_info=None,
        )
        output = formatter.format(record)
        data = json.loads(output)
        assert isinstance(data, dict)

    def test_format_has_expected_keys(self) -> None:
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="test.logger",
            level=logging.WARNING,
            pathname="",
            lineno=0,
            msg="test message",
            args=(),
            exc_info=None,
        )
        data = json.loads(formatter.format(record))
        assert "timestamp" in data
        assert "level" in data
        assert "logger" in data
        assert "message" in data

    def test_format_values_correct(self) -> None:
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="my.logger",
            level=logging.ERROR,
            pathname="",
            lineno=0,
            msg="something failed",
            args=(),
            exc_info=None,
        )
        data = json.loads(formatter.format(record))
        assert data["level"] == "ERROR"
        assert data["logger"] == "my.logger"
        assert data["message"] == "something failed"

    def test_format_includes_exception_info_when_present(self) -> None:
        formatter = JSONFormatter()
        try:
            raise ValueError("test error")
        except ValueError:
            import sys
            exc_info = sys.exc_info()

        record = logging.LogRecord(
            name="test.logger",
            level=logging.ERROR,
            pathname="",
            lineno=0,
            msg="an error occurred",
            args=(),
            exc_info=exc_info,
        )
        data = json.loads(formatter.format(record))
        assert "exception" in data
        assert "ValueError" in data["exception"]
        assert "test error" in data["exception"]

    def test_format_no_exception_key_without_exc_info(self) -> None:
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="no error",
            args=(),
            exc_info=None,
        )
        data = json.loads(formatter.format(record))
        assert "exception" not in data


class TestConfigureLogging:
    def setup_method(self) -> None:
        # Save original state
        self._original_handlers = logging.root.handlers[:]
        self._original_level = logging.root.level

    def teardown_method(self) -> None:
        # Restore original state
        logging.root.handlers.clear()
        logging.root.handlers.extend(self._original_handlers)
        logging.root.setLevel(self._original_level)

    def test_configure_logging_json_sets_json_formatter(self) -> None:
        configure_logging("INFO", "json")
        assert len(logging.root.handlers) == 1
        handler = logging.root.handlers[0]
        assert isinstance(handler.formatter, JSONFormatter)

    def test_configure_logging_text_sets_standard_formatter(self) -> None:
        configure_logging("INFO", "text")
        assert len(logging.root.handlers) == 1
        handler = logging.root.handlers[0]
        assert not isinstance(handler.formatter, JSONFormatter)
        assert isinstance(handler.formatter, logging.Formatter)

    def test_configure_logging_sets_level(self) -> None:
        configure_logging("DEBUG", "text")
        assert logging.root.level == logging.DEBUG

    def test_configure_logging_sets_warning_level(self) -> None:
        configure_logging("WARNING", "text")
        assert logging.root.level == logging.WARNING

    def test_configure_logging_clears_existing_handlers(self) -> None:
        # Add a dummy handler first
        dummy = logging.StreamHandler()
        logging.root.addHandler(dummy)
        configure_logging("INFO", "text")
        # Should only have one handler after configure
        assert len(logging.root.handlers) == 1

    def test_configure_logging_json_produces_valid_json_output(self, capfd: pytest.CaptureFixture[str]) -> None:
        configure_logging("INFO", "json")
        logger = logging.getLogger("test.json.output")
        logger.info("test json output")
        captured = capfd.readouterr()
        output = captured.err.strip()
        data = json.loads(output)
        assert data["message"] == "test json output"
        assert data["level"] == "INFO"
