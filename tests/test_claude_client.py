"""Tests for _sanitize_error() in claude_client."""
from pathlib import Path

from link_project_to_chat.backends.claude import ClaudeBackend as ClaudeClient, _sanitize_error


def test_truncates_long_errors():
    long_input = "x" * 1000
    result = _sanitize_error(long_input)
    assert len(result) <= 203  # 200 chars + "..."
    assert result.endswith("...")


def test_takes_first_line_only():
    multi = "First line error\nSecond line with /secret/path\nThird line"
    result = _sanitize_error(multi)
    assert result == "First line error"


def test_redacts_api_key_patterns():
    text = "Authentication failed: sk-proj-abc123XYZsecretkey"
    result = _sanitize_error(text)
    assert "sk-proj-***" in result
    assert "abc123XYZsecretkey" not in result


def test_empty_message():
    assert _sanitize_error("") == "Unknown error"
    assert _sanitize_error("   ") == "Unknown error"
    assert _sanitize_error("\n\n") == "Unknown error"


def test_preserves_short_clean_errors():
    msg = "Connection refused"
    assert _sanitize_error(msg) == msg


def test_telegram_awareness_in_command():
    client = ClaudeClient(project_path=Path("/tmp"))
    cmd = client._build_cmd()
    assert "--append-system-prompt" in cmd
    prompt = cmd[cmd.index("--append-system-prompt") + 1]
    # Covers all four scopes: identity, output style, user commands, fragility.
    assert "link-project-to-chat" in prompt
    assert "MarkdownV2" in prompt
    assert "/run" in prompt and "/effort" in prompt
    assert "CHANNEL FRAGILITY" in prompt


def test_team_system_note_injected_into_append_system_prompt():
    """When set, team_system_note rides alongside the Telegram awareness preamble."""
    client = ClaudeClient(project_path=Path("/tmp"))
    client.team_system_note = "Your peer is @acme_dev_bot."
    prompt = client._build_cmd()[client._build_cmd().index("--append-system-prompt") + 1]
    assert "@acme_dev_bot" in prompt


def test_partial_messages_flag_is_passed():
    """Without --include-partial-messages the CLI only emits a final assistant
    event per turn, so thinking arrives in one block at the end instead of
    live. parse_stream_line relies on the stream_event deltas this flag enables.
    """
    client = ClaudeClient(project_path=Path("/tmp"))
    assert "--include-partial-messages" in client._build_cmd()


def test_team_system_note_survives_active_skill():
    """A later /use <skill> sets append_system_prompt; team note must still be present."""
    client = ClaudeClient(project_path=Path("/tmp"))
    client.team_system_note = "Peer: @acme_dev_bot."
    client.append_system_prompt = "# Custom skill content\nRemember: haiku every reply."
    cmd = client._build_cmd()
    prompt = cmd[cmd.index("--append-system-prompt") + 1]
    assert "@acme_dev_bot" in prompt
    assert "haiku every reply" in prompt
