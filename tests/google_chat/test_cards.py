from __future__ import annotations

import time

import pytest

from link_project_to_chat.google_chat import cards
from link_project_to_chat.google_chat.cards import CallbackTokenError, build_buttons_card, make_callback_token, verify_callback_token
from link_project_to_chat.transport.base import Button, ButtonStyle, Buttons, PromptKind, PromptSpec


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


def test_build_prompt_card_text_kind_carries_kind_prompt_callback():
    secret = b"x" * 32
    card = cards.build_prompt_card(
        PromptSpec(
            key="name",
            title="Name",
            body="Enter name",
            kind=PromptKind.TEXT,
            placeholder="Name",
            submit_label="Send",
        ),
        secret=secret,
        space="spaces/AAA",
        prompt_id="p-1",
        expected_sender_native_id="users/1",
        now=1000,
        ttl_seconds=60,
    )

    widget = card["cardsV2"][0]["card"]["sections"][0]["widgets"][0]
    button = card["cardsV2"][0]["card"]["sections"][0]["widgets"][1]["buttonList"]["buttons"][0]
    params = {param["key"]: param["value"] for param in button["onClick"]["action"]["parameters"]}
    payload = verify_callback_token(secret=secret, token=params["callback_token"], now=1001)

    assert widget["textInput"]["name"] == "answer"
    assert widget["textInput"]["label"] == "Name"
    assert params["form_field"] == "answer"
    assert payload["kind"] == "prompt"
    assert payload["prompt_id"] == "p-1"
    assert payload["space"] == "spaces/AAA"
    assert payload["sender"] == "users/1"
