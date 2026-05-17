from __future__ import annotations

import base64
import hashlib
import hmac
import json

from link_project_to_chat.transport.base import Buttons
from link_project_to_chat.transport.base import PromptKind, PromptOption, PromptSpec


class CallbackTokenError(Exception):
    """Invalid or expired Google Chat callback token."""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(data: str) -> bytes:
    padded = data + ("=" * (-len(data) % 4))
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def make_callback_token(*, secret: bytes, payload: dict, ttl_seconds: int, now: int) -> str:
    body = dict(payload)
    body["expires_at"] = now + ttl_seconds
    raw = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(secret, raw, hashlib.sha256).digest()
    return f"{_b64(raw)}.{_b64(signature)}"


def verify_callback_token(*, secret: bytes, token: str, now: int) -> dict:
    try:
        raw_b64, sig_b64 = token.split(".", 1)
        raw = _unb64(raw_b64)
        supplied = _unb64(sig_b64)
    except Exception as exc:
        raise CallbackTokenError("malformed callback token") from exc
    expected = hmac.new(secret, raw, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, supplied):
        raise CallbackTokenError("invalid callback token")
    payload = json.loads(raw.decode("utf-8"))
    try:
        expires_at = int(payload["expires_at"])
    except (KeyError, TypeError, ValueError) as exc:
        # An attacker who can produce a valid HMAC already owns the secret,
        # but a uniform exception type keeps the public surface predictable
        # for callers and avoids leaking parse internals.
        raise CallbackTokenError("malformed callback token: missing expires_at") from exc
    if expires_at < now:
        raise CallbackTokenError("expired callback token")
    return payload


def build_buttons_card(
    buttons: Buttons,
    *,
    secret: bytes,
    space: str,
    sender: str,
    message: str,
    now: int,
    ttl_seconds: int,
) -> dict:
    """Build a Cards v2 dict with HMAC-signed callback tokens for each button."""
    widgets = []
    for row in buttons.rows:
        button_list = []
        for button in row:
            token = make_callback_token(
                secret=secret,
                payload={
                    "space": space,
                    "sender": sender,
                    "kind": "button",
                    "value": button.value,
                    "message": message,
                },
                ttl_seconds=ttl_seconds,
                now=now,
            )
            button_list.append(
                {
                    "text": button.label,
                    "onClick": {
                        "action": {
                            "function": "lp2c_button_click",
                            "parameters": [
                                {"key": "callback_token", "value": token},
                            ],
                        },
                    },
                }
            )
        widgets.append({"buttonList": {"buttons": button_list}})

    return {
        "cardsV2": [
            {
                "cardId": "lp2c-buttons",
                "card": {
                    "sections": [
                        {
                            "widgets": widgets,
                        }
                    ],
                },
            }
        ],
    }


def build_prompt_card(
    spec: PromptSpec,
    *,
    secret: bytes,
    space: str,
    prompt_id: str,
    expected_sender_native_id: str | None,
    now: int,
    ttl_seconds: int,
) -> dict:
    """Build a Cards v2 prompt payload with HMAC-signed submit callbacks."""
    widgets: list[dict] = []

    if spec.kind in {PromptKind.TEXT, PromptKind.SECRET}:
        text_input: dict[str, object] = {
            "name": "answer",
            "label": spec.title,
            "type": "SINGLE_LINE",
        }
        if spec.placeholder:
            text_input["hintText"] = spec.placeholder
        if spec.initial_text:
            text_input["value"] = spec.initial_text
        widgets.append({"textInput": text_input})
        widgets.append(
            {
                "buttonList": {
                    "buttons": [
                        _prompt_button(
                            label=spec.submit_label,
                            secret=secret,
                            space=space,
                            prompt_id=prompt_id,
                            expected_sender_native_id=expected_sender_native_id,
                            value=None,
                            form_field="answer",
                            now=now,
                            ttl_seconds=ttl_seconds,
                        )
                    ]
                }
            }
        )
    elif spec.kind is PromptKind.CHOICE:
        widgets.append(
            {
                "buttonList": {
                    "buttons": [
                        _prompt_option_button(
                            option,
                            secret=secret,
                            space=space,
                            prompt_id=prompt_id,
                            expected_sender_native_id=expected_sender_native_id,
                            now=now,
                            ttl_seconds=ttl_seconds,
                        )
                        for option in spec.options
                    ]
                }
            }
        )
    elif spec.kind is PromptKind.CONFIRM:
        widgets.append(
            {
                "buttonList": {
                    "buttons": [
                        _prompt_button(
                            label="Yes",
                            secret=secret,
                            space=space,
                            prompt_id=prompt_id,
                            expected_sender_native_id=expected_sender_native_id,
                            value="yes",
                            form_field=None,
                            now=now,
                            ttl_seconds=ttl_seconds,
                        ),
                        _prompt_button(
                            label="No",
                            secret=secret,
                            space=space,
                            prompt_id=prompt_id,
                            expected_sender_native_id=expected_sender_native_id,
                            value="no",
                            form_field=None,
                            now=now,
                            ttl_seconds=ttl_seconds,
                        ),
                    ]
                }
            }
        )

    return {
        "cardsV2": [
            {
                "cardId": "lp2c-prompt",
                "card": {
                    "header": {"title": spec.title},
                    "sections": [{"widgets": widgets}],
                },
            }
        ],
    }


def _prompt_option_button(
    option: PromptOption,
    *,
    secret: bytes,
    space: str,
    prompt_id: str,
    expected_sender_native_id: str | None,
    now: int,
    ttl_seconds: int,
) -> dict:
    return _prompt_button(
        label=option.label,
        secret=secret,
        space=space,
        prompt_id=prompt_id,
        expected_sender_native_id=expected_sender_native_id,
        value=option.value,
        form_field=None,
        now=now,
        ttl_seconds=ttl_seconds,
    )


def _prompt_button(
    *,
    label: str,
    secret: bytes,
    space: str,
    prompt_id: str,
    expected_sender_native_id: str | None,
    value: str | None,
    form_field: str | None,
    now: int,
    ttl_seconds: int,
) -> dict:
    payload = {
        "space": space,
        "kind": "prompt",
        "prompt_id": prompt_id,
    }
    if value is not None:
        payload["value"] = value
    if expected_sender_native_id is not None:
        payload["sender"] = expected_sender_native_id

    params = [
        {
            "key": "callback_token",
            "value": make_callback_token(
                secret=secret,
                payload=payload,
                ttl_seconds=ttl_seconds,
                now=now,
            ),
        }
    ]
    if form_field is not None:
        params.append({"key": "form_field", "value": form_field})

    return {
        "text": label,
        "onClick": {
            "action": {
                "function": "lp2c_prompt_submit",
                "parameters": params,
            },
        },
    }
