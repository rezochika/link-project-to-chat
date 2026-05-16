from __future__ import annotations

import pytest

from link_project_to_chat.google_chat.auth import (
    GoogleChatAuthError,
    VerifiedGoogleChatRequest,
    verify_google_chat_request,
)


def test_missing_authorization_header_rejected():
    with pytest.raises(GoogleChatAuthError):
        verify_google_chat_request(headers={}, mode="endpoint_url", audiences=["https://x.test/google-chat/events"])


def test_non_bearer_authorization_header_rejected():
    with pytest.raises(GoogleChatAuthError):
        verify_google_chat_request(
            headers={"authorization": "Basic abc"},
            mode="endpoint_url",
            audiences=["https://x.test/google-chat/events"],
        )


def test_endpoint_url_claims_are_accepted_with_injected_verifier():
    def verifier(token: str, audience: str) -> dict:
        return {
            "iss": "https://accounts.google.com",
            "aud": audience,
            "email": "chat@system.gserviceaccount.com",
            "email_verified": True,
            "sub": "chat",
            "exp": 1770000000,
        }

    verified = verify_google_chat_request(
        headers={"authorization": "Bearer token"},
        mode="endpoint_url",
        audiences=["https://x.test/google-chat/events"],
        oidc_verifier=verifier,
    )

    assert verified == VerifiedGoogleChatRequest(
        issuer="https://accounts.google.com",
        audience="https://x.test/google-chat/events",
        subject="chat",
        email="chat@system.gserviceaccount.com",
        expires_at=1770000000,
        auth_mode="endpoint_url",
    )


def test_project_number_claims_are_accepted_with_injected_verifier():
    def verifier(token: str, audience: str) -> dict:
        return {"iss": "chat@system.gserviceaccount.com", "aud": audience, "sub": "chat", "exp": 1770000000}

    verified = verify_google_chat_request(
        headers={"authorization": "Bearer token"},
        mode="project_number",
        audiences=["123"],
        jwt_verifier=verifier,
    )

    assert verified.issuer == "chat@system.gserviceaccount.com"
    assert verified.audience == "123"
    assert verified.auth_mode == "project_number"
