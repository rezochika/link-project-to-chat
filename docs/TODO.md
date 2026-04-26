# Project TODO — Consolidated Specs & Plans

_Last refreshed 2026-04-26 from all spec/plan documents under `docs/superpowers/specs/`, `docs/superpowers/plans/`, and root-level planning docs. Reflects status as of branch `feat/transport-abstraction` HEAD._

Status legend: ✅ shipped · 🟡 in progress / partial · 📋 designed, not started · ⏳ small pending fix

---

## 1. Transport Abstraction Track

Decouples bot logic from Telegram so other platforms can plug in.

### 1.1 Shipped specs

| Spec | Version | Spec doc | Plan | Status |
|---|---|---|---|---|
| #0 Core transport | v0.13.0 | [spec](superpowers/specs/2026-04-20-transport-abstraction-design.md) | [plan](superpowers/plans/2026-04-20-transport-abstraction.md) | ✅ |
| #0b Voice port | v0.14.0 | [spec](superpowers/specs/2026-04-20-transport-voice-port-design.md) | [plan](superpowers/plans/2026-04-20-transport-voice-port.md) | ✅ |
| #0a Group / team port | v0.15.0 | [spec](superpowers/specs/2026-04-21-transport-group-team-port-design.md) | [plan](superpowers/plans/2026-04-21-transport-group-team-port.md) | ✅ |
| #0c Manager port | v0.16.0 | [spec](superpowers/specs/2026-04-21-transport-manager-port-design.md) | [plan](superpowers/plans/2026-04-21-transport-manager-port.md) | ✅ |

`bot.py` has zero direct telegram imports; `manager/bot.py` runs through `TelegramTransport` with a 7-name allowlist for `Update`/`ConversationHandler` family pinned in [tests/test_manager_lockout.py](../tests/test_manager_lockout.py). See [where-are-we.md](../where-are-we.md) for full deliverable list.

### 1.2 Spec #0 review-fix follow-ups

Plan: [2026-04-25-transport-spec0-review-fixes.md](superpowers/plans/2026-04-25-transport-spec0-review-fixes.md) (Tasks 1–11; PR #6 review feedback).

Status tracker: [2026-04-25-spec0-followups.md](2026-04-25-spec0-followups.md)

| ID | Item | Severity | Status |
|---|---|---|---|
| F1 | Soften filename sanitizer's dotfile rejection (`transport/telegram.py:_safe_basename`) | Low | ✅ closed in `4a0bb69` |
| F2 | `bot.build()` should return `None` (`bot.py:1974`) | Trivial | ✅ closed in `4a0bb69` |
| F3 | Document `TelegramTransport.start()` vs `run()` dual entry | Trivial | ✅ closed in `4a0bb69` |
| F4 | Lockout test missing `encoding="utf-8"` on `Path.read_text()` (`tests/test_transport_lockout.py:37`) — fails on non-UTF-8 default locales since `bot.py` contains em-dashes/emojis | Trivial | ✅ closed |
| A1 | Migrate `_trusted_users` persistence to string identity ids (`config.py:bind_trusted_user`) | Medium | ✅ closed by spec #1 (`2a7b8e7`) |
| A2 | Replace `int(.native_id)` casts for `group_chat_id` (4 sites in `bot.py`) | Medium | ✅ closed — schema in `13dbdd9` (`BotPeerRef`/`RoomBinding`), call-site rewrite in `8906b51` |
| A3 | Manager `_guard` legacy int path vs `_guard_invocation` string path | Low | 📋 deferred to future Conversation primitive spec |
| C1 | Port `manager/bot.py` to Transport Protocol | — | ✅ closed by spec #0c |

### 1.3 New transport platforms (designed, not implemented)

| Spec | Spec doc | Plan | Status | Notes |
|---|---|---|---|---|
| #1 Web UI | [spec](superpowers/specs/2026-04-21-transport-web-ui-design.md) | [plan](superpowers/plans/2026-04-21-web-transport.md) · [review-fix plan](superpowers/plans/2026-04-25-transport-spec1-review-fixes.md) | ✅ | Shipped 2026-04-25 (commits `6c12b39`..`d24ef52`); review-fix landed same day (commits `77abcff`..`7b73b8d`) closing P1.1/P1.2/P1.3/P1.4/P2. First non-Telegram transport. FastAPI + HTMX + SSE + SQLite. Closed A1, partially closed A2 (schema only). |
| #2 Discord | [spec](superpowers/specs/2026-04-21-transport-discord-design.md) | [plan](superpowers/plans/2026-04-21-discord-transport.md) | 📋 | Uses discord.py 2.x, depends on #1 primitives. |
| #3 Slack | [spec](superpowers/specs/2026-04-21-transport-slack-design.md) | [plan](superpowers/plans/2026-04-21-slack-transport.md) | 📋 | slack_bolt + Socket Mode; final cross-platform validation. |
| #4 Google Chat | [spec](superpowers/specs/2026-04-25-transport-google-chat-design.md) | — | 📋 | HTTP Chat app events + Google Chat REST API; Cards v2/dialogs map to `Buttons`/`PromptSpec`; depends on public HTTPS endpoint or future Pub/Sub delivery. |

---

## 2. Backend Abstraction Track

Multi-backend support (Claude → Codex / others). Phases 1–4 have shipped for the Claude + Codex pair.

| Phase | Spec | Plan | Status |
|---|---|---|---|
| Phase 1 — Claude extraction | [spec](superpowers/specs/2026-04-23-backend-phase-1-claude-extraction-design.md) | [plan](superpowers/plans/2026-04-23-backend-phase-1-claude-extraction.md) | ✅ |
| Phase 2 — Config & `/backend` command | [spec](superpowers/specs/2026-04-23-backend-phase-2-config-and-backend-command-design.md) | [plan](superpowers/plans/2026-04-23-backend-phase-2-config-and-backend-command.md) | ✅ |
| Phase 3 — Codex adapter | [spec](superpowers/specs/2026-04-23-backend-phase-3-codex-adapter-design.md) | [plan](superpowers/plans/2026-04-23-backend-phase-3-codex-adapter.md) | ✅ |
| Phase 4 — Capability expansion & hardening | [spec](superpowers/specs/2026-04-23-backend-phase-4-capability-expansion-design.md) | [plan](superpowers/plans/2026-04-23-backend-phase-4-capability-expansion-readiness.md) | ✅ |

Phase 1 evidence: `src/link_project_to_chat/backends/` (5 files: `base.py` `AgentBackend` Protocol, `claude.py` `ClaudeBackend`, `claude_parser.py`, `factory.py`, `__init__.py`). `claude_client.py` was removed; `ProjectBot` constructs a Claude backend via the factory. Commits: `0ab1c56` (Protocol+factory), `ee53d19` (move Claude client), `f1acefd` (inject into TaskManager), `f20d8d1` (route through factory + remove shim).

Phase 2 evidence: `ProjectConfig.backend` + `backend_state` dataclass fields (`config.py:55-56`), `Config.default_backend` (`config.py:104`), legacy-flat-field migration helpers (`_legacy_backend_state`, `_mirror_legacy_claude_fields`, `_effective_backend_state` at `config.py:194-241`), `/backend` command registered in `bot.py:53`, `ProjectBot.__init__(backend_name, backend_state)` (`bot.py:111`), capability-gated `/thinking`/`/permissions`/`/compact` responses, and manager-side propagation. Commits: `4828120` (config migration + dual-write), `7917b44` (helper migration + persistence call sites), `f73b43e` (`/backend` switching + capability gating), `283c5ed` (manager+CLI propagation), `cb91bb4` (parameterize Telegram-awareness preamble), `552df09` (post-phase-2 cleanup).

Phase 3 evidence: `CodexBackend` (`backends/codex.py`) implements the `AgentBackend` Protocol against the `codex exec --json` / `codex exec resume --json` CLI surface, registered with the factory under name `codex` and selectable via `/backend codex`. `codex_parser.py` translates the Codex JSONL stream (`thread.started`, `item.completed` agent_message, `turn.completed`, error frames) into the shared `StreamEvent` taxonomy. The new `BaseBackend` helper (`backends/base.py`) hosts a shared `_prepare_env` with per-backend keep/scrub allowlists — Claude scrubs `OPENAI_*` and `ANTHROPIC_*`, while Codex keeps `OPENAI_*`/`CODEX_*` but still scrubs `ANTHROPIC_*` and other token patterns. `CODEX_CAPABILITIES` declares conservative flags (no thinking, no compact, no allowed_tools, no usage-cap detection; resume enabled) and now exposes `/permissions` via Codex CLI sandbox controls: `plan` maps to read-only sandbox, `acceptEdits`/`dontAsk`/`auto` map to `--full-auto`, and bypass modes map to Codex's explicit dangerous bypass flag. Team routing context is now backend-level: `ProjectBot` injects the same peer/self @handle and relay rules into Claude and Codex team bots, and Codex prepends that note to the `codex exec` prompt. Live coverage runs only when `RUN_CODEX_LIVE=1` is set and the `codex_live` pytest marker is selected (`tests/backends/test_codex_live.py`); the live tests spawn a real `codex` subprocess inside a fresh git-initialised tmp dir, verify the round-trip emits OK as both a `TextDelta` and the closing `Result`, and confirm a follow-up turn replies AGAIN while reusing the same `session_id`. Commits: `da86be3` (codex CLI findings + parser fixtures), `5cccb8b` (shared env-policy helper), `efa1ea6` (codex JSONL parser), `01d5b80` (codex backend adapter), `7d216ec` (guard claude-only bot commands under codex), plus this Task 6 commit locking capability declarations, env policy, contract test, and live coverage.

Phase 4 evidence: Codex model selection and reasoning effort shipped in `93f8b9c`; `/backend` button picker shipped in `e2e2143`; provider-aware `/status` surfaced effort, request count, last duration, and Codex token usage in `7245199`; friendly model-label resolution shipped in `d0e4b97`; per-chat cross-backend context history shipped in `caabb76`; backend-level permissions were generalized and Codex `/permissions` enabled in `2b1dba6`; the final status slice records and displays permissions, Claude tool allow/deny lists, usage-cap state, and last backend error. Remaining Codex `False` capability flags (`supports_thinking`, `supports_compact`, `supports_allowed_tools`, `supports_usage_cap_detection`) reflect missing CLI evidence rather than adapter conservatism.

PM analysis: [backend-abstraction-pm-analysis.md](backend-abstraction-pm-analysis.md). Phase 1 smoke evidence: [backend-phase-1-smoke-evidence.md](backend-phase-1-smoke-evidence.md).

---

## 3. Earlier Feature Tracks (Shipped)

| Feature | Spec | Plan | Status |
|---|---|---|---|
| Automated project creation | [spec](superpowers/specs/2026-04-13-automated-project-creation-design.md) | [plan](superpowers/plans/2026-04-13-automated-project-creation.md) | ✅ |
| Claude skills / personas | [spec](superpowers/specs/2026-04-13-claude-skills-design.md) | (rolled into earlier work) | ✅ |
| Voice messages | (folded into spec #0b) | [plan](superpowers/plans/2026-04-14-voice-messages.md) | ✅ |
| `/create_team` command | [spec](superpowers/specs/2026-04-17-create-team-command-design.md) | [plan](superpowers/plans/2026-04-17-create-team-command.md) | ✅ |
| Dual-agent AI team | [spec](superpowers/specs/2026-04-17-dual-agent-ai-team-design.md) | [plan](superpowers/plans/2026-04-17-dual-agent-ai-team.md), [merged v2](superpowers/plans/2026-04-19-dual-agent-team-merged.md) | ✅ |
| Live streaming + thinking toggle | [spec](superpowers/specs/2026-04-20-live-stream-and-thinking-toggle-design.md) | [plan](superpowers/plans/2026-04-20-live-stream-and-thinking-toggle.md) | ✅ |

---

## 4. Security & Quality Audit

Audit: [issues-2026-04-22.md](issues-2026-04-22.md) — 29 issues + 3 post-audit operational fixes. Remediation: [2026-04-22-remediation-plan.md](2026-04-22-remediation-plan.md). PM gate: [review-2026-04-22-batch1.md](review-2026-04-22-batch1.md).

### 4.1 Batch 1 — Critical + High (security)

| ID | File | Item | Status |
|---|---|---|---|
| C1 | `task_manager.py:329–336` | `/run` resource exhaustion — concurrent cap (3) | ✅ APPROVED |
| H1 | `task_manager.py:241` | Scrub error messages (`_SENSITIVE_RE` for tokens, paths) | ✅ APPROVED w/ note |
| H2 | `bot.py:1636` | `str.startswith` → `Path.is_relative_to` (path traversal) | ✅ APPROVED (commit `3710342`) |
| H3 | `claude_client.py:255–261` | Scrub env vars (`*_TOKEN`, `*_KEY`, `AWS_*`, etc.) before subprocess | ✅ APPROVED (commit `3710342`) |
| H4 | `botfather.py:110` | Session file chmod race — chmod before `client.start()` | ✅ APPROVED |
| H5 | `tests/test_security.py` | Path-traversal tests for `_send_image` | ✅ APPROVED |
| H6 | `tests/test_security.py` | Env-var scrubbing tests | ✅ APPROVED |

**Batch 1: FULLY APPROVED.**

### 4.2 Batch 2 — Medium

| ID | File | Item | Status |
|---|---|---|---|
| M1 | `bot.py` | Atomic `find_by_message` + cancel under lock / `asyncio.shield` | ⏳ pending |
| M2 | `backends/claude.py` | `chat()` raises typed exception instead of `"Error:..."` strings (`ClaudeStreamError`) | ✅ shipped (PR #6 commit `04619cf`) |
| M4 | `config.py` | Document predictable lock path | ⏳ doc-only |
| M5 | `config.py` | Replace O(n) loop with dict-keyed update (`_merge_project_entry`) | ✅ shipped (commit `84ef5f0`) |
| M6 | `task_manager.py:518–522` | `heapq.nlargest` instead of full sort in `list_tasks` | ✅ shipped (commit `84ef5f0`) |
| M8 | `bot.py` | `tempfile.gettempdir()` instead of hardcoded `/tmp/...` | ⏳ pending |
| M10 | `tests/` | Auth tests: concurrent attempts, 30 msg/min boundary, multi-user precedence | ⏳ pending |
| M11 | `tests/` | Config I/O: malformed JSON, perm errors, concurrent access | ⏳ pending |
| M12 | `tests/` | `LiveMessage._rotate_once` boundary tests (now in `transport/streaming.py`) | ⏳ pending |
| M13 | `_auth.py` + `docs/` | Document `_auth()` + `docs/auth-migration.md` | ✅ shipped (commit `84ef5f0`) |

Retired: M3, M7, M9 (re-verified 2026-04-23).

### 4.3 Batch 3 — Low (all shipped)

| ID | File | Item | Status |
|---|---|---|---|
| L1 | `config.py` | Replace `print(..., file=sys.stderr)` with `logger.warning` | ✅ shipped (commit `60f3dba`) |
| L2 | `transport/streaming.py` (was `livestream.py`) | Hard-truncate fallback after 5 binary-search iterations | ✅ shipped (commit `60f3dba`) |
| L3 | `group_state.py` | LRU eviction (max 500) on `_states` | ✅ shipped (commit `60f3dba`) |
| L4 | `transport/streaming.py` | Named constants for magic numbers | ✅ shipped (commit `60f3dba`) |
| L5 | `_auth.py` | `.strip()` before `.lower()` on usernames | ✅ shipped (commit `60f3dba`) |
| L6 | `task_manager.py` | Document `COMPACT_PROMPT` | ✅ shipped (commit `60f3dba`) |
| L7 | `docs/CHANGELOG.md` | Auth refactor entry | ✅ shipped (commit `b8ee1af`) |

### 4.4 Test issues

| ID | Test | Status |
|---|---|---|
| F1 | `tests/test_task_manager.py::test_cancelling_waiting_input_task_releases_next_claude_task` | 🟡 intermittent; passed in latest full run; async-race suspected |
| F2 | `tests/transport/test_telegram_transport.py::test_enable_team_relay_lifecycle` | 🟡 intermittent; passed in latest full run |
| — | `tests/test_transport_lockout.py:37` — `Path(...).read_text()` encoding bug | ✅ closed (see F4 in §1.2) |

### 4.5 Post-audit operational fixes

| ID | Area | Resolution | Status |
|---|---|---|---|
| R1 | `bot.py` + `team_relay.py` | Partial-message relay short-circuit in `_on_stream_event` for group mode | ✅ commit `01f4645` |
| R2 | `team_relay.py` | `(sender, reply_to_msg_id)` coalesce buffer w/ 3s window for split messages | ✅ commit `01f4645` |
| R3 | `personas/software_manager.md` | Brevity guard (~3000 char cap on group messages) | ✅ commit `01f4645` |

---

## 5. Maintenance Fixes (Pending)

Small-scope plans, ready to implement.

| Plan | Scope | Status |
|---|---|---|
| [Fix CLI Telethon session permissions](superpowers/plans/2026-04-24-fix-cli-telethon-session-permissions.md) | Close perm race in `setup --phone`; mirror BotFatherClient pattern | ⏳ pending |
| [Fix Windows config M11 collection](superpowers/plans/2026-04-24-fix-windows-config-m11-collection.md) | Make M11 tests collect on Windows; preserve Unix root-skip | ⏳ pending |
| [Isolate OpenAI transcriber tests](superpowers/plans/2026-04-24-isolate-openai-transcriber-tests.md) | Tests pass without optional `openai` dep | ⏳ pending |
| [Update team-relay lifecycle test](superpowers/plans/2026-04-24-update-team-relay-lifecycle-test.md) | Match current TeamRelay contract (new + edited handlers) | ⏳ pending |
| Spec D′ — StringSession for team-bot relays | Manager exports `telethon.session` once and seeds subprocesses via `LP2C_TELETHON_SESSION_STRING`; eliminates the `database is locked` race on concurrent autostart (path-mode env var kept as fallback) | ✅ branch `fix/team-relay-string-session` |

---

## 6. Sandbox / Directory Jailing

[sandbox-plan.md](../sandbox-plan.md) — 📋 designed, not implemented.

Optionally restrict claude subprocess + `/run` to project directory. macOS Seatbelt (`sandbox-exec`) + Linux `bwrap`. Adds `ProjectConfig.jailed: bool = True`, `Config.projects_dir: str | None`, CLI flags `--jail/--no-jail`, manager wizard toggle.

Open questions:
1. Linux: use `landlock` package as fallback when `bwrap` absent?
2. `start --jail` runtime-only vs persisted? (rec: runtime-only)
3. `configure --projects-dir` create eagerly vs lazily? (rec: lazy)
4. `~/.claude/` writes — allow / block? (rec: allow reads, block writes)

---

## 7. Known Pending Issues (from where-are-we.md)

| Item | Location | Status |
|---|---|---|
| `_proc` is single slot — concurrent Claude tasks could overwrite | `backends/claude.py:158` (was `claude_client.py`) | ⏳ pending |
| Manager bot `/add_project` wizard allows skipping token (inconsistent with CLI) | `manager/bot.py` | ⏳ pending |
| ~~`livestream.LiveMessage` dead code~~ | ~~`livestream.py`~~ | ✅ removed (file no longer exists; project bot uses `transport/streaming.py`) |
| `WebTransport.stop()` doesn't fully release uvicorn listener; tests hardcode ports → `[Errno 98]` flakes when running suite end-to-end | `web/transport.py:stop` | ⏳ swap task-cancellation for `uvicorn_server.shutdown()` (~10 lines) |
| `tests/test_cli_transport.py` depends on `/tmp/x` existing (Click `Path(exists=True)` validation) | `tests/test_cli_transport.py` | ⏳ replace with `tmp_path` fixture |
| No end-to-end test wires `ProjectBot` + `WebTransport` + `_auth_identity` + handler in one flow | new test | ⏳ follow-up after spec #1 review-fix |

---

## Summary by Status

| Status | Count |
|---|---|
| ✅ Shipped | 6 transport specs (#0/#0a/#0b/#0c/#1) + Backend Phases 1–4 + 6 earlier features + 7 batch-1 items + 4 batch-2 items (M2/M5/M6/M13) + 7 batch-3 items (L1–L7) + 3 post-audit + 5 follow-ups (F1/F2/F3 + livestream removal + A1) |
| 🟡 Partial / intermittent | 2 intermittent flaky tests (F1, F2 in §4.4) |
| 📋 Designed, not started | 3 specs (Discord #2, Slack #3, Google Chat #4), sandbox |
| ⏳ Small pending fixes | 4 maintenance plans · 6 audit items (M1, M4, M8, M10, M11, M12) · 2 known issues · 1 deferred follow-up (A3) · WebTransport.stop() listener-release |

---

## Source Documents

**Specs:** [docs/superpowers/specs/](superpowers/specs/) (17 design docs)
**Plans:** [docs/superpowers/plans/](superpowers/plans/) (23 implementation plans)
**Audit:** [issues-2026-04-22.md](issues-2026-04-22.md) · [2026-04-22-remediation-plan.md](2026-04-22-remediation-plan.md) · [review-2026-04-22-batch1.md](review-2026-04-22-batch1.md)
**Follow-ups:** [2026-04-25-spec0-followups.md](2026-04-25-spec0-followups.md)
**State:** [where-are-we.md](../where-are-we.md) · [CHANGELOG.md](CHANGELOG.md)
**Sandbox:** [sandbox-plan.md](../sandbox-plan.md)
