# Team-Mode Safety Model — Design Spec

**Status:** Draft (2026-05-15). Not yet implemented.
**Date:** 2026-05-15
**Supersedes:** Augments `2026-04-17-dual-agent-ai-team-design.md` (Phase 2 safety rails). Replaces the loop-only cap with a unified authority + bounded-autonomy model.
**Motivating incidents:** autonomous quota burn on 2026-04-27; unauthorized push of `080fa9d` and `eba56c3` to `origin/feat/plugin-system` on 2026-05-15.

---

## 1. Overview

Team mode currently grants each bot subprocess the same shell/tool authority a single-bot session has. Bot-to-bot messages can therefore drive irreversible external operations (`git push`, `gh ...`, package publish, etc.) without the human in the loop, and per-turn re-execution of persona instructions causes review loops that burn quota with no productive output.

This design introduces a coherent team-mode safety model with three principles:

1. **Bots are not equivalent to the user.** A bot's chat output cannot grant authority for an external side effect. Authority comes only from messages whose `from_id` matches the authenticated user.
2. **Authority is scoped, time-bounded, and traceable.** Every grant is `(user_message_id, scopes, granted_at)`. Every side-effecting tool invocation checks against active grants and the call site records the grant it consumed.
3. **Autonomy is bounded per user prompt.** Each user message resets a consecutive-bot-turn counter; the relay auto-pauses when the counter hits `TeamConfig.max_autonomous_turns` (default 5). Existing same-author-streak and rolling-window caps stay as defense in depth.

The codex per-turn re-injection issue characterized in the user-memory note `project_codex_prompt_structure.md` (root cause of the manager review loop on both 2026-04-27 and 2026-05-15) is fixed by moving prompt composition into the backend so codex can skip persona/team-note/history re-injection on session resume.

## 2. Goals & non-goals

**Goals**
- Make `git push`, `gh pr create`, `gh release create`, and generic network ops impossible-by-construction in team mode without an explicit `--auth <scope>` directive from the authenticated user.
- Bound autonomous bot-to-bot activity to a configurable turn budget per team.
- Eliminate the codex prompt re-injection that drives review loops.
- Drop verbatim duplicate forwards at the relay before they reach the peer bot.
- Preserve backwards compatibility: existing teams in `config.json` get strict-safety defaults via migration, no opt-in step.

**Non-goals (v1)**
- Not addressing skill-discovery flakiness on team-bot first turn (separate investigation).
- Not addressing the streaming-edit ordering race in `_record_round` (needs logging-only diagnosis first).
- Not introducing a per-tool denylist for codex (codex CLI exposes none; we use sandbox downgrade instead).
- Not retroactively unwinding `eba56c3` / `080fa9d` from `origin/feat/plugin-system` — that's a human decision.

## 3. Decisions driving this design

Outcomes of the brainstorming Q&A on 2026-05-15:

| # | Decision |
|---|---|
| 1 | Hybrid posture: strict (no irreversible without auth) AND bounded (auto-pause after N turns) |
| 2 | Authorization via inline `--auth <scope>` directive in user @mention |
| 3 | Autonomy budget is per-team configurable, default 5 |
| 4 | Closed scope vocabulary: `push`, `pr_create`, `release`, `network`, `all` |
| 5 | Authority grants have a 10-minute TTL |
| 6 | Codex prompt composition moves into the backend so resume can skip re-injection |
| 7 | Claude uses `is_authorized` (session-grant within TTL); codex uses `consume_grant` (one-shot per turn) for sandbox elevation |
| 8 | Backwards compatible: defaults applied via existing config migration |

## 4. Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│ User message arrives in team group                               │
└────────────────────────────┬─────────────────────────────────────┘
                             │
                             ▼
                  ┌──────────────────────┐
                  │ TeamRelay            │
                  │  ─ on_user_message() │ ──► parse --auth directives
                  │                      │      record grant in TeamAuthority
                  │                      │      reset bot-turn counter
                  └──────────┬───────────┘
                             │
                             ▼ (forward to peer bot if @peer prefixed)
                  ┌──────────────────────┐
                  │ Peer ProjectBot       │
                  │  builds prompt via    │
                  │  backend.compose_     │
                  │  user_prompt()        │
                  └──────────┬───────────┘
                             │
                             ▼
                  ┌──────────────────────┐      ┌──────────────────┐
                  │ Claude backend       │      │ Codex backend    │
                  │  --disallowedTools   │      │  --full-auto OR  │
                  │  augmented per       │      │  --dangerously-  │
                  │  team_authority      │      │  bypass (one     │
                  │                      │      │  consumed grant) │
                  └──────────┬───────────┘      └────────┬─────────┘
                             │                            │
                             └────────────┬───────────────┘
                                          │
                                          ▼
                            ┌──────────────────────────┐
                            │ Bot turn output relayed  │
                            │  ─ content-hash dedupe   │
                            │  ─ increment turn counter│
                            │     → halt if at budget  │
                            └──────────────────────────┘
```

State ownership:
- Each `ProjectBot` process owns its own `TeamAuthority` instance (manager and dev have separate copies).
- State stays synchronized via chat-as-source-of-truth: both bots' message handlers observe every group message from the authenticated user and call `team_authority.record_user_message(msg_id, text)` independently. The Telegram group is the canonical event log.
- The autonomous-turn counter lives entirely on `TeamRelay` (not `TeamAuthority`) because forward-counting is the relay's concern. The relay increments its own counter on each forward, halts when it reaches `max_autonomous_turns`, and resets to 0 on observing any user message. This keeps `TeamAuthority` as a pure grant store.
- Each `AgentBackend` holds a reference to its bot's `TeamAuthority` via a new Protocol field, queried at command-build time.
- The known divergence point: `consume_grant` burns the grant in the bot that calls it but leaves the other bot's copy alive. In practice only the user-@mentioned bot invokes irreversible ops, so the maximum cross-process drift is "two pushes per `--auth push` grant within TTL" — bounded and acceptable for v1.

## 5. Component: `team_safety.py`

New module at `src/link_project_to_chat/team_safety.py`. Contains the data layer.

### 5.1 Scope vocabulary

Closed `frozenset` of scope names. Unknown tokens in `--auth <token>` are silently dropped at parse time so typos and future-token leakage don't accidentally grant authority.

```python
VALID_SCOPES: Final[frozenset[str]] = frozenset({
    "push",       # git push, gh pr merge
    "pr_create",  # gh pr create
    "release",    # gh release create, package publish
    "network",    # generic outbound network (curl POST, fetch)
    "all",        # wildcard
})
```

### 5.2 Directive parser

Single regex anchored to whitespace boundaries:

```python
_AUTH_DIRECTIVE_RE = re.compile(r"(?:^|\s)--auth\s+([a-z_]+)(?=\s|$)")

def parse_auth_directives(text: str) -> frozenset[str]:
    if not text:
        return frozenset()
    return frozenset(s for s in _AUTH_DIRECTIVE_RE.findall(text) if s in VALID_SCOPES)
```

Boundary anchoring prevents matching `--authentication`, `--auth-mode`, etc.

### 5.3 `AuthorityGrant`

Immutable dataclass:
```python
@dataclass(frozen=True)
class AuthorityGrant:
    user_message_id: int
    scopes: frozenset[str]
    granted_at: float   # time.monotonic()
    def covers(self, scope: str) -> bool:
        return "all" in self.scopes or scope in self.scopes
    def is_expired(self, now: float, ttl: float = 600.0) -> bool:
        return (now - self.granted_at) > ttl
```

### 5.4 `TeamAuthority`

Per-team state holder. Methods are not thread-safe (called from a single asyncio task in the relay).

```python
class TeamAuthority:
    def __init__(self, team_name: str) -> None: ...
    def record_user_message(self, msg_id: int, text: str) -> frozenset[str]: ...
    def is_authorized(self, scope: str) -> bool: ...
    def consume_grant(self, scope: str) -> AuthorityGrant | None: ...
    @property
    def status_snapshot(self) -> dict: ...
```

Key semantics:
- `record_user_message`: parse directives, store grant if any, return granted scopes (for logging). Also signals the relay to reset its bot-turn counter (via callback or direct relay reference, decided at impl time).
- `is_authorized` vs. `consume_grant`: inspect-only vs. burn-after-use. Inspect for repeated-safe ops (PR comments), consume for one-shot irreversibles (push, release, sandbox elevation).
- `_grants` is a `deque(maxlen=4)` — old grants drop out automatically.
- Bot-turn counting is **not** on `TeamAuthority` — it's on `TeamRelay` (forwards are the relay's concern). The cap check happens in the relay's forward path and reads `TeamConfig.max_autonomous_turns` from the relay's own state.

## 6. Component: relay enforcement

Changes to `src/link_project_to_chat/transport/_telegram_relay.py`.

### 6.1 `TeamAuthority` integration

`TeamRelay.__init__` accepts a `team_authority: TeamAuthority` parameter (the same instance the bot's backend uses) and a `max_autonomous_turns: int` parameter from `TeamConfig`. The relay observes group messages via its existing Telethon hooks and forwards user-message details to `team_authority.record_user_message`. On any non-bot message from the authenticated user, the relay also resets its own bot-turn counter to 0.

```python
async def _on_user_message_observed(self, msg_id: int, from_id: int, text: str) -> None:
    if from_id != self._authenticated_user_id:
        return
    granted = self._team_authority.record_user_message(msg_id, text)
    self._consecutive_bot_turns = 0
    if granted:
        logger.info(
            "team-mode auth granted: team=%s scopes=%s user_msg=%s",
            self._team_name, sorted(granted), msg_id,
        )
```

### 6.2 Bot-turn budget enforcement

In the existing forward path, after passing the `_is_ack_only` check:

```python
if self._is_recent_duplicate(sender, body):
    self._record_round(sender)  # still contributes to existing streak cap
    logger.debug("team-relay: dropped duplicate forward from @%s", sender)
    return
self._consecutive_bot_turns += 1
if self._consecutive_bot_turns >= self._max_autonomous_turns:
    self._halted = True
    await self._send_halt_notice(reason="bot_turn_budget_exhausted")
    return
await self._relay(sender, body)
```

### 6.3 Content-hash dedupe

```python
def _is_recent_duplicate(self, sender: str, body: str) -> bool:
    h = hashlib.sha256(body.strip().encode()).hexdigest()[:16]
    history = self._recent_forwards.setdefault(sender, deque(maxlen=4))
    now = time.monotonic()
    while history and (now - history[0][1]) > 60.0:
        history.popleft()
    if any(prev_h == h for prev_h, _ in history):
        return True
    history.append((h, now))
    return False
```

Catches the 2026-05-15 loop shape where the manager re-sent byte-identical "Request changes" payloads before the same-author-streak cap fired.

### 6.4 Halt notice wording

Replace the misleading `"3 consecutive forwards from @{last_sender} with no peer reply between"` line at `_send_halt_notice`. New wording:

- For same-author streak: `"3 consecutive forwards from @{last_sender} within {window}s"`
- For dedupe-driven halt: `"bot turn budget exhausted: {n} bot-to-bot turns since last user message"`

No claim about peer-reply ordering — the streak rule doesn't actually check that.

## 7. Component: backend enforcement

### 7.1 Protocol change

Add to `AgentBackend` Protocol in `src/link_project_to_chat/backends/base.py`:
```python
team_authority: TeamAuthority | None
```

Set/cleared by `bot.py:_refresh_team_system_note` alongside `team_system_note`. Same gate, same lifecycle.

### 7.2 Claude backend

In `claude.py:_build_cmd`, augment `--disallowedTools` when team_system_note is set:

```python
effective_disallowed = list(self.disallowed_tools)
if self.team_system_note:
    effective_disallowed.extend(_team_blocks_for(self.team_authority))
if effective_disallowed:
    cmd.extend(["--disallowedTools", ",".join(effective_disallowed)])
```

`_team_blocks_for(authority)` returns:
- Static block list: `Bash(git push:*)`, `Bash(git push)`, `Bash(gh pr create:*)`, `Bash(gh pr merge:*)`, `Bash(gh release create:*)`, `Bash(gh workflow run:*)`
- Removes blocks for scopes covered by `authority.is_authorized(scope)`

Inspect-only semantics. Within a 10-minute TTL after `--auth push`, repeated `git push` is allowed. The autonomous-turn cap (5) is the second-line guard against runaway re-use.

### 7.3 Codex backend

In `codex.py:_permission_args`:

```python
def _permission_args(self) -> list[str]:
    mode = self.permissions
    if self.team_system_note and mode in ("dangerously-skip-permissions", "bypassPermissions"):
        if self.team_authority and self.team_authority.consume_grant("network"):
            return ["--dangerously-bypass-approvals-and-sandbox"]
        return ["--full-auto"]   # workspace-write sandbox: writes ok, network blocked
    # ... existing branches unchanged
```

Consume semantics. Sandbox elevation is binary (on or off for the entire turn), so a single `--auth push` or `--auth network` grant is consumed when the turn invokes codex with elevated sandbox. Subsequent turns without a fresh grant return to `--full-auto`.

Surface to user: log an info message `"team-mode safety: codex sandbox kept at full-auto despite '{mode}' configured"` on the first invocation per session that triggers the downgrade. Append to `/status` output (see Section 9).

## 8. Component: codex prompt composition

### 8.1 The current re-injection problem

Codex's user message currently contains three re-injected layers each turn:
1. `team_system_note` wrapped in `<system-reminder>` ([codex.py:115-123](src/link_project_to_chat/backends/codex.py:115))
2. Persona block via `format_persona_prompt` ([bot.py:996-1000](src/link_project_to_chat/bot.py:996))
3. Conversation history block (up to 4000 chars)

On session resume, the previous turn's full prompt — including all three — is already in codex's conversation history. Re-injecting them as a new user message makes codex read them as **new instructions**, re-executing the persona's review protocol from scratch each turn. This is the documented root cause (see user-memory `project_codex_prompt_structure.md`) of the manager review loops on 2026-04-27 and 2026-05-15.

### 8.2 Fix: backend-owned prompt composition

New Protocol method on `AgentBackend`:
```python
def compose_user_prompt(
    self,
    raw_message: str,
    persona: str | None,
    history: str,
) -> str: ...
```

Bot.py becomes the data source; each backend assembles the parts per its own re-injection sensitivity.

**Claude** keeps current behavior: always include persona + history. Claude has a stable system-prompt channel via `--append-system-prompt`, so per-turn re-inclusion is cache-friendly and does not read as fresh instructions.

**Codex** tracks content hashes per injection layer:
```python
def compose_user_prompt(self, raw_message, persona, history):
    parts = []
    is_resume = self.session_id is not None
    team_hash = _hash(self.team_system_note)
    persona_hash = _hash(persona)

    if not is_resume or team_hash != self._last_team_note_hash:
        if self.team_system_note:
            parts.append(f"<system-reminder>\n{self.team_system_note}\n</system-reminder>")
        self._last_team_note_hash = team_hash

    if not is_resume or persona_hash != self._last_persona_hash:
        if persona:
            parts.append(persona)
        self._last_persona_hash = persona_hash

    if not is_resume and history:
        parts.append(history)

    parts.append(raw_message)
    return "\n\n".join(parts)
```

The hash-compare-skip pattern:
- First turn (no session_id): all three layers injected. Hashes recorded.
- Subsequent turns with unchanged persona/team-note: only `raw_message` sent. Codex retrieves persona/team-note from its own conversation history.
- Mid-session persona swap (`/use other_persona`): hash differs, new persona injected once, hash updated.
- Team-note change (peer username discovered post-`get_me()`): hash differs, new note injected once.

### 8.3 Bot.py turn-path change

Replace [bot.py:996-1000](src/link_project_to_chat/bot.py:996):
```python
# Before
prompt = format_persona_prompt(persona, prompt)
prompt = await self._history_block(chat) + prompt

# After
history = await self._history_block(chat)
persona_text = format_persona_prompt(persona, "") if persona else ""
prompt = backend.compose_user_prompt(prompt, persona_text, history)
```

Bot.py no longer concatenates blindly; each backend assembles the parts.

### 8.4 Session invalidation fallback

If codex's session is lost (CLI restart, codex's own session-eviction policy), backend.session_id is reset to None. The next turn re-injects everything because `is_resume` is False. No additional logic needed.

## 9. Cosmetic and surface fixes

### 9.1 `[No response]` placeholder suppression

Change `claude.py:333`:
```python
# Before
return result_text or "[No response]"
# After
return result_text
```

At the bot's send site, skip when stripped content is empty:
```python
text = (await backend.chat(message)).strip()
if not text:
    logger.debug("backend returned empty turn; suppressing send")
    return
await transport.send_text(chat, text)
```

The `[No response]` sentinel was a debug convenience that leaked to chat as message #68 on 2026-05-15.

### 9.2 `/status` team-safety block

When `team_system_note` is set, append:
```
Team safety: strict mode
Autonomous turn 2 / 5 (resets on next user msg)
Active grants:
  • push (45s ago, msg #312)
```
Rendered via `team_authority.status_snapshot`.

### 9.3 Halt-notice rewording

See Section 6.4.

## 10. Config and migration

`TeamConfig` in `src/link_project_to_chat/config.py` gains:
```python
@dataclass
class TeamConfig:
    # ... existing fields ...
    max_autonomous_turns: int = 5
    safety_mode: str = "strict"   # "strict" | "off" (reserved for future opt-out)
```

Migration is via the existing config-load pattern: missing fields default to the dataclass defaults. Existing teams in `config.json` get `max_autonomous_turns=5` and `safety_mode="strict"` on first load. No explicit opt-in step.

`safety_mode = "off"` is reserved for a future per-team override but is not wired in v1 — all teams operate in strict mode.

## 11. Test surface

Five new or extended test files:

| Test file | Coverage |
|---|---|
| `tests/test_team_safety.py` (new) | `parse_auth_directives` (valid + invalid + boundary cases); `TeamAuthority` state transitions; TTL expiry; `is_authorized` vs. `consume_grant` semantics; `all` wildcard; `record_user_message` reset behavior |
| `tests/transport/test_telegram_relay.py` (extend) | Content-hash dedupe (drop verbatim, dissimilar passes); bounded-autonomy cap halts at N; halt notice wording for each cap |
| `tests/test_backend_claude.py` (extend) | Team-mode disallowed-tools augment without grants (blocks present); with `push` grant (block removed); with `all` (all blocks removed) |
| `tests/test_backend_codex.py` (extend) | Sandbox downgrade in team mode under `dangerously-skip-permissions`; grant consumption flow (one elevation per grant) |
| `tests/test_codex_resume_no_reinjection.py` (new) | `compose_user_prompt` skips persona/team-note on resume when hashes match; re-injects when persona changes mid-session; injects on first turn |

The existing `tests/transport/test_contract.py` parametrized contract does not need to change — none of the safety hooks alter the `Transport` Protocol surface.

## 12. File change summary

| File | LOC est. | Risk |
|---|---|---|
| `src/link_project_to_chat/team_safety.py` (new) | ~150 | Low |
| `src/link_project_to_chat/transport/_telegram_relay.py` | +80 | Medium (forward path) |
| `src/link_project_to_chat/backends/base.py` | +5 | Low (Protocol field add) |
| `src/link_project_to_chat/backends/claude.py` | +30 | Low |
| `src/link_project_to_chat/backends/codex.py` | +50 | Medium (prompt composition refactor) |
| `src/link_project_to_chat/bot.py` | +20 | Medium (turn path) |
| `src/link_project_to_chat/config.py` | +5 | Low |
| Tests (5 files) | ~400 | — |
| **Total** | **~750** | — |

Backwards compatible: existing teams pick up strict-safety defaults via config migration.

## 13. Out of scope

Intentionally not addressed in v1:
- **Skill-discovery flakiness on team-bot first turn** — separate investigation. Manager noted "the required `using-superpowers` skill path is missing in this workspace" on the first turn of 2026-05-15's session but was able to invoke skills later. Unrelated to safety model.
- **Streaming-edit ordering race in `_record_round`** — when one bot streams slower than the other, the round-recording sequence can misorder forwards. Needs a logging-only diagnostic patch first to confirm the hypothesis.
- **Force-pushing `eba56c3` / `080fa9d` back off `origin/feat/plugin-system`** — human decision, not a code fix.
- **Per-tool denylist for codex** — codex CLI exposes no equivalent of `--disallowedTools`. We use sandbox downgrade as the lever. If codex gains a denylist surface later, this design's `is_authorized`/`consume_grant` API supports both shapes.
- **Audit log / `/audit` slash command** — visibility into autonomous activity would be useful but doesn't fix root causes. Considered and rejected in Approach C during brainstorming.

## 14. Open questions and risks

**Risk: session-lost silent regression.** If codex's CLI restarts mid-session and the bot doesn't detect it, the next turn's `is_resume=True` check will skip persona injection while codex has no in-memory history of the persona. Mitigation: detect codex CLI restart via session-id-not-found errors and reset hash state. If undetected, the symptom is "manager forgets its role for one turn, then user's next message re-injects" — degraded but not catastrophic.

**Risk: false-positive duplicates from dedupe gate.** Bots sometimes legitimately re-send near-identical messages (e.g., re-confirming a status after a query). Mitigation: hash is on `body.strip()` only — small formatting differences (trailing whitespace, newlines) don't dedupe. If we see legitimate retransmits suppressed, we can scope dedupe to messages whose `reply_to` doesn't point to a recent peer turn.

**Risk: `--auth network` is broader than necessary.** A single `--auth network` grant lets codex run any network op for the turn, not just the intended push. Mitigation v1: accept this since codex's lever is binary. Future: if codex gains per-tool denylist, narrow to `--auth push` mapping to a single git-push-only sandbox profile.

**Open question: scope inheritance across multi-turn workflows.** If user grants `--auth push` and the manager delegates "push the branch" to dev, does dev's turn see the grant? Yes — both bots' `TeamAuthority` instances independently observe the same user message in the group chat and parse the same directive. State stays synchronized through chat-as-source-of-truth. Worth a test case in `tests/test_team_safety.py` covering both single-bot and two-bot observation paths.

**Open question: how to communicate downgrade to user.** When codex's `dangerously-skip-permissions` gets silently downgraded to `--full-auto` in team mode, the user expects danger mode based on their config. The `/status` block (Section 9.2) makes this visible, but a first-time bot-startup notice in the team group might also be warranted. Decide during implementation.

---

## Appendix A — Authority grant flow worked example

User: `@manager rebase feat/plugin-system on main and push --auth push`

1. Relay receives the message, `from_id == authenticated_user_id`, calls `team_authority.record_user_message(msg_id=N, text=...)`.
2. `parse_auth_directives` returns `frozenset({"push"})`. New `AuthorityGrant(N, {"push"}, monotonic_now)` stored.
3. Consecutive-bot-turn counter reset to 0.
4. Forward to manager bot.
5. Manager turn invokes claude with `--disallowedTools` augmented by `_team_blocks_for(authority)`. Because `authority.is_authorized("push")` returns True, `Bash(git push:*)` is removed from the block list for this turn.
6. Manager calls `git rebase`, then `git push`. Claude allows both. Manager turn ends.
7. Manager replies in chat with success. Relay increments its own `_consecutive_bot_turns` to 1. Not at cap (5). Forward proceeds.
8. If manager spontaneously starts another autonomous turn (rare but observed), counter increments per forward. At counter = 5, relay halts and emits halt notice.
9. Within 10-min TTL, `--auth push` still authorizes further pushes if the manager makes additional turns. After 10 min the grant expires.

## Appendix B — Codex prompt composition worked example

**Turn 1** (no session_id):
```
<system-reminder>
You are the 'manager' role bot in a dual-agent team group. ...
</system-reminder>

[PERSONA: software_manager]
You are a Senior Software Manager. Review protocol: before approving...
[END PERSONA]

[Recent conversation history — last 0 turns]

@team_lpct_mgr_claude_bot analyze how well is executed docs/...
```
After turn 1: `_last_team_note_hash` and `_last_persona_hash` recorded.

**Turn 2** (session_id set, persona unchanged, team-note unchanged):
```
@team_lpct_mgr_claude_bot delegate to fix the issues to developer bot
```
Just the raw user message. Codex has persona/team-note in its session history.

**Turn 3** (user runs `/use other_persona` mid-session):
- bot.py invokes `format_persona_prompt(other_persona, "")` → different text.
- `compose_user_prompt` computes `persona_hash != _last_persona_hash` → re-inject persona once.
- Resulting prompt includes the new persona block plus raw user message. Hash updated.
