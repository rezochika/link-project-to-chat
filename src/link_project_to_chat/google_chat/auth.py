from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

logger = logging.getLogger(__name__)


class GoogleChatAuthError(Exception):
    """Google Chat platform request verification failed."""


@dataclass(frozen=True)
class VerifiedGoogleChatRequest:
    issuer: str | None
    audience: str
    subject: str | None
    email: str | None
    expires_at: int | None
    auth_mode: Literal["endpoint_url", "project_number"]


def _bearer(headers: Mapping[str, str]) -> str:
    value = headers.get("authorization") or headers.get("Authorization")
    if not value or not value.startswith("Bearer "):
        raise GoogleChatAuthError("missing Google Chat bearer token")
    token = value.removeprefix("Bearer ").strip()
    if not token:
        raise GoogleChatAuthError("empty Google Chat bearer token")
    return token


def verify_google_chat_request(
    *,
    headers: Mapping[str, str],
    mode: Literal["endpoint_url", "project_number"],
    audiences: list[str],
    oidc_verifier: Callable[[str, str], dict] | None = None,
    jwt_verifier: Callable[[str, str], dict] | None = None,
) -> VerifiedGoogleChatRequest:
    token = _bearer(headers)
    if not audiences:
        raise GoogleChatAuthError("google_chat.allowed_audiences is empty")
    for audience in audiences:
        claims = _verify_one(token, mode, audience, oidc_verifier, jwt_verifier)
        if claims is not None:
            return claims
    raise GoogleChatAuthError("Google Chat token audience mismatch")


def _verify_one(
    token: str,
    mode: Literal["endpoint_url", "project_number"],
    audience: str,
    oidc_verifier: Callable[[str, str], dict] | None,
    jwt_verifier: Callable[[str, str], dict] | None,
) -> VerifiedGoogleChatRequest | None:
    try:
        if mode == "endpoint_url":
            verify = oidc_verifier or _default_oidc_verifier
            claims = verify(token, audience)
            issuer = claims.get("iss")
            if issuer not in {"https://accounts.google.com", "accounts.google.com"}:
                return None
            if claims.get("email") != "chat@system.gserviceaccount.com":
                return None
            if not claims.get("email_verified", False):
                return None
        else:  # mode == "project_number"
            verify = jwt_verifier or _default_chat_jwt_verifier
            claims = verify(token, audience)
            if claims.get("iss") != "chat@system.gserviceaccount.com":
                return None
        if claims.get("aud") != audience:
            return None
        return VerifiedGoogleChatRequest(
            issuer=claims.get("iss"),
            audience=audience,
            subject=claims.get("sub"),
            email=claims.get("email"),
            expires_at=claims.get("exp"),
            auth_mode=mode,
        )
    except NotImplementedError:
        # A misconfigured project_number deployment with no injected
        # `jwt_verifier` must surface loudly, not be misreported as
        # "audience mismatch". Programming bugs (NameError, AttributeError)
        # are likewise re-raised so they don't hide behind a silent miss.
        raise
    except (NameError, AttributeError):
        raise
    except Exception as exc:
        # Any verifier exception or claim shape mismatch yields a soft
        # miss so the caller can try the next allowed audience. The
        # outer `verify_google_chat_request()` raises `GoogleChatAuthError`
        # only when every audience has been exhausted.
        logger.debug("_verify_one soft miss for audience %r: %s", audience, exc)
        return None


def _default_oidc_verifier(token: str, audience: str) -> dict:
    from google.auth.transport import requests as _grequests  # noqa: PLC0415
    from google.oauth2 import id_token as _id_token  # noqa: PLC0415

    return _id_token.verify_oauth2_token(token, _grequests.Request(), audience)


def _default_chat_jwt_verifier(token: str, audience: str) -> dict:
    # See https://developers.google.com/workspace/chat/authenticate-authorize-chat-app
    raise NotImplementedError(
        "project_number JWT verification requires fetching Google Chat public certs; "
        "v1 deployments should use endpoint_url mode or inject jwt_verifier"
    )
