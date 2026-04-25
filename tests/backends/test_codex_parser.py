from pathlib import Path

from link_project_to_chat.backends.codex_parser import parse_codex_line
from link_project_to_chat.events import TextDelta

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def _fixture_lines(name: str) -> list[str]:
    return (FIXTURES / name).read_text(encoding="utf-8").splitlines()


def test_thread_started_yields_session_metadata():
    parsed = parse_codex_line(_fixture_lines("codex_exec_ok.jsonl")[0])

    assert parsed.thread_id == "019dc702-1602-7381-a86f-94950237eab4"
    assert parsed.turn_completed is False
    assert parsed.events == []


def test_agent_message_yields_text_delta():
    parsed = parse_codex_line(_fixture_lines("codex_exec_ok.jsonl")[2])

    assert parsed.events == [TextDelta(text="OK")]
    assert parsed.thread_id is None


def test_turn_completed_preserves_usage():
    parsed = parse_codex_line(_fixture_lines("codex_exec_ok.jsonl")[3])

    assert parsed.turn_completed is True
    assert parsed.usage is not None
    assert "input_tokens" in parsed.usage
    assert "cached_input_tokens" in parsed.usage
    assert "output_tokens" in parsed.usage
    assert "reasoning_output_tokens" in parsed.usage


def test_non_json_stderr_line_is_ignored():
    parsed = parse_codex_line(
        "2026-04-23T10:23:58.058856Z  WARN codex_core::plugins::startup_sync: startup remote plugin sync failed"
    )

    assert parsed.events == []
    assert parsed.thread_id is None
    assert parsed.turn_completed is False


def test_resume_fixture_reuses_thread_id():
    parsed = parse_codex_line(_fixture_lines("codex_resume_ok.jsonl")[0])

    assert parsed.thread_id == "019dc702-1602-7381-a86f-94950237eab4"


def test_resume_agent_message_text():
    parsed = parse_codex_line(_fixture_lines("codex_resume_ok.jsonl")[2])

    assert parsed.events == [TextDelta(text="AGAIN")]
