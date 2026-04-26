"""Lockout: bot.py must route Claude-specific attribute access through the
tier-2 ``self._claude`` accessor rather than reaching into the backend
through ``self.task_manager.backend`` or any local alias of it.

The enforcement is AST-based (not substring-based) because an alias-bypass
pattern such as::

    bd = self.task_manager.backend
    bd.effort = "high"

evades a simple ``.claude.`` substring check. The AST walker collects every
local variable assigned from ``self.task_manager.backend`` (transitively)
and flags any attribute access on those aliases — plus direct
``self.task_manager.backend.X`` access — whose attribute name is in the
tier-2 (Claude-specific) set.
"""
from __future__ import annotations

import ast
from pathlib import Path

BOT_PY = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "link_project_to_chat"
    / "bot.py"
)

# Attributes that live on ClaudeBackend but are NOT part of the AgentBackend
# Protocol. Access to these must go through ProjectBot._claude.
#
# Note: ``effort`` was originally tier-2 (Claude-only), but Phase 4 promoted
# it to the Protocol (Codex also supports reasoning effort), so it's now a
# tier-1 attribute and must NOT appear in this set.
TIER2_ATTRS = frozenset({
    "permission_mode",
    "skip_permissions",
    "allowed_tools",
    "disallowed_tools",
    "append_system_prompt",
    "team_system_note",
    "show_thinking",
})


def _is_self_task_manager_backend(node: ast.AST) -> bool:
    """True iff *node* is literally the expression ``self.task_manager.backend``."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "backend"
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "task_manager"
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "self"
    )


def _collect_backend_aliases(tree: ast.AST) -> set[str]:
    """Return every local name transitively assigned from ``self.task_manager.backend``."""
    aliases: set[str] = set()
    # Iterate to fixed point so chained aliases (a = self.task_manager.backend;
    # b = a; c = b) are all captured regardless of source order.
    while True:
        before = set(aliases)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            source_is_alias = _is_self_task_manager_backend(node.value) or (
                isinstance(node.value, ast.Name) and node.value.id in aliases
            )
            if not source_is_alias:
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    aliases.add(target.id)
        if aliases == before:
            return aliases


def _find_tier2_violations(source: str) -> list[str]:
    """Return a list of ``"<access> at line <N>"`` strings for every tier-2 leak."""
    tree = ast.parse(source)
    aliases = _collect_backend_aliases(tree)
    violations: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Attribute) and node.attr in TIER2_ATTRS):
            continue
        if _is_self_task_manager_backend(node.value):
            violations.append(f"self.task_manager.backend.{node.attr} at line {node.lineno}")
        elif isinstance(node.value, ast.Name) and node.value.id in aliases:
            violations.append(f"{node.value.id}.{node.attr} at line {node.lineno}")
    return violations


def test_bot_does_not_construct_claude_client_directly():
    """Direct construction of ClaudeClient/ClaudeBackend bypasses the factory
    registry and defeats the capability-gating plan for Phase 2."""
    source = BOT_PY.read_text(encoding="utf-8")
    assert "ClaudeClient(" not in source
    assert "ClaudeBackend(" not in source


def test_bot_does_not_leak_tier2_attrs_through_backend_or_alias():
    """Tier-2 attributes (effort, permission_mode, ...) must be accessed via
    self._claude, not through self.task_manager.backend or an alias of it.
    Catches both the direct form and the alias-bypass pattern."""
    source = BOT_PY.read_text(encoding="utf-8")
    violations = _find_tier2_violations(source)
    assert not violations, (
        "Tier-2 Claude-specific attribute leaks detected in bot.py:\n  - "
        + "\n  - ".join(violations)
    )


# ---------------------------------------------------------------------------
# Self-tests for the AST checker — prove it catches the bypass pattern.
# ---------------------------------------------------------------------------


def test_ast_checker_flags_direct_backend_tier2_access():
    source = (
        "class Bot:\n"
        "    def f(self):\n"
        "        self.task_manager.backend.permission_mode = 'dontAsk'\n"
    )
    violations = _find_tier2_violations(source)
    assert any("permission_mode" in v for v in violations)


def test_ast_checker_flags_single_hop_alias_bypass():
    """``bd = self.task_manager.backend; bd.permission_mode`` must be caught."""
    source = (
        "class Bot:\n"
        "    def f(self):\n"
        "        bd = self.task_manager.backend\n"
        "        bd.permission_mode = 'dontAsk'\n"
    )
    violations = _find_tier2_violations(source)
    assert any("bd.permission_mode" in v for v in violations)


def test_ast_checker_flags_chained_alias_bypass():
    """Transitive aliases (bd → c → ...) must still be caught."""
    source = (
        "class Bot:\n"
        "    def f(self):\n"
        "        bd = self.task_manager.backend\n"
        "        c = bd\n"
        "        c.permission_mode = 'dontAsk'\n"
    )
    violations = _find_tier2_violations(source)
    assert any("permission_mode" in v for v in violations)


def test_ast_checker_allows_self_claude_tier2_access():
    """Tier-2 access through self._claude is the sanctioned path."""
    source = (
        "class Bot:\n"
        "    def f(self):\n"
        "        self._claude.append_system_prompt = 'p'\n"
        "        self._claude.permission_mode = 'dontAsk'\n"
    )
    violations = _find_tier2_violations(source)
    assert violations == []


def test_ast_checker_allows_tier1_backend_access():
    """Protocol attributes (model, session_id, status) on the backend are fine."""
    source = (
        "class Bot:\n"
        "    def f(self):\n"
        "        _ = self.task_manager.backend.model\n"
        "        _ = self.task_manager.backend.session_id\n"
        "        _ = self.task_manager.backend.status\n"
    )
    violations = _find_tier2_violations(source)
    assert violations == []
