from link_project_to_chat.backends.codex import CODEX_CAPABILITIES


def test_codex_capabilities_match_validated_findings():
    # Phase 4 promoted models + reasoning effort: Codex now declares both,
    # mirroring `~/.codex/models_cache.json` priority order and the four
    # effort levels accepted by `-c model_reasoning_effort=...` (no `max`).
    assert tuple(CODEX_CAPABILITIES.models) == (
        "gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex", "gpt-5.2",
    )
    assert CODEX_CAPABILITIES.supports_thinking is False
    assert CODEX_CAPABILITIES.supports_permissions is True
    assert CODEX_CAPABILITIES.supports_resume is True
    assert CODEX_CAPABILITIES.supports_compact is False
    assert CODEX_CAPABILITIES.supports_allowed_tools is False
    assert CODEX_CAPABILITIES.supports_usage_cap_detection is False
    assert CODEX_CAPABILITIES.supports_effort is True
    assert tuple(CODEX_CAPABILITIES.effort_levels) == ("low", "medium", "high", "xhigh")
