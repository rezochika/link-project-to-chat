# Backend Abstraction Phase 5 — Gemini Adapter (Conservative)

**Status:** Designed, not started.
**Date:** 2026-04-27
**Part of:** Backend-abstraction track, spec #5.
**Depends on:** Spec #2 (config + `/backend` command + factory + capability gating). Spec #3 (Codex adapter — pattern reference). Spec #4 (capability expansion machinery — `_backend_buttons`, `_switch_backend`, capability-gated commands).
**Does NOT depend on:** the 34 phase 4 follow-ups in [TODO.md §2.1](../../TODO.md). Phase 5 ships against the current abstraction; structural fixes (BackendStatus TypedDict, asyncio.Lock for swap, async ConversationLog) land separately and Phase 5 inherits them.

---

## 1. Overview

With Phase 4 shipped, the system has two registered backends (Claude, Codex), a registry-driven `/backend` button picker, capability-gated commands, and a proven "ship conservative → promote on evidence" rollout cadence. **Phase 5 adds a third backend — `GeminiBackend` — wrapping Google's official `gemini-cli`.**

Phase 5 is the **third-backend test of the abstraction**. The actual design question this phase answers is: *does the `AgentBackend` Protocol generalize beyond two implementations without reshape?* If it does, the abstraction is healthy. If it doesn't, the corrective scope lives here.

This phase mirrors Phase 3's discipline: a **validation stage** captures the installed `gemini` CLI's actual behavior in a findings doc; then the adapter is written against that doc, declaring only capabilities the findings demonstrate. Everything else is `False` and handled by the existing capability gate. Future capability promotion lives in a hypothetical Phase 6, not here.

## 2. Goals & non-goals

**Goals**
- Validate the installed Gemini CLI: command shape, streaming JSONL format, resume/session model, auth/env-var requirements, exit/error codes, sandbox behavior. Produce `docs/superpowers/specs/2026-04-27-gemini-cli-findings.md`.
- Implement `GeminiBackend` in `backends/gemini.py` + a parser in `backends/gemini_parser.py`.
- Register `"gemini"` with the backend factory.
- Declare `BackendCapabilities` for Gemini conservatively — only what's validated by Task 0.
- Per-backend env-var policy: keep `GEMINI_*` and `GOOGLE_*`, scrub Anthropic/OpenAI/Codex tokens.
- Ship the **correct subprocess lifecycle from day 1** (preempt the P4-C2 zombie-proc bug that Codex still has) — Gemini's `chat_stream` `finally` terminates the process before clearing `_proc`.

**Non-goals**
- No `/model` picker for Gemini. `models = ()`. Promotion deferred to a hypothetical Phase 6 once usage surfaces a real user-asked-for gap.
- No `/effort` for Gemini. `supports_effort = False`. Promotion conditional on Task 0 finding a reasoning-level CLI flag AND a real user need.
- No `/permissions` for Gemini. `supports_permissions = False` even if Task 0 reveals `--sandbox` flags — the mapping work happens in Phase 6.
- No `/thinking`, `/compact`, allowed-tools, usage-cap detection.
- No manager-bot `/default_backend` command (separate, future spec).
- No bot.py changes — `_backend_buttons` and `_switch_backend` are already registry-driven.
- No config.py changes — `backend_state` is already keyed by backend name; the `gemini` slot is created lazily on first use.
- No backfill of phase 4 follow-up fixes for Codex/Claude (those happen on a separate branch). Exception: Section 7 ships the C2-class lifecycle fix in Gemini's own code preemptively.

## 3. Decisions driving this design

| # | Question | Decision |
|---|---|---|
| 1 | Local CLI evidence vs ship-and-discover? | **Validate first.** Q1 of brainstorming established no local Gemini CLI is available at spec time. Task 0 produces the findings doc the adapter is written against. Same discipline as Phase 3. |
| 2 | Match Phase 3's adapter shape, Phase 3+4 merged, or Phase 4's multi-doc readiness package? | **Phase-3-style adapter only.** Single design doc + single implementation plan. Capability-promotion deferred to a hypothetical Phase 6. (Q2 of brainstorming.) |
| 3 | Sequence relative to phase 4 follow-ups? | **In parallel.** Phase 5 ships against current abstraction; phase 4 follow-ups land separately and Gemini inherits the fixes. (Q3 of brainstorming.) |
| 4 | Which Gemini CLI binary? | **Google's official `gemini-cli`** (npm `@google/gemini-cli`, binary `gemini`). The agentic-style CLI with streaming JSONL output and tool support — closest fit to the Codex pattern. (Q4 of brainstorming.) |
| 5 | How aggressive should v1 capability declarations be? | **Maximum conservative.** All `BackendCapabilities` flags `False`, `models = ()`, `effort_levels = ()`. Only flags Task 0 directly demonstrates may flip True. (Q5 of brainstorming.) |
| 6 | Codex-clone, Claude-clone, or single-file inline structure? | **Codex-clone** (Approach 1). Per-turn subprocess, separate parser file, mirrored test layout. Diffability against `codex.py` makes review trivial and proves the abstraction generalizes. |
| 7 | Carry the P4-C2 zombie-proc lesson into Gemini's lifecycle? | **Yes.** Gemini's `chat_stream` `finally` terminates the process before clearing `_proc` from day 1. When P4-C2 is later fixed in Codex, the two backends end up identical here. |

## 4. Architecture

### 4.1 Stage A — Gemini CLI validation (Task 0)

Before any adapter code, produce `docs/superpowers/specs/2026-04-27-gemini-cli-findings.md`. Each section contains verbatim CLI captures, no speculation. Mirrors `2026-04-23-codex-cli-findings.md` exactly.

The findings doc must answer:

1. **Binary identity & version.** `gemini --version` — pinned exactly.
2. **One-shot turn invocation.** Subcommand and flags that take a prompt and exit. Likely `gemini`, `gemini chat`, or `gemini exec`. Captured form is what `_build_cmd` uses.
3. **JSON / streaming output.** Equivalent of `codex exec --json`. Per-line JSONL shape — what events does a successful turn emit?
4. **Session resume.** Does the CLI emit `session_id` / `thread_id` and accept a resume form? This is the only capability flag that may flip True at first ship.
5. **Authentication.** `GEMINI_API_KEY` env var, OAuth credentials file under `~/.gemini/`, or both? Pins the env-policy keep-list.
6. **Cwd / sandbox.** Does Gemini CLI refuse to run outside a git repo (Codex 0.125 did)? Sandbox flags?
7. **Model selection flag.** Likely `--model <slug>`. Captured but `models = ()` regardless in v1.
8. **Stderr noise patterns.** Benign stderr during a successful turn (Codex emits `failed to record rollout items`). Logged but doesn't fail the turn.
9. **Failure modes.** Auth missing, network down, invalid model, rate limit — what does stderr show?
10. **Dangerous edge cases.** Truncation, large-prompt rejection, sub-command differences.

**Sign-off gate:** Reviewer reads the findings doc and confirms each of the 10 items has a captured answer (not "unknown"). Only then does the implementation plan unblock.

### 4.2 Stage B — Adapter & parser

```
src/link_project_to_chat/backends/
├── gemini.py             NEW — GeminiBackend(BaseBackend) + GEMINI_CAPABILITIES + register("gemini", _make_gemini)
├── gemini_parser.py      NEW — parse_gemini_line(line) → ParsedFrame
├── codex.py              UNCHANGED
├── claude.py             UNCHANGED
├── factory.py            UNCHANGED (registry-driven; no list to update)
└── base.py               UNCHANGED (Protocol already supports the surface Gemini needs)
```

**Module dependencies (intentional):**
- `gemini.py` imports `BaseBackend`, `BackendCapabilities`, `HealthStatus` from `.base`; `parse_gemini_line` from `.gemini_parser`; `register` from `.factory`. Lazy-imports `task_manager._command_popen_kwargs` and `_terminate_process_tree` inside `_popen` and `close_interactive` to avoid the circular path through `backends/__init__.py` (same pattern Phase 3 used).
- `gemini_parser.py` imports only the `StreamEvent` taxonomy from `..events`. No bot/transport/config dependencies. Single-purpose.

**Public surface mirrors Codex:**
```python
class GeminiBackend(BaseBackend):
    name = "gemini"
    capabilities = GEMINI_CAPABILITIES
    MODEL_OPTIONS = []
    _env_keep_patterns = ("GEMINI_*", "GOOGLE_*")
    _env_scrub_patterns = (
        "*_TOKEN", "*_KEY", "*_SECRET",
        "ANTHROPIC_*", "OPENAI_*", "CODEX_*",
        "AWS_*", "GITHUB_*", "DATABASE_*", "PASSWORD*",
    )

    def __init__(self, project_path: Path, state: dict): ...
    def _build_cmd(self, user_message: str) -> list[str]: ...
    def _build_prompt(self, user_message: str) -> str: ...
    def _popen(self, cmd: list[str]) -> subprocess.Popen: ...
    async def chat_stream(self, user_message, on_proc=None): ...
    async def chat(self, user_message, on_proc=None) -> str: ...
    async def probe_health(self) -> HealthStatus: ...
    def close_interactive(self) -> None: ...
    def cancel(self) -> bool: ...
    def current_permission(self) -> str: ...        # returns "default" — supports_permissions=False
    def set_permission(self, mode: str | None) -> None: ...  # ignored — supports_permissions=False
    @property
    def status(self) -> dict: ...
```

**No bot.py changes.** `/backend gemini` works automatically because Phase 4's `_backend_buttons` reads from `backends.factory.available()` and `_switch_backend` is registry-driven. The capability gate handles `/model`, `/effort`, `/permissions` rejection.

**No config.py changes.** `backend_state["gemini"]` is created on first use; persisted fields (`session_id` if resume support is confirmed) flow through the existing `_patch_backend_config` helper.

## 5. Capability declaration

The exact `BackendCapabilities` instance committed at first code commit:

```python
GEMINI_CAPABILITIES = BackendCapabilities(
    models=(),                           # /model rejected
    supports_thinking=False,             # no verified thinking-event surface
    supports_permissions=False,          # /permissions rejected
    supports_resume=False,               # promoted to True by Task 0 IFF the CLI exposes session-id + resume; otherwise stays False
    supports_compact=False,
    supports_allowed_tools=False,
    supports_usage_cap_detection=False,
    supports_effort=False,
    effort_levels=(),
)
```

**Pinning test:** `tests/backends/test_capability_declaration.py::test_gemini_capabilities_match_validated_findings` asserts the exact tuple. Any future change requires updating BOTH the declaration AND the findings doc — drift is impossible to ship silently.

**User-visible behavior at first ship:**
- `/model`, `/effort`, `/permissions`, `/compact`, `/thinking` reject with `"This backend doesn't support /<cmd>."` via the existing capability gate.
- `/status` shows `Backend: gemini`, `Model: default`, no effort/permissions/tools lines.
- `/backend` button picker includes `gemini` automatically.

**Default model passed to CLI:** if Task 0 finds `gemini-cli` requires `--model`, `_build_cmd` hardcodes `gemini-2.5-pro` (most-capable; minimizes surprise) until model promotion lands in Phase 6. Otherwise `_build_cmd` omits `--model` and lets the CLI pick its own default.

## 6. Env policy

```python
_env_keep_patterns = ("GEMINI_*", "GOOGLE_*")
_env_scrub_patterns = (
    "*_TOKEN", "*_KEY", "*_SECRET",
    "ANTHROPIC_*", "OPENAI_*", "CODEX_*",
    "AWS_*", "GITHUB_*", "DATABASE_*", "PASSWORD*",
)
```

**Rationale per pattern:**
- `GEMINI_*` — covers `GEMINI_API_KEY` and any future config envs. Required for auth.
- `GOOGLE_*` — covers `GOOGLE_APPLICATION_CREDENTIALS`, `GOOGLE_CLOUD_PROJECT`. Needed if Task 0 confirms gcloud-auth fallback.
- `ANTHROPIC_*` / `OPENAI_*` / `CODEX_*` — defense-in-depth: a misconfigured machine shouldn't leak Claude or Codex auth into the Gemini subprocess.
- `*_TOKEN` / `*_KEY` / `*_SECRET` — generic shape match. Catches `GITHUB_TOKEN`, `STRIPE_KEY`, etc. that have no business in an LLM subprocess.

**Order of evaluation:** `_env_keep_patterns` matches FIRST in `BaseBackend._prepare_env` (Phase 3 invariant) — so `GEMINI_API_KEY` survives even though it would also match `*_KEY`.

**Test:** `tests/backends/test_env_policy.py::test_gemini_keeps_google_but_scrubs_anthropic_and_openai` — sets a representative env, calls `_prepare_env`, asserts kept set vs scrubbed set.

**Open question deferred to Task 0:** if `gemini-cli` reads only from a credentials file (`~/.gemini/oauth_creds.json` or similar) rather than env vars, the env policy is best-effort and the credentials file becomes the actual auth surface. The findings doc captures this and the spec is updated before code lands.

## 7. Lifecycle, cancel, and the P4-C2 lesson

**Process model:** Per-turn subprocess — Codex pattern, not Claude's persistent REPL. `gemini exec --json "<prompt>"` (exact form from Task 0) spawned at each turn, exits at `turn.completed` equivalent.

**`chat_stream` skeleton** mirrors `codex.py` but **fixes P4-C2 from day 1**:

```python
async def chat_stream(self, user_message, on_proc=None):
    cmd = self._build_cmd(user_message)
    proc = self._popen(cmd)
    self._proc = proc
    if on_proc:
        on_proc(proc)
    self._started_at = time.monotonic()
    self._total_requests += 1

    collected_text: list[str] = []
    try:
        async for parsed in self._iter_lines(proc):
            for event in parsed.events:
                if isinstance(event, TextDelta):
                    collected_text.append(event.text)
                yield event
            if parsed.turn_completed:
                yield Result(
                    text="".join(collected_text) or "[No response]",
                    session_id=self.session_id,
                    model=self.model_display,
                )
                await self._drain_and_reap(proc)
                return
        # stdout EOF without turn-complete
        await self._drain_and_reap_with_error(proc)
    finally:
        # P4-C2 fix: terminate before clearing _proc, so an early
        # generator close (CancelledError, exception, early return)
        # doesn't orphan the subprocess.
        if proc.poll() is None:
            try:
                proc.kill()
                await asyncio.to_thread(proc.wait, 5)
            except Exception:
                logger.exception("gemini chat_stream cleanup failed pid=%s", proc.pid)
        if self._proc is proc:
            self._proc = None
            self._started_at = None
```

**Why this matters:** The phase 4 audit ([TODO.md §2.1 P4-C2](../../TODO.md)) found that `CodexBackend.chat_stream`'s `finally` clears `_proc` without killing the subprocess, leaving zombies on early generator close. Gemini ships with the corrected order from day 1. When P4-C2 is fixed in Codex later, the two backends end up identical here — exactly the goal.

**`cancel()`** mirrors Codex: bare `proc.kill()`. Tree-kill happens via `task_manager`'s `CancelledError` handler calling `close_interactive()`.

**`close_interactive()`** mirrors Codex but with the same P4-C2 lesson applied: terminate the process tree, then clear `_proc`. Lazy-imports `task_manager._terminate_process_tree` to avoid the circular path through `backends/__init__.py`.

**Regression test (in spec, not deferred):** `tests/backends/test_gemini_backend.py::test_chat_stream_kills_proc_on_generator_close` — closes the async generator before `turn_completed`, asserts `proc.poll() is not None` afterwards. This is the test missing for Codex (P4-T4 in §2.1); Gemini ships it from day 1.

**Stderr handling:** Read after stdout EOF or after `turn_completed`. Benign patterns from Task 0 logged at WARNING. Non-benign stderr on a non-zero exit becomes `Error(message=...)` and `_last_error`.

## 8. Tests

Three layers, mirroring Phase 3:

### 8.1 Unit tests — `tests/backends/test_gemini_backend.py`

Always run. Driven by `tests/fixtures/gemini_exec_ok.jsonl` and `gemini_exec_error.jsonl` (captured during Task 0).

- `test_build_cmd_emits_minimum_args` — `_build_cmd("hi")` produces the verified command shape; no `--model`, no effort, no permission flags.
- `test_build_cmd_appends_resume_when_session_id_set` — only if Task 0 confirmed resume support.
- `test_chat_stream_emits_text_delta_then_result` — fixture-driven; asserts ≥1 `TextDelta` then closing `Result` with joined text.
- `test_chat_stream_drains_proc_after_turn_completed` — `proc.wait` awaited before `_proc` clears (regression for the C2-class lifecycle bug).
- `test_chat_stream_kills_proc_on_generator_close` — close generator early, assert `proc.poll() is not None`. Missing for Codex per P4-T4; Gemini ships it.
- `test_chat_stream_logs_post_turn_nonzero_exit` — non-zero exit after clean `turn_completed` → WARNING log, not exception.
- `test_successful_stderr_warning_does_not_fail_turn` — benign stderr (Task 0 patterns) alongside clean stdout still yields `Result`.
- `test_cancel_kills_proc` — `cancel()` returns `True` and proc terminates.
- `test_close_interactive_terminates_tree` — verifies the lazy-import path through `_terminate_process_tree`.
- `test_status_shape` — `backend.status` returns the full dict shape (`running`, `pid`, `session_id`, `total_requests`, `last_message`, `last_error`). Forward-compatible with the BackendStatus TypedDict that P4-I9 will introduce.

### 8.2 Contract tests — `tests/backends/test_contract.py`

Parametrized; once `register("gemini", _make_gemini)` runs at import time, the existing tests pick up the new backend automatically.

- `test_backend_contract_declares_name_and_capabilities[gemini]`
- `test_backend_contract_chat_returns_string[gemini]` (skips if Task 0 finds no fixture-driveable path)
- `test_backend_contract_probe_health[gemini]`

### 8.3 Live tests — `tests/backends/test_gemini_live.py`

Marker `@pytest.mark.gemini_live`; skip unless `RUN_GEMINI_LIVE=1`. Conftest registers the marker alongside `codex_live`; `_isolate_home` exempts it (same way it exempts `codex_live`) so the real `~/.gemini/` auth is visible to the spawned process.

- `test_gemini_live_round_trip` — spawns real `gemini` subprocess in a fresh git-init'd `tmp_path`, asks for "OK", asserts ≥1 `TextDelta` and a closing `Result`.
- `test_gemini_live_resume_reuses_session` — only if `supports_resume=True`; second turn reuses `session_id`.

### 8.4 Capability declaration & env policy tests

- `tests/backends/test_capability_declaration.py::test_gemini_capabilities_match_validated_findings` pins the exact `BackendCapabilities` tuple, comment-linked to the line in the findings doc proving each `True` flag.
- `tests/backends/test_env_policy.py::test_gemini_keeps_google_but_scrubs_anthropic_and_openai` (described in §6).

### 8.5 Test fixtures

`tests/fixtures/gemini_exec_ok.jsonl` and `gemini_exec_error.jsonl` are captured directly during Task 0 (literal `gemini exec ... > ok.jsonl`), not synthesized. The findings doc cites the capture command for reproducibility.

### 8.6 pytest registration

- `pyproject.toml` — register the `gemini_live` marker.
- `tests/conftest.py` — `_isolate_home` exempts the `gemini_live` marker (same line shape as `codex_live`).

## 9. Out of scope

- `/model` picker for Gemini. `models = ()`. Promotion lives in a future Phase 6.
- `/effort`, `/permissions`, `/thinking`, `/compact`, `/allowed_tools` for Gemini.
- Usage-cap detection for Gemini.
- Manager-bot `/default_backend` command.
- Bot.py changes — `_backend_buttons` and `_switch_backend` are already registry-driven. If a third backend exposes a gap there, it lands in a separate slice.
- Phase 4 follow-up fixes for Codex/Claude (those happen on a separate branch). Exception: Section 7 ships the C2-class lifecycle fix in Gemini's own code preemptively.
- Auto-routing or per-message backend override.
- Adding Gemini to the manager-bot wizard's backend choice (the existing `/backend` command in the project bot is sufficient for opt-in).

## 10. Open questions (resolved during Task 0)

1. Final value of `supports_resume` (True if a session-id mechanism exists).
2. Default model the adapter passes when `self.model is None` (or whether `--model` is omitted entirely).
3. Final env-policy keep-list (auth via `GEMINI_API_KEY` env var vs OAuth credential file).
4. Whether `gemini-cli` requires a git-initialized cwd (Codex 0.125 did).
5. Whether stderr emits any benign noise on a successful turn that needs documented tolerance.

These get answered in `2026-04-27-gemini-cli-findings.md` and folded back into this spec before any code commit.

## 11. Triggers for writing a Phase 6 spec (Gemini capability expansion)

Mirrors the Phase 4 trigger list. Write Phase 6 when at least two of:

- A real user reports `/model` was rejected for `gemini-cli` despite `--model <slug>` being a documented flag (the Codex/Phase 4 trigger pattern).
- Phase 5 has been opt-in for the agreed soak window with concrete user-reported gaps.
- A capability Phase 5 declared `False` has a clear path to `True` based on observed CLI behavior.
- A Gemini-specific stderr/error pattern hits real users and needs handling.
- A `gemini-cli` version bump exposes a new event type or capability flag.

## 12. Rollback plan

The opt-in nature (`/backend gemini` only — never the default) means Phase 5 is rolled back by removing the `register("gemini", _make_gemini)` line in `gemini.py` (or deleting the file outright). No bot.py / config.py changes to undo. Existing Claude/Codex users see no difference.

If Task 0 reveals the `gemini-cli` JSONL surface is structurally incompatible with the existing `AgentBackend` Protocol (e.g., no per-turn process exit, no streaming events), the corrective scope lands here:
- Option A: extend `AgentBackend` Protocol with the minimum new method needed; both Claude and Codex inherit a no-op default.
- Option B: write a different adapter shape (HTTP-server-based, mirror Approach 2 from brainstorming) and document the divergence.

## 13. Sign-off

Phase 5 ships when:
1. **Task 0's findings doc is committed and reviewed.** Each of the 10 questions in §4.1 has a captured answer (not "unknown").
2. **The adapter passes the unit + contract test suite at conservative declarations.**
3. **The live test suite passes on at least one authenticated machine OR skips cleanly on machines without `gemini-cli` installed.**

No further Phase 5 slice is planned without fresh evidence. Any future expansion needs a Phase 6 spec written from real Gemini-usage gaps.
