from __future__ import annotations

import time

import pytest

from link_project_to_chat.google_chat.cards import CallbackTokenError, make_callback_token, verify_callback_token
from link_project_to_chat.transport.base import Button, ButtonStyle, Buttons
from link_project_to_chat.google_chat.cards import build_buttons_card


def test_callback_token_round_trips_bound_payload():
    secret = b"x" * 32
    token = make_callback_token(
        secret=secret,
        payload={"space": "spaces/AAA", "sender": "users/1", "kind": "button", "value": "run"},
        ttl_seconds=60,
        now=1000,
    )

    payload = verify_callback_token(secret=secret, token=token, now=1001)

    assert payload["space"] == "spaces/AAA"
    assert payload["value"] == "run"


def test_callback_token_rejects_tampering():
    secret = b"x" * 32
    token = make_callback_token(secret=secret, payload={"value": "run"}, ttl_seconds=60, now=1000)

    with pytest.raises(CallbackTokenError):
        verify_callback_token(secret=secret, token=token + "x", now=1001)


def test_buttons_card_contains_callback_token_not_raw_secret():
    secret = b"x" * 32
    buttons = Buttons(rows=[[Button("Run", "run", ButtonStyle.PRIMARY)]])

    card = build_buttons_card(
        buttons,
        secret=secret,
        space="spaces/AAA",
        sender="users/1",
        message="spaces/AAA/messages/1",
        now=int(time.time()),
        ttl_seconds=60,
    )

    assert "callback_token" in str(card)
    assert "Bearer" not in str(card)
