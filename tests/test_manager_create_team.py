from __future__ import annotations

from pathlib import Path


def test_persona_keyboard_lists_discovered_personas(tmp_path):
    from link_project_to_chat.manager.bot import _build_persona_keyboard

    # Create fake personas (path layout matches what load_personas() expects)
    personas_dir = tmp_path / ".claude" / "personas"
    personas_dir.mkdir(parents=True)
    (personas_dir / "developer.md").write_text("# Developer")
    (personas_dir / "tester.md").write_text("# Tester")

    kb = _build_persona_keyboard(tmp_path, callback_prefix="team_persona_mgr")
    buttons = [btn for row in kb.inline_keyboard for btn in row]
    labels = {btn.text for btn in buttons}
    # Assert at LEAST our two test personas appear (load_personas may also discover globals)
    assert "developer" in labels
    assert "tester" in labels
    # Callbacks are prefixed
    for btn in buttons:
        assert btn.callback_data.startswith("team_persona_mgr:")
