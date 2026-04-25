# Spec #0 Review-Fix — Open Follow-Ups

Tracker for items deferred or surfaced during execution of `docs/superpowers/plans/2026-04-25-transport-spec0-review-fixes.md` (PR #6 on `rezochika/link-project-to-chat`).

Categorized by spec milestone:
- **F**: cleanup follow-ups from this PR's reviews (small, optional) — ✅ closed in `4a0bb69`
- **A**: re-targeted to spec #1 (Web UI) — initially tagged for spec #0a, but #0a (group/team port) shipped without addressing them. Web UI is the first transport that actually needs string-typed user IDs and per-transport group IDs.
- **C**: ✅ closed by spec #0c (manager port) — only the documented residue (`cli.py:696 run_polling`, manager-lockout allowlist) remains, deferred to a future "Conversation primitive" spec.

---

## F — Cleanup follow-ups from PR #6 reviews

### F1 — Soften filename sanitizer's dotfile rejection

**Source:** Task 1 (C1) code-quality review, finding I-2.
**File:** `src/link_project_to_chat/transport/telegram.py` — `_safe_basename` (~line 55).
**Severity:** Low (UX, not security).

The sanitizer rejects every filename starting with `.` and substitutes `"document"` / `"audio"`. Path-traversal protection only requires rejecting `"."` and `".."` after `PurePath(...).name` extraction; legitimate dotfiles like `.bashrc`, `.gitignore`, `.env.example` are not traversal vectors.

**Suggested change:**
```python
if not candidate or candidate in (".", ".."):
    return fallback
return candidate
```

Plan said "reject all dotfile-like names that could shadow OS paths" — the threat model overstated the risk. A standalone PR can soften this without re-opening C1.

---

### F2 — `bot.build()` should return `None`

**Source:** Task 6 (I4) code-quality review, suggestion 1.
**File:** `src/link_project_to_chat/bot.py:1974`.
**Severity:** Trivial.

`build()` still does `return app` (the PTB Application) for backward compat, but no caller in `src/` or `tests/` reads the return value after the I4 cleanup. The leak is dormant. Drop the `return` and change the annotation to `-> None`.

```bash
grep -n "= bot.build()\|=bot.build()\|build()\." src/ tests/  # confirm nothing reads the return
```

---

### F3 — Document `TelegramTransport.start()` vs `run()` dual entry

**Source:** Task 6 (I4) code-quality review, suggestion 2.
**File:** `src/link_project_to_chat/transport/telegram.py` — `run()` docstring.
**Severity:** Trivial.

`start()` and `run()` are both legitimate entry points and both invoke `post_init` once via the `_post_init_ran` guard. Add a sentence to the `run()` docstring noting this and the `start()`-then-`run()` ordering invariant.

---

## A — Re-targeted to spec #1 (Web UI)

> **Re-tag note (2026-04-25):** Originally listed under spec #0a. Spec #0a (group/team port, `2026-04-21-transport-group-team-port-design.md`) has since shipped without addressing these — they don't block the Telegram-only path, so #0a closed cleanly. They become load-bearing the moment a non-Telegram transport ships. Spec #1 (Web UI) is the natural new home since it's the first transport with string-only user IDs and per-transport group identity.

### A1 — Migrate `_trusted_users` persistence to string identity ids

**Source:** Task 5 (I3) code-quality review, finding I-2; deliberately documented in `_auth_identity` docstring.
**Files:** `src/link_project_to_chat/_auth.py:_trust_user`, `src/link_project_to_chat/config.py:bind_trusted_user`/`bind_project_trusted_user` (lines ~802, ~827).
**Severity:** Medium — blocks Discord/Slack/Web user trust persistence.

In-memory trust now tolerates non-numeric `native_id` (PR #6 commit `0ad608e`). But `bind_trusted_user` and `bind_project_trusted_user` in `config.py` still do `trusted_users[normalized] = int(user_id)` unconditionally. A real Discord/Slack user matching the username allowlist would crash with `ValueError` on first contact when persistence runs.

**Scope:**
- Drop `int(user_id)` cast in `config.py` write path; persist as string.
- Update `_trusted_user_id` (legacy singular int field) — either widen the type or migrate consumers.
- Add migration test: round-trip a non-numeric id through save → load → trust check.

---

### A2 — Replace `int(incoming.chat.native_id)` casts for `group_chat_id`

**Source:** Task 5 (I3) plan note; final-verification grep.
**File:** `src/link_project_to_chat/bot.py:645, 653, 1272, 1289`.
**Severity:** Medium — blocks Discord/Slack/Web group support.

Four remaining `int(.native_id)` casts compare incoming chat id to the config-stored `self.group_chat_id` (Telegram-int). Out of scope for spec #0 because the config schema for groups is Telegram-specific.

**Scope:**
- Add transport-aware group identity to config schema (e.g., `group_id: str` keyed by `transport_id`).
- Replace 4 call sites with platform-neutral comparison.
- Backwards-compat read path for existing int-typed `group_chat_id` in user configs.

---

### A3 — Manager `_guard` legacy int path vs `_guard_invocation` string path

**Source:** Final integration review, "Documented deferrals" note.
**File:** `src/link_project_to_chat/manager/bot.py` — legacy `_guard(update)` and `_guard_invocation`.
**Severity:** Low — currently mutually exclusive in practice.

`_guard` (legacy update path) still calls `self._rate_limited(user.id)` with `user.id` as int. `_guard_invocation` uses string keys. They share `_rate_limits` dict, producing mixed-key state for the same user if both fire. Currently disjoint by handler routing, but a future refactor could collide them.

**Scope:** retire `_guard(update)` when the residual `Update`-based handlers are migrated as part of the future "Conversation primitive" spec (which will close the remaining `tests/test_manager_lockout.py` allowlist).

---

## C — ✅ Closed by spec #0c (manager port)

### C1 — Port `manager/bot.py` to the Transport Protocol — ✅ Closed

**Status:** Closed by spec #0c (`docs/superpowers/specs/2026-04-21-transport-manager-port-design.md`). Manager now runs through `TelegramTransport`; `manager/telegram_group.py` was relocated to `transport/_telegram_group.py` (commit `6702fac`); 11 commands ported via `transport.on_command`; wizards ported (`0188ac4`, `381a8cf`, `cb2036d`, `575b99e`); `tests/test_manager_lockout.py` pins the residual telegram-import allowlist. pyproject bumped to **0.16.0** (`10521b2`).

**Residue (intentional, allowlisted):**
- `cli.py:696` calls `bot.build().run_polling()`. Spec #0c §2 explicitly defers the last `Update`/`ConversationHandler` references until a future "Conversation primitive" abstraction lands.
- `manager/bot.py` attaches `_post_init`/`_post_stop` to the underlying Application because the manager owns its lifecycle via `run_polling`. Documented inline.
- The 6-name `telegram.ext` import allowlist in `tests/test_manager_lockout.py` (`Update`, `ConversationHandler`, `ContextTypes`, `MessageHandler`, `CommandHandler`, `CallbackQueryHandler`, `filters`) is intentional surface preserved for the as-yet-unwritten Conversation primitive spec.

---

## Status as of 2026-04-25 (re-verified after spec #0a/#0c closure)

| Item | Severity | Owner | Status |
|---|---|---|---|
| F1 dotfile policy | Low | — | ✅ closed in `4a0bb69` |
| F2 `build() -> None` | Trivial | — | ✅ closed in `4a0bb69` |
| F3 docstring note | Trivial | — | ✅ closed in `4a0bb69` |
| A1 trust persistence | Medium | spec #1 (Web UI) | open — load-bearing once a non-Telegram transport ships |
| A2 group_chat_id | Medium | spec #1 (Web UI) | open — same trigger as A1 |
| A3 manager `_guard` int | Low | spec #1 / Conversation primitive | open — disjoint in practice today |
| C1 manager transport port | — | spec #0c | ✅ closed (manager runs through `TelegramTransport`; residue allowlisted) |
