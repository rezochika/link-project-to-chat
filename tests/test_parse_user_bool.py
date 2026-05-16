"""parse_user_bool — shared helper for CLI / manager / config-field bool input.

Pins the accepted vocabulary so future drift between cli.py and manager/bot.py
fails loudly. Callers depend on the tri-state (True / False / None) contract;
``None`` lets them report errors in their own idiom (SystemExit vs send_text).
"""
from __future__ import annotations

import pytest

from link_project_to_chat.config import parse_user_bool


@pytest.mark.parametrize("raw", ["true", "True", "TRUE", "yes", "YES", "on", "ON", "1"])
def test_parse_user_bool_truthy(raw: str):
    assert parse_user_bool(raw) is True


@pytest.mark.parametrize("raw", ["false", "False", "FALSE", "no", "NO", "off", "OFF", "0"])
def test_parse_user_bool_falsy(raw: str):
    assert parse_user_bool(raw) is False


def test_parse_user_bool_strips_whitespace():
    assert parse_user_bool("  true  ") is True
    assert parse_user_bool("\tno\n") is False


@pytest.mark.parametrize("raw", ["", "  ", "maybe", "truthy", "2", "01", "y", "n"])
def test_parse_user_bool_unrecognized_returns_none(raw: str):
    assert parse_user_bool(raw) is None


def test_parse_user_bool_non_string_returns_none():
    # Helper signature is ``str``-only, but defensive callers may pass anything
    # they pulled out of click/PTB. Returning None keeps the tri-state contract.
    assert parse_user_bool(None) is None  # type: ignore[arg-type]
    assert parse_user_bool(True) is None  # type: ignore[arg-type]
    assert parse_user_bool(1) is None  # type: ignore[arg-type]
    assert parse_user_bool(["true"]) is None  # type: ignore[arg-type]
