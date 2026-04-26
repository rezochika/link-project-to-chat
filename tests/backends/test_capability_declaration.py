from link_project_to_chat.backends.codex import CODEX_CAPABILITIES


def test_codex_capabilities_match_validated_findings():
    assert tuple(CODEX_CAPABILITIES.models) == ()
    assert CODEX_CAPABILITIES.supports_thinking is False
    assert CODEX_CAPABILITIES.supports_permissions is False
    assert CODEX_CAPABILITIES.supports_resume is True
    assert CODEX_CAPABILITIES.supports_compact is False
    assert CODEX_CAPABILITIES.supports_allowed_tools is False
    assert CODEX_CAPABILITIES.supports_usage_cap_detection is False
